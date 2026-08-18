"""Cross-platform Chrome profile manual auth helper (Mac/Windows/Linux)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

MC_HOST = "https://monkeycode-ai.net"
START_URL = MC_HOST + "/console/tasks"
DEFAULT_TIMEOUT = 900
POLL_SEC = 1.5


def log(msg: str) -> None:
    print(f"[chrome-auth] {msg}", flush=True)


def find_chrome() -> str:
    env = os.environ.get("MC_CONSOLE_CHROME") or os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env

    system = platform.system()
    candidates: List[str] = []
    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif system == "Windows":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            str(Path(pf) / "Google/Chrome/Application/chrome.exe"),
            str(Path(pf86) / "Google/Chrome/Application/chrome.exe"),
            str(Path(local) / "Google/Chrome/Application/chrome.exe") if local else "",
            str(Path(pf) / "Microsoft/Edge/Application/msedge.exe"),
            str(Path(pf86) / "Microsoft/Edge/Application/msedge.exe"),
            str(Path(local) / "Microsoft/Edge/Application/msedge.exe") if local else "",
            str(Path(pf) / "Chromium/Application/chrome.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            shutil.which("google-chrome") or "",
            shutil.which("chromium") or "",
            shutil.which("chromium-browser") or "",
        ]

    for c in candidates:
        if c and Path(c).exists():
            return c
    # last resort: PATH names
    for name in ("google-chrome", "chrome", "chromium", "msedge"):
        w = shutil.which(name)
        if w:
            return w
    raise FileNotFoundError(
        "未找到 Chrome/Edge/Chromium。请安装 Google Chrome，或设置环境变量 MC_CONSOLE_CHROME 指向浏览器可执行文件。"
    )


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class SimpleWS:
    """Minimal WebSocket client for Chrome CDP (text frames)."""

    def __init__(self, url: str, origin: str, timeout: float = 5.0):
        u = urlparse(url)
        if u.scheme not in ("ws", "http"):
            raise ValueError("unsupported ws url: " + url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n"
            f"\r\n"
        )
        self.sock.sendall(req.encode("utf-8"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP websocket handshake closed")
            buf += chunk
        header = buf.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
        if "101" not in header.split("\r\n", 1)[0]:
            raise ConnectionError("CDP websocket upgrade failed: " + header[:200])
        self._id = 0

    def _recv_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("socket closed")
            out += chunk
        return out

    def _recv_frame(self) -> tuple[int, bytes]:
        b1, b2 = self._recv_exact(2)
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        n = len(data)
        mask_bit = 0x80  # client must mask
        if n < 126:
            header.append(mask_bit | n)
        elif n < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", n))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self, timeout: Optional[float] = None) -> str:
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x1:  # text
                return payload.decode("utf-8", "replace")
            if opcode == 0x8:  # close
                raise ConnectionError("ws closed")
            if opcode == 0x9:  # ping -> pong
                # send pong
                frame = bytearray([0x8A, 0x80 | len(payload)])
                mask = os.urandom(4)
                frame.extend(mask)
                frame.extend(bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                self.sock.sendall(bytes(frame))
                continue
            # ignore binary/continuation

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 8.0) -> dict:
        self._id += 1
        mid = self._id
        self.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            remaining = max(0.1, end - time.time())
            try:
                msg = json.loads(self.recv_text(timeout=remaining))
            except Exception:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError(method)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def cdp_version(port: int) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def cdp_targets(port: int) -> List[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1.5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return []


def get_session_cookie(port: int) -> Optional[str]:
    targets = cdp_targets(port)
    page = None
    for t in targets:
        if t.get("type") == "page" and "monkeycode" in (t.get("url") or ""):
            page = t
            break
    if not page:
        for t in targets:
            if t.get("type") == "page":
                page = t
                break
    ws_url = (page or {}).get("webSocketDebuggerUrl")
    if not ws_url:
        ver = cdp_version(port) or {}
        ws_url = ver.get("webSocketDebuggerUrl")
    if not ws_url:
        return None
    origin = f"http://127.0.0.1:{port}"
    ws = SimpleWS(ws_url, origin=origin, timeout=5)
    try:
        try:
            res = ws.call("Network.getCookies", {"urls": [MC_HOST, MC_HOST + "/", START_URL]})
            cookies = res.get("cookies") or []
        except Exception:
            res = ws.call("Storage.getCookies")
            cookies = res.get("cookies") or []
        for c in cookies:
            if c.get("name") == "monkeycode_ai_session" and c.get("value"):
                return str(c.get("value"))
        return None
    finally:
        ws.close()


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        else:
            # kill process group if started with start_new_session
            try:
                os.killpg(proc.pid, 15)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(proc.pid, 9)
                except Exception:
                    proc.kill()
    except Exception as e:
        log(f"kill chrome warn: {e}")
        try:
            proc.kill()
        except Exception:
            pass


def safe_rmtree(path: Path, retries: int = 8) -> None:
    if not path.exists():
        return
    last_err = None
    for i in range(retries):
        try:
            shutil.rmtree(path, ignore_errors=False)
            if not path.exists():
                return
        except Exception as e:
            last_err = e
            time.sleep(0.4 + i * 0.2)
            # Windows file locks: try clearing readonly
            if platform.system() == "Windows":
                try:
                    for root, dirs, files in os.walk(path):
                        for name in files:
                            fp = Path(root) / name
                            try:
                                os.chmod(fp, 0o666)
                            except Exception:
                                pass
                except Exception:
                    pass
    # final best-effort
    shutil.rmtree(path, ignore_errors=True)
    if path.exists() and last_err:
        log(f"profile cleanup incomplete: {path} ({last_err})")


@dataclass
class AuthJob:
    id: str
    state: str = "starting"  # starting|waiting_login|minting|done|error|timeout|cancelled
    message: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    email: str = ""
    account_id: Optional[int] = None
    error: str = ""
    debug_port: int = 0
    profile_dir: str = ""
    chrome_path: str = ""
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "email": self.email,
            "account_id": self.account_id,
            "error": self.error,
            "debug_port": self.debug_port,
        }


class ManualAuthManager:
    def __init__(self, work_root: Path, mint_callback: Callable[[str], Dict[str, Any]], timeout: int = DEFAULT_TIMEOUT):
        self.work_root = work_root
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.mint_callback = mint_callback
        self.timeout = timeout
        self._lock = threading.Lock()
        self._jobs: Dict[str, AuthJob] = {}

    def get(self, job_id: str) -> Optional[AuthJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_active(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [j.to_public() for j in self._jobs.values() if j.state in ("starting", "waiting_login", "minting")]

    def start(self) -> AuthJob:
        # only one interactive job at a time to avoid profile chaos
        with self._lock:
            for j in self._jobs.values():
                if j.state in ("starting", "waiting_login", "minting"):
                    raise RuntimeError("已有进行中的手动授权，请先完成或取消")

        chrome = find_chrome()
        port = pick_free_port()
        job_id = hashlib.sha1(f"{time.time()}-{port}".encode()).hexdigest()[:12]
        profile = self.work_root / f"profile-{job_id}"
        if profile.exists():
            safe_rmtree(profile)
        profile.mkdir(parents=True, exist_ok=True)

        job = AuthJob(
            id=job_id,
            state="starting",
            message="正在启动独立 Chrome Profile…",
            debug_port=port,
            profile_dir=str(profile),
            chrome_path=chrome,
        )
        with self._lock:
            self._jobs[job_id] = job

        args = [
            chrome,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={str(profile)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=ChromeWhatsNewUI,TranslateUI",
            "--new-window",
            START_URL,
        ]
        # Windows: hide console flash not needed for GUI chrome
        popen_kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
            # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS-ish
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        log(f"launch chrome job={job_id} port={port} profile={profile}")
        proc = subprocess.Popen(args, **popen_kwargs)
        job._proc = proc

        th = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        job._thread = th
        th.start()
        return job

    def cancel(self, job_id: str) -> AuthJob:
        job = self.get(job_id)
        if not job:
            raise KeyError("job not found")
        job._stop.set()
        job.state = "cancelled"
        job.message = "已取消"
        job.updated_at = time.time()
        self._cleanup(job)
        return job

    def _set(self, job: AuthJob, state: str, message: str, **extra: Any) -> None:
        job.state = state
        job.message = message
        job.updated_at = time.time()
        for k, v in extra.items():
            setattr(job, k, v)

    def _cleanup(self, job: AuthJob) -> None:
        if job._proc is not None:
            kill_process_tree(job._proc)
            job._proc = None
        # give OS a moment to release file locks
        time.sleep(0.6)
        if job.profile_dir:
            safe_rmtree(Path(job.profile_dir))
            log(f"removed profile {job.profile_dir}")

    def _run_job(self, job: AuthJob) -> None:
        try:
            # wait CDP
            for _ in range(80):
                if job._stop.is_set():
                    self._set(job, "cancelled", "已取消")
                    return
                if cdp_version(job.debug_port):
                    break
                if job._proc and job._proc.poll() is not None:
                    self._set(job, "error", "Chrome 意外退出", error="chrome exited early")
                    return
                time.sleep(0.25)
            else:
                self._set(job, "error", "Chrome 调试端口未就绪", error="cdp timeout")
                return

            self._set(job, "waiting_login", "请在弹出的 Chrome 窗口完成上游登录授权…")
            deadline = time.time() + self.timeout
            session = None
            while time.time() < deadline:
                if job._stop.is_set():
                    self._set(job, "cancelled", "已取消")
                    return
                if job._proc and job._proc.poll() is not None:
                    # user closed chrome before login
                    self._set(job, "error", "Chrome 已关闭，未完成授权", error="chrome closed")
                    return
                try:
                    session = get_session_cookie(job.debug_port)
                except Exception as e:
                    log(f"cookie poll: {e}")
                    session = None
                if session:
                    break
                time.sleep(POLL_SEC)

            if not session:
                self._set(job, "timeout", "等待登录超时", error="timeout")
                return

            self._set(job, "minting", "已检测到登录，正在签发凭证并入库…")
            result = self.mint_callback(session)
            account = result.get("account") or {}
            user = result.get("user") or {}
            email = account.get("email") or user.get("email") or ""
            self._set(
                job,
                "done",
                f"授权完成：{email or 'ok'}",
                email=email,
                account_id=account.get("id"),
            )
        except Exception as e:
            self._set(job, "error", f"授权失败：{e}", error=str(e))
            log(f"job error: {e}")
        finally:
            # Always delete the temporary profile after finish
            try:
                self._cleanup(job)
            except Exception as e:
                log(f"cleanup error: {e}")
            # prune old finished jobs (keep last 20)
            with self._lock:
                finished = [j for j in self._jobs.values() if j.state not in ("starting", "waiting_login", "minting")]
                finished.sort(key=lambda x: x.updated_at, reverse=True)
                for j in finished[20:]:
                    self._jobs.pop(j.id, None)
