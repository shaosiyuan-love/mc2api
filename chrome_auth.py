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
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# 上游控制台可能同时存在 .com / .net；手动授权默认打开 .com，Cookie 双域都取。
MC_HOSTS = [
    (os.environ.get("MC_CONSOLE_WEB") or "https://monkeycode-ai.com").rstrip("/"),
    "https://monkeycode-ai.net",
    "https://monkeycode-ai.com",
]
# de-dupe preserve order
_seen = set()
MC_HOSTS = [h for h in MC_HOSTS if not (h in _seen or _seen.add(h))]  # type: ignore[func-returns-value]
MC_HOST = MC_HOSTS[0]
START_URL = MC_HOST + "/console/tasks"
COOKIE_NAME = "monkeycode_ai_session"
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


def _cookie_probe_urls() -> List[str]:
    urls: List[str] = []
    for host in MC_HOSTS:
        urls.extend(
            [
                host,
                host + "/",
                host + "/console/tasks",
                host + "/console",
                host + "/login",
                host + "/api/v1/users/status",
            ]
        )
    return urls


def _is_session_cookie_name(name: str) -> bool:
    n = (name or "").lower().strip()
    if not n:
        return False
    if n == COOKIE_NAME:
        return True
    # tolerate renames / prefixes
    aliases = (
        "monkeycode_ai_session",
        "monkeycode_session",
        "mc_session",
        "mc_ai_session",
        "ohmyagent_session",
    )
    if n in aliases:
        return True
    if "session" in n and ("monkey" in n or "ohmy" in n or n.endswith("_session")):
        return True
    return False


def _pick_session_cookie(cookies: List[dict]) -> Optional[dict]:
    ranked: List[Tuple[int, dict]] = []
    for c in cookies:
        name = str(c.get("name") or "")
        val = str(c.get("value") or "").strip()
        if not val or not _is_session_cookie_name(name):
            continue
        domain = str(c.get("domain") or "").lower()
        score = 0
        if name.lower() == COOKIE_NAME:
            score += 5
        if "monkeycode-ai.com" in domain:
            score += 3
        elif "monkeycode-ai.net" in domain:
            score += 2
        elif "monkeycode" in domain:
            score += 1
        if len(val) >= 20:
            score += 1
        ranked.append((score, c))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[0][1] if ranked else None


def _pick_session_value(cookies: List[dict]) -> Optional[str]:
    c = _pick_session_cookie(cookies)
    if not c:
        return None
    return str(c.get("value") or "").strip() or None


def _cookie_relevant(c: dict) -> bool:
    """Keep session + CDN/WAF cookies needed for API auth."""
    name = str(c.get("name") or "").lower()
    domain = str(c.get("domain") or "").lower()
    if not name or not str(c.get("value") or "").strip():
        return False
    if _is_session_cookie_name(name):
        return True
    # Cloudflare / common edge cookies
    if name.startswith("cf_") or name in ("__cf_bm", "cf_clearance", "__cflb", "_cfuvid"):
        return True
    if "monkeycode" in domain or domain in ("document.cookie",):
        return True
    return False


def build_cookie_header(cookies: List[dict]) -> str:
    """Build Cookie request header; last value wins per name."""
    merged: Dict[str, str] = {}
    for c in cookies:
        if not _cookie_relevant(c):
            continue
        name = str(c.get("name") or "").strip()
        val = str(c.get("value") or "").strip()
        if not name or not val:
            continue
        merged[name] = val
    # ensure session key present if any session-like exists
    if COOKIE_NAME not in merged:
        for n, v in list(merged.items()):
            if _is_session_cookie_name(n):
                merged[COOKIE_NAME] = v
                break
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def preferred_web_from_cookies(cookies: List[dict], page_url: str = "") -> str:
    blob = " ".join(
        str(c.get("domain") or "") for c in cookies
    ) + " " + (page_url or "")
    blob = blob.lower()
    if "monkeycode-ai.com" in blob:
        return "https://monkeycode-ai.com"
    if "monkeycode-ai.net" in blob:
        return "https://monkeycode-ai.net"
    return MC_HOST


def _collect_cookies_via_ws(ws: SimpleWS) -> List[dict]:
    cookies: List[dict] = []
    # Enable domains (ignore failures on older/newer Chrome)
    for method in ("Network.enable", "Storage.enable", "Page.enable"):
        try:
            ws.call(method, timeout=3.0)
        except Exception:
            pass

    # 1) all cookies (still works on many Chrome builds)
    for method, params in (
        ("Network.getAllCookies", None),
        ("Network.getCookies", {"urls": _cookie_probe_urls()}),
        ("Storage.getCookies", None),
    ):
        try:
            res = ws.call(method, params, timeout=5.0)
            part = res.get("cookies") or []
            if isinstance(part, list) and part:
                cookies.extend(part)
        except Exception:
            continue

    # 2) document.cookie fallback (non-HttpOnly only)
    try:
        res = ws.call(
            "Runtime.evaluate",
            {
                "expression": "(() => { try { return document.cookie || ''; } catch (e) { return ''; } })()",
                "returnByValue": True,
            },
            timeout=5.0,
        )
        raw = ((res.get("result") or {}).get("value")) or ""
        if isinstance(raw, str) and raw.strip():
            for part in raw.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                cookies.append({"name": k.strip(), "value": v.strip(), "domain": "document.cookie"})
    except Exception:
        pass
    return cookies


def probe_session_cookie(port: int) -> Dict[str, Any]:
    """
    Probe CDP for session cookie.
    Returns {ok, session, cookie_header, preferred_web, page_url, cookie_names, error, method}
    """
    out: Dict[str, Any] = {
        "ok": False,
        "session": None,
        "cookie_header": "",
        "preferred_web": MC_HOST,
        "page_url": "",
        "cookie_names": [],
        "error": "",
        "method": "",
    }
    targets = cdp_targets(port)
    pages = [t for t in targets if t.get("type") == "page"]
    page = None
    for t in pages:
        url = (t.get("url") or "").lower()
        if "monkeycode" in url or "monkeycode-ai.com" in url or "monkeycode-ai.net" in url:
            page = t
            break
    if not page and pages:
        page = pages[0]
    out["page_url"] = str((page or {}).get("url") or "")[:200]

    candidates: List[Tuple[str, str]] = []
    if page and page.get("webSocketDebuggerUrl"):
        candidates.append(("page", str(page["webSocketDebuggerUrl"])))
    ver = cdp_version(port) or {}
    if ver.get("webSocketDebuggerUrl"):
        candidates.append(("browser", str(ver["webSocketDebuggerUrl"])))
    # any other page targets
    for t in pages:
        wu = t.get("webSocketDebuggerUrl")
        if wu and ("page", str(wu)) not in candidates:
            candidates.append(("page-other", str(wu)))

    if not candidates:
        out["error"] = "no cdp websocket"
        return out

    origin = f"http://127.0.0.1:{port}"
    all_cookies: List[dict] = []
    last_err = ""
    for method_tag, ws_url in candidates:
        # SimpleWS only speaks plain ws://
        if ws_url.startswith("wss://"):
            last_err = "wss not supported"
            continue
        if ws_url.startswith("ws://"):
            pass
        elif ws_url.startswith("http://"):
            ws_url = "ws://" + ws_url[len("http://") :]
        try:
            ws = SimpleWS(ws_url, origin=origin, timeout=5)
        except Exception as e:
            last_err = f"ws connect {method_tag}: {e}"
            continue
        try:
            part = _collect_cookies_via_ws(ws)
            if part:
                all_cookies.extend(part)
                out["method"] = method_tag
        except Exception as e:
            last_err = f"collect {method_tag}: {e}"
        finally:
            ws.close()

    # unique names for UI
    names = []
    seen = set()
    for c in all_cookies:
        n = str(c.get("name") or "")
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    out["cookie_names"] = names[:30]

    session_c = _pick_session_cookie(all_cookies)
    session = str((session_c or {}).get("value") or "").strip() if session_c else None
    cookie_header = build_cookie_header(all_cookies)
    preferred_web = preferred_web_from_cookies(all_cookies, out.get("page_url") or "")
    out["cookie_header"] = cookie_header
    out["preferred_web"] = preferred_web
    if session:
        out["ok"] = True
        out["session"] = session
        # if header missing session name, force-include
        if cookie_header and COOKIE_NAME not in cookie_header and session_c:
            name = str(session_c.get("name") or COOKIE_NAME)
            out["cookie_header"] = f"{name}={session}" + ("; " + cookie_header if cookie_header else "")
        elif not cookie_header and session:
            out["cookie_header"] = f"{COOKIE_NAME}={session}"
        return out

    out["error"] = last_err or ("no session cookie; saw: " + (",".join(names[:12]) if names else "(none)"))
    return out


def get_session_cookie(port: int) -> Optional[str]:
    return probe_session_cookie(port).get("session")


def _page_ws_url(port: int) -> Tuple[Optional[str], str]:
    """Pick a monkeycode page websocket URL for in-page JS."""
    targets = cdp_targets(port)
    pages = [t for t in targets if t.get("type") == "page"]
    page = None
    for t in pages:
        url = (t.get("url") or "").lower()
        if "monkeycode-ai.com" in url or "monkeycode-ai.net" in url or "monkeycode" in url:
            if not url.startswith("chrome://") and "devtools://" not in url:
                page = t
                break
    if not page:
        for t in pages:
            u = (t.get("url") or "")
            if u.startswith("http"):
                page = t
                break
    if not page:
        return None, ""
    return str(page.get("webSocketDebuggerUrl") or "") or None, str(page.get("url") or "")


def mint_via_browser_cdp(port: int) -> Dict[str, Any]:
    """
    Mint API key inside the logged-in Chrome page (fetch + credentials:include).
    This matches real browser requests and avoids Windows cookie-replay 401.
    """
    ws_url, page_url = _page_ws_url(port)
    out: Dict[str, Any] = {
        "ok": False,
        "error": "",
        "page_url": page_url,
        "user": {},
        "api_key": "",
        "signing_secret": "",
        "key_id": None,
        "raw": "",
    }
    if not ws_url:
        out["error"] = "no page websocket for in-browser mint"
        return out
    if ws_url.startswith("http://"):
        ws_url = "ws://" + ws_url[len("http://") :]
    if not ws_url.startswith("ws://"):
        out["error"] = f"unsupported ws url: {ws_url[:80]}"
        return out

    # relative URLs so we stay on whatever host the user logged into (.com/.net)
    js = r"""
(() => {
  const run = async () => {
    const optsGet = { method: 'GET', credentials: 'include', headers: { 'Accept': 'application/json' } };
    const st = await fetch('/api/v1/users/status', optsGet);
    const stText = await st.text();
    let stJson = null;
    try { stJson = JSON.parse(stText); } catch (e) { stJson = { raw: stText.slice(0, 300) }; }
    if (!st.ok) {
      return { ok: false, step: 'status', status: st.status, body: stJson, page: location.href };
    }
    const optsPost = {
      method: 'POST',
      credentials: 'include',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      body: '{}'
    };
    const cr = await fetch('/api/v1/users/ohmyagent/api-keys', optsPost);
    const crText = await cr.text();
    let crJson = null;
    try { crJson = JSON.parse(crText); } catch (e) { crJson = { raw: crText.slice(0, 300) }; }
    if (!cr.ok) {
      return { ok: false, step: 'api-keys', status: cr.status, body: crJson, page: location.href, user: (stJson && stJson.data && stJson.data.user) || null };
    }
    const data = (crJson && crJson.data) || {};
    return {
      ok: true,
      status: cr.status,
      page: location.href,
      user: (stJson && stJson.data && stJson.data.user) || {},
      api_key: data.api_key || '',
      signing_secret: data.signing_secret || '',
      key_id: data.id || null,
      body: crJson
    };
  };
  return run().then((r) => JSON.stringify(r)).catch((e) => JSON.stringify({ ok: false, error: String(e), page: location.href }));
})()
"""
    origin = f"http://127.0.0.1:{port}"
    try:
        ws = SimpleWS(ws_url, origin=origin, timeout=8)
    except Exception as e:
        out["error"] = f"ws connect: {e}"
        return out
    try:
        try:
            ws.call("Runtime.enable", timeout=3.0)
        except Exception:
            pass
        res = ws.call(
            "Runtime.evaluate",
            {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout=45.0,
        )
        val = (res.get("result") or {}).get("value")
        if isinstance(val, dict):
            payload = val
        else:
            try:
                payload = json.loads(val or "{}")
            except Exception:
                out["error"] = f"bad js result: {str(val)[:200]}"
                return out
        out["raw"] = json.dumps(payload, ensure_ascii=False)[:500]
        out["page_url"] = str(payload.get("page") or page_url or "")
        if not payload.get("ok"):
            body = payload.get("body")
            out["error"] = (
                f"in-page {payload.get('step') or 'mint'} "
                f"HTTP {payload.get('status')}: {json.dumps(body, ensure_ascii=False)[:180] if body is not None else payload.get('error')}"
            )
            return out
        api_key = str(payload.get("api_key") or "").strip()
        secret = str(payload.get("signing_secret") or "").strip()
        if not api_key or not secret:
            out["error"] = "in-page mint missing api_key/signing_secret: " + out["raw"][:200]
            return out
        out["ok"] = True
        out["api_key"] = api_key
        out["signing_secret"] = secret
        out["key_id"] = payload.get("key_id")
        out["user"] = payload.get("user") or {}
        return out
    except Exception as e:
        out["error"] = f"in-page mint exception: {e}"
        return out
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
    page_url: str = ""
    cookie_names: str = ""
    probe_count: int = 0
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
            "page_url": self.page_url,
            "cookie_names": self.cookie_names,
            "probe_count": self.probe_count,
        }


class ManualAuthManager:
    def __init__(
        self,
        work_root: Path,
        mint_callback: Callable[..., Dict[str, Any]],
        timeout: int = DEFAULT_TIMEOUT,
    ):
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

            self._set(
                job,
                "waiting_login",
                f"请在弹出的 Chrome 完成登录（打开 {MC_HOST}）。登录成功后不要关窗，等待自动检测…",
            )
            deadline = time.time() + self.timeout
            session = None
            cookie_header = ""
            preferred_web = MC_HOST
            last_detail = ""
            while time.time() < deadline:
                if job._stop.is_set():
                    self._set(job, "cancelled", "已取消")
                    return
                if job._proc and job._proc.poll() is not None:
                    # user closed chrome before login
                    self._set(job, "error", "Chrome 已关闭，未完成授权", error="chrome closed")
                    return
                try:
                    probe = probe_session_cookie(job.debug_port)
                except Exception as e:
                    log(f"cookie poll: {e}")
                    probe = {
                        "ok": False,
                        "session": None,
                        "cookie_header": "",
                        "preferred_web": MC_HOST,
                        "error": str(e),
                        "page_url": "",
                        "cookie_names": [],
                    }
                job.probe_count = int(job.probe_count or 0) + 1
                job.page_url = str(probe.get("page_url") or "")
                names = probe.get("cookie_names") or []
                if isinstance(names, list):
                    job.cookie_names = ",".join(str(x) for x in names[:15])
                session = probe.get("session") if probe.get("ok") else None
                cookie_header = str(probe.get("cookie_header") or "")
                preferred_web = str(probe.get("preferred_web") or MC_HOST)
                if session:
                    log(
                        f"session cookie found via {probe.get('method')} page={job.page_url} "
                        f"web={preferred_web} cookie_header_len={len(cookie_header)}"
                    )
                    break
                detail = str(probe.get("error") or "")
                # refresh UI every few probes
                if job.probe_count == 1 or job.probe_count % 3 == 0 or detail != last_detail:
                    last_detail = detail
                    page = job.page_url or "(未检测到页面)"
                    names_s = job.cookie_names or "(无cookie)"
                    self._set(
                        job,
                        "waiting_login",
                        f"等待登录中… 第{job.probe_count}次探测 | 页面: {page[:80]} | cookies: {names_s[:100]}",
                    )
                time.sleep(POLL_SEC)

            if not session:
                self._set(
                    job,
                    "timeout",
                    f"等待登录超时。最后页面: {job.page_url or '-'}；cookies: {job.cookie_names or '(无)'}。"
                    f"请确认已在弹窗 Chrome 登录成功，或改用下方 Session 签发。",
                    error="timeout",
                )
                return

            self._set(job, "minting", "已检测到登录，正在页面内签发凭证…")
            # Prefer in-page fetch (same as browser). Server-side cookie replay can 401 on Windows.
            browser_mint = mint_via_browser_cdp(job.debug_port)
            if browser_mint.get("ok"):
                log(
                    f"in-page mint ok page={browser_mint.get('page_url')} "
                    f"key={(browser_mint.get('api_key') or '')[:12]}…"
                )
                result = self.mint_callback(
                    {
                        "pre_minted": True,
                        "api_key": browser_mint.get("api_key"),
                        "signing_secret": browser_mint.get("signing_secret"),
                        "key_id": browser_mint.get("key_id"),
                        "user": browser_mint.get("user") or {},
                        "page_url": browser_mint.get("page_url") or job.page_url,
                        "preferred_web": preferred_web,
                    }
                )
            else:
                log(f"in-page mint failed: {browser_mint.get('error')}; fallback server mint")
                self._set(
                    job,
                    "minting",
                    f"页面内签发失败，尝试服务端重放… ({str(browser_mint.get('error') or '')[:80]})",
                )
                result = self.mint_callback(
                    {
                        "session": session,
                        "cookie_header": cookie_header or f"{COOKIE_NAME}={session}",
                        "preferred_web": preferred_web,
                        "page_url": job.page_url,
                    }
                )
            account = result.get("account") or {}
            user = result.get("user") or {}
            email = account.get("email") or user.get("email") or ""
            via = result.get("web") or result.get("mint_via") or ""
            self._set(
                job,
                "done",
                f"授权完成：{email or 'ok'}" + (f"（{via}）" if via else ""),
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
