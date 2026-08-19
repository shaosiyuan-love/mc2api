#!/usr/bin/env python3
"""mc2api: local gateway, account pool, client keys, scheduling, upstream bridge."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse
from chrome_auth import ManualAuthManager

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MC_CONSOLE_DATA", str(ROOT / "data")))
DB_PATH = DATA_DIR / "console.db"
PROXY_FILE = DATA_DIR / "proxy.json"
STATIC_DIR = ROOT / "static"
HOST = os.environ.get("MC_CONSOLE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MC_CONSOLE_PORT", "18095"))
UPSTREAM_DEFAULT = os.environ.get(
    "MC_CONSOLE_UPSTREAM", "https://proxy.monkeycode-ai.net/v1"
).rstrip("/")
# Token 来自哪个控制台域名，就固定走对应上游（不要每次 401 再猜）
UPSTREAM_COM = os.environ.get(
    "MC_CONSOLE_UPSTREAM_COM", "https://proxy.monkeycode-ai.com/v1"
).rstrip("/")
UPSTREAM_NET = os.environ.get(
    "MC_CONSOLE_UPSTREAM_NET", "https://proxy.monkeycode-ai.net/v1"
).rstrip("/")
DEFAULT_SYSTEM = os.environ.get("MC_CONSOLE_DEFAULT_SYSTEM", "You are a helpful assistant.")
ADMIN_TOKEN_ENV = os.environ.get("MC_CONSOLE_ADMIN_TOKEN", "").strip()
ADMIN_TOKEN_FILE = DATA_DIR / "admin_token.txt"
REQUEST_TIMEOUT = float(os.environ.get("MC_CONSOLE_TIMEOUT", "300"))
COOLDOWN_BASE = int(os.environ.get("MC_CONSOLE_COOLDOWN_BASE", "30"))
COOLDOWN_MAX = int(os.environ.get("MC_CONSOLE_COOLDOWN_MAX", "600"))
DEFAULT_MAX_CONCURRENT = int(os.environ.get("MC_CONSOLE_MAX_CONCURRENT", "3"))
STICKY_TTL = float(os.environ.get("MC_CONSOLE_STICKY_TTL", "1800"))  # seconds
CAPACITY_WAIT = float(os.environ.get("MC_CONSOLE_CAPACITY_WAIT", "30"))  # seconds; wait for lease free
USER_AGENT = os.environ.get("MC_CONSOLE_UA", "ohmyagent c49e56a")
# 控制台 Web（签发/手动授权）；默认 .com，兼容 .net
MC_WEB = (os.environ.get("MC_CONSOLE_WEB") or "https://monkeycode-ai.com").rstrip("/")
MC_WEB_FALLBACKS = [
    MC_WEB,
    "https://monkeycode-ai.com",
    "https://monkeycode-ai.net",
]
_mc_web_seen = set()
MC_WEB_HOSTS = [h for h in MC_WEB_FALLBACKS if not (h in _mc_web_seen or _mc_web_seen.add(h))]


def upstream_base_for_web(web_or_page: str = "") -> str:
    """Map mint/login domain → fixed upstream proxy. No per-request guessing."""
    s = (web_or_page or "").strip().lower()
    if "monkeycode-ai.com" in s:
        return UPSTREAM_COM
    if "monkeycode-ai.net" in s:
        return UPSTREAM_NET
    # bare hints
    if s.endswith(".com") or "/.com" in s:
        return UPSTREAM_COM
    if s.endswith(".net") or "/.net" in s:
        return UPSTREAM_NET
    return UPSTREAM_DEFAULT

MODEL_ALIASES = {
    "deepseek-v4-flash": "monkeycode-basic/deepseek-v4-flash",
    "deepseek-v4-pro": "monkeycode-pro/deepseek-v4-pro",
    "qwen3.5-plus": "monkeycode-basic/qwen3.5-plus",
    "qwen3.5": "monkeycode-basic/qwen3.5-plus",
    "glm-5.2": "monkeycode-pro/glm-5.2",
    "gpt-5.4": "monkeycode-ultra/gpt-5.4",
    "gpt-5.5": "monkeycode-ultra/gpt-5.5",
    "claude-haiku-4-5": "monkeycode-basic/deepseek-v4-flash",
    "claude-sonnet-4-6": "monkeycode-basic/deepseek-v4-flash",
    "claude-opus-4-6": "monkeycode-pro/deepseek-v4-pro",
    "claude-opus-4-7": "monkeycode-pro/deepseek-v4-pro",
}

_db_lock = threading.RLock()
_admin_token = ""
_manual_auth: ManualAuthManager | None = None
_proxy_lock = threading.RLock()
# cached egress: enabled + url + opener
_proxy_state: Dict[str, Any] = {
    "enabled": False,
    "url": "",
    "updated_at": 0.0,
    "opener": None,
    "sig": "",
}


_log_file_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[mc2api] {msg}"
    print(line, flush=True)
    # Always append to data/server.log so Windows start.bat also has logs
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _log_file_lock:
            with open(DATA_DIR / "server.log", "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
    except Exception:
        pass


def log_block(title: str, text: str) -> None:
    """Multi-line dump into server.log (and stdout)."""
    bar = "=" * 72
    block = f"[mc2api] {bar}\n[mc2api] {title}\n{text.rstrip()}\n[mc2api] {bar}"
    print(block, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _log_file_lock:
            with open(DATA_DIR / "server.log", "a", encoding="utf-8") as fp:
                fp.write(block + "\n")
    except Exception:
        pass


def now() -> float:
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or now()))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        conn = db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  label TEXT NOT NULL DEFAULT '',
                  email TEXT NOT NULL DEFAULT '',
                  api_key TEXT NOT NULL UNIQUE,
                  signing_secret TEXT NOT NULL,
                  base_url TEXT NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  weight INTEGER NOT NULL DEFAULT 1,
                  fail_count INTEGER NOT NULL DEFAULT 0,
                  cooldown_until REAL NOT NULL DEFAULT 0,
                  last_error TEXT NOT NULL DEFAULT '',
                  last_used_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_keys (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL DEFAULT '',
                  token TEXT NOT NULL UNIQUE,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  request_count INTEGER NOT NULL DEFAULT 0,
                  last_used_at REAL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  created_at REAL NOT NULL,
                  client_key_id INTEGER,
                  account_id INTEGER,
                  path TEXT NOT NULL,
                  model TEXT NOT NULL DEFAULT '',
                  status INTEGER NOT NULL,
                  latency_ms INTEGER NOT NULL,
                  error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_req_created ON request_logs(created_at DESC);
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
            if "max_concurrent" not in cols:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN max_concurrent INTEGER NOT NULL DEFAULT %d"
                    % DEFAULT_MAX_CONCURRENT
                )
            if "priority" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        finally:
            conn.close()


def ensure_admin_token() -> str:
    global _admin_token
    if ADMIN_TOKEN_ENV:
        _admin_token = ADMIN_TOKEN_ENV
        return _admin_token
    if ADMIN_TOKEN_FILE.exists():
        tok = ADMIN_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            _admin_token = tok
            return _admin_token
    tok = "mcadm_" + secrets.token_urlsafe(24)
    ADMIN_TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(ADMIN_TOKEN_FILE, 0o600)
    except Exception:
        pass
    _admin_token = tok
    log(f"generated admin token -> {ADMIN_TOKEN_FILE}")
    return _admin_token


def mask_proxy_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        p = urlparse(url)
    except Exception:
        return url
    if not p.password and not (p.username and "@" in (p.netloc or "")):
        return url
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    user = p.username or ""
    auth = f"{user}:***@" if user else "***@"
    netloc = auth + host
    return urlunparse((p.scheme, netloc, p.path or "", "", p.query or "", ""))


def validate_proxy_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("代理 URL 不能为空")
    p = urlparse(url)
    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https", "socks5", "socks5h", "socks4", "socks4a"):
        raise ValueError("代理协议仅支持 http / https / socks5 / socks4")
    if not p.hostname:
        raise ValueError("代理 URL 缺少主机名")
    return url


def _build_socks_opener(proxy_url: str) -> urllib.request.OpenerDirector:
    """SOCKS opener via PySocks (optional dependency)."""
    try:
        import http.client
        import socks  # type: ignore
    except ImportError as e:
        raise RuntimeError("SOCKS 代理需要 PySocks：pip install PySocks") from e

    p = urlparse(proxy_url)
    scheme = (p.scheme or "").lower()
    stype = {
        "socks5": socks.SOCKS5,
        "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "socks4a": socks.SOCKS4,
    }.get(scheme)
    if stype is None:
        raise ValueError("unsupported socks scheme")
    proxy_host = p.hostname or "127.0.0.1"
    proxy_port = int(p.port or 1080)
    # socks5h / socks4a: remote DNS; plain socks5 also remote DNS (safer default)
    rdns = scheme in ("socks5", "socks5h", "socks4a")
    username = p.username
    password = p.password

    class SockHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:  # noqa: ANN201
            self.sock = socks.create_connection(
                (self.host, self.port),
                timeout=self.timeout,
                proxy_type=stype,
                proxy_addr=proxy_host,
                proxy_port=proxy_port,
                proxy_username=username,
                proxy_password=password,
                proxy_rdns=rdns,
            )

    class SockHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:  # noqa: ANN201
            self.sock = socks.create_connection(
                (self.host, self.port),
                timeout=self.timeout,
                proxy_type=stype,
                proxy_addr=proxy_host,
                proxy_port=proxy_port,
                proxy_username=username,
                proxy_password=password,
                proxy_rdns=rdns,
            )
            if getattr(self, "_tunnel_host", None):
                self._tunnel()
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    class SockHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):  # noqa: ANN001, ANN201
            return self.do_open(SockHTTPConnection, req)

    class SockHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001, ANN201
            return self.do_open(SockHTTPSConnection, req)

    return urllib.request.build_opener(SockHTTPHandler, SockHTTPSHandler)


def _build_proxy_opener(proxy_url: str) -> urllib.request.OpenerDirector:
    proxy_url = validate_proxy_url(proxy_url)
    p = urlparse(proxy_url)
    scheme = (p.scheme or "").lower()
    if scheme in ("socks5", "socks5h", "socks4", "socks4a"):
        return _build_socks_opener(proxy_url)
    # HTTP(S) forward proxy for both http and https targets
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )


def _read_proxy_file() -> Dict[str, Any]:
    if not PROXY_FILE.exists():
        return {"enabled": False, "url": "", "updated_at": 0.0}
    try:
        raw = json.loads(PROXY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False, "url": "", "updated_at": 0.0}
    if not isinstance(raw, dict):
        return {"enabled": False, "url": "", "updated_at": 0.0}
    return {
        "enabled": bool(raw.get("enabled")),
        "url": str(raw.get("url") or "").strip(),
        "updated_at": float(raw.get("updated_at") or 0.0),
    }


def _sync_proxy_state(force: bool = False) -> Dict[str, Any]:
    """Load proxy.json into memory opener. Thread-safe."""
    with _proxy_lock:
        disk = _read_proxy_file()
        enabled = bool(disk.get("enabled")) and bool(disk.get("url"))
        url = str(disk.get("url") or "").strip() if enabled else ""
        sig = f"{int(enabled)}|{url}"
        if not force and sig == _proxy_state.get("sig") and _proxy_state.get("opener") is not None:
            return dict(_proxy_state)
        opener: Any = None
        if enabled and url:
            opener = _build_proxy_opener(url)
        else:
            opener = urllib.request.build_opener()
        _proxy_state.update({
            "enabled": enabled,
            "url": url if enabled else str(disk.get("url") or "").strip(),
            "updated_at": float(disk.get("updated_at") or 0.0),
            "opener": opener,
            "sig": sig,
        })
        return dict(_proxy_state)


def get_proxy_config(public: bool = True) -> Dict[str, Any]:
    st = _sync_proxy_state()
    url = str(st.get("url") or "")
    enabled = bool(st.get("enabled")) and bool(url)
    out = {
        "enabled": enabled,
        "url": url,
        "url_masked": mask_proxy_url(url),
        "mode": "proxy" if enabled else "direct",
        "updated_at": st.get("updated_at") or 0.0,
    }
    if public:
        # local admin: keep full url for edit, also expose masked
        pass
    return out


def save_proxy_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    enabled = bool(payload.get("enabled"))
    url = str(payload.get("url") or "").strip()
    # if UI sent masked password marker, keep previous secret
    if "***" in url:
        prev = _read_proxy_file()
        prev_url = str(prev.get("url") or "")
        if prev_url and mask_proxy_url(prev_url) == url:
            url = prev_url
        elif "***" in url:
            raise ValueError("代理 URL 含掩码，请重新填写完整地址（含密码）")
    if enabled:
        url = validate_proxy_url(url)
        # fail fast if opener cannot be built
        _build_proxy_opener(url)
    elif url:
        # allow storing disabled draft; still validate if non-empty
        validate_proxy_url(url)
    data = {
        "enabled": enabled,
        "url": url,
        "updated_at": now(),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROXY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PROXY_FILE)
    try:
        os.chmod(PROXY_FILE, 0o600)
    except Exception:
        pass
    _sync_proxy_state(force=True)
    log(f"proxy config saved enabled={enabled} url={mask_proxy_url(url) or '(empty)'}")
    return get_proxy_config()


def http_open(req: urllib.request.Request, timeout: float = 30):
    """Outbound HTTP via configured egress proxy (hot-reloaded)."""
    st = _sync_proxy_state()
    opener = st.get("opener") or urllib.request.build_opener()
    return opener.open(req, timeout=timeout)


def _opener_for_probe(enabled: Optional[bool] = None, url: Optional[str] = None) -> Tuple[Any, Dict[str, Any]]:
    """Build opener for probe; None fields fall back to saved config."""
    saved = get_proxy_config()
    use_enabled = saved.get("enabled") if enabled is None else bool(enabled)
    use_url = saved.get("url") if url is None else str(url or "").strip()
    if "***" in (use_url or ""):
        prev = str(saved.get("url") or "")
        if prev and mask_proxy_url(prev) == use_url:
            use_url = prev
    info = {
        "enabled": bool(use_enabled) and bool(use_url),
        "url": use_url if (use_enabled and use_url) else "",
        "url_masked": mask_proxy_url(use_url) if use_url else "",
        "mode": "proxy" if (use_enabled and use_url) else "direct",
    }
    if info["enabled"]:
        return _build_proxy_opener(str(info["url"])), info
    return urllib.request.build_opener(), info


def test_proxy_egress(enabled: Optional[bool] = None, url: Optional[str] = None) -> Dict[str, Any]:
    """Probe egress: optional public IP + upstream host reachability."""
    opener, info = _opener_for_probe(enabled=enabled, url=url)
    result: Dict[str, Any] = {
        "ok": False,
        "mode": info.get("mode"),
        "proxy_url_masked": info.get("url_masked") or "",
        "egress_ip": None,
        "upstream_ok": False,
        "upstream_status": None,
        "latency_ms": None,
        "error": "",
    }
    t0 = now()
    try:
        # 1) egress IP (best-effort)
        try:
            req_ip = urllib.request.Request(
                "https://api.ipify.org?format=json",
                headers={"User-Agent": "mc2api/1.0", "Accept": "application/json"},
                method="GET",
            )
            with opener.open(req_ip, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
                result["egress_ip"] = body.get("ip") or None
        except Exception as e:
            result["egress_ip_error"] = str(e)[:200]

        # 2) upstream base reachability (any HTTP response counts as path OK)
        up = UPSTREAM_DEFAULT.rstrip("/")
        probe = up.rsplit("/v1", 1)[0] if up.endswith("/v1") else up
        req_up = urllib.request.Request(
            probe + "/",
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            method="GET",
        )
        try:
            with opener.open(req_up, timeout=20) as resp:
                result["upstream_status"] = int(getattr(resp, "status", 200) or 200)
                result["upstream_ok"] = True
        except urllib.error.HTTPError as e:
            result["upstream_status"] = int(e.code)
            result["upstream_ok"] = True
        result["latency_ms"] = int((now() - t0) * 1000)
        result["ok"] = True
        return result
    except Exception as e:
        result["latency_ms"] = int((now() - t0) * 1000)
        result["error"] = str(e)[:400]
        result["ok"] = False
        return result


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def sign_system(secret: str, system_text: str) -> str:
    dig = hmac.new(secret.encode("utf-8"), system_text.encode("utf-8"), hashlib.sha256).hexdigest()
    return "v1=" + dig


def normalize_model(model: str) -> str:
    m = (model or "").strip()
    if not m:
        return MODEL_ALIASES["deepseek-v4-flash"]
    if m in MODEL_ALIASES:
        return MODEL_ALIASES[m]
    short = m.split("/")[-1]
    if short in MODEL_ALIASES:
        return MODEL_ALIASES[short]
    return m


def extract_system_sign_text(body: Dict[str, Any]) -> str:
    system = body.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text") or "")
            if isinstance(block, str):
                return block
    return ""


def ensure_system(body: Dict[str, Any]) -> str:
    text = extract_system_sign_text(body)
    if text.strip():
        return text
    body["system"] = DEFAULT_SYSTEM
    return DEFAULT_SYSTEM


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                if c.get("type") in ("text", "input_text") and c.get("text") is not None:
                    parts.append(str(c.get("text") or ""))
                elif isinstance(c.get("text"), str):
                    parts.append(c["text"])
        return "\n".join(parts)
    return str(content)


def openai_to_anthropic(body: Dict[str, Any]) -> Dict[str, Any]:
    messages_in = body.get("messages") or []
    system_parts: List[str] = []
    messages: List[Dict[str, Any]] = []
    for m in messages_in:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user").lower()
        text = content_to_text(m.get("content")).strip()
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if not text and role != "assistant":
            continue
        if role == "tool":
            role = "user"
            text = "[tool]\n" + text
        if role not in ("user", "assistant"):
            role = "user"
        if messages and messages[-1]["role"] == role:
            prev = messages[-1]["content"]
            if isinstance(prev, list) and prev and isinstance(prev[0], dict):
                prev[0]["text"] = str(prev[0].get("text") or "") + "\n" + text
            else:
                messages[-1]["content"] = f"{prev}\n{text}"
        else:
            messages.append({"role": role, "content": [{"type": "text", "text": text}]})
    if not messages:
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    if messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens") or 4096
    try:
        max_tokens = int(max_tokens)
    except Exception:
        max_tokens = 4096
    out: Dict[str, Any] = {
        "model": normalize_model(str(body.get("model") or "")),
        "max_tokens": max(1, max_tokens),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    out["system"] = "\n\n".join(p for p in system_parts if p).strip() or DEFAULT_SYSTEM
    if body.get("temperature") is not None:
        out["temperature"] = body.get("temperature")
    if body.get("tools"):
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                tools.append(
                    {
                        "name": fn.get("name"),
                        "description": fn.get("description") or "",
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                    }
                )
            elif t.get("name"):
                tools.append(t)
        if tools:
            out["tools"] = tools
    return out


def anthropic_text(msg: Dict[str, Any]) -> str:
    parts = []
    for b in msg.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text") or ""))
    return "".join(parts)


def anthropic_to_openai(msg: Dict[str, Any], model: str) -> Dict[str, Any]:
    usage = msg.get("usage") or {}
    pt = int(usage.get("input_tokens") or 0)
    ct = int(usage.get("output_tokens") or 0)
    stop = msg.get("stop_reason") or "stop"
    finish = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(stop, "stop")
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(now()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": anthropic_text(msg)},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


# ---------- pool / keys ----------

def list_accounts(include_secret: bool = False) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = db()
        try:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id DESC").fetchall()
        finally:
            conn.close()
    out = []
    t = now()
    for r in rows:
        d = row_to_dict(r) or {}
        d["enabled"] = bool(d.get("enabled"))
        d["in_cooldown"] = float(d.get("cooldown_until") or 0) > t
        d["api_key_preview"] = (d.get("api_key") or "")[:10] + "…"
        d["max_concurrent"] = int(d.get("max_concurrent") or DEFAULT_MAX_CONCURRENT)
        d["priority"] = int(d.get("priority") or 0)
        d["in_flight"] = _scheduler.inflight(int(d["id"]))
        if not include_secret:
            d.pop("signing_secret", None)
            d["api_key"] = d["api_key_preview"]
        out.append(d)
    return out


def get_account(account_id: int) -> Optional[Dict[str, Any]]:
    with _db_lock:
        conn = db()
        try:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        finally:
            conn.close()
    return row_to_dict(row)


def upsert_account(payload: Dict[str, Any]) -> Dict[str, Any]:
    api_key = str(payload.get("api_key") or "").strip()
    secret = str(payload.get("signing_secret") or "").strip()
    if not api_key or not secret:
        raise ValueError("api_key and signing_secret required")
    label = str(payload.get("label") or payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip()
    base_url = str(payload.get("base_url") or UPSTREAM_DEFAULT).rstrip("/")
    enabled = 1 if payload.get("enabled", True) else 0
    weight = max(1, int(payload.get("weight") or 1))
    priority = int(payload.get("priority") or 0)
    max_concurrent = max(1, int(payload.get("max_concurrent") or DEFAULT_MAX_CONCURRENT))
    t = now()
    with _db_lock:
        conn = db()
        try:
            existing = conn.execute("SELECT id FROM accounts WHERE api_key=?", (api_key,)).fetchone()
            if existing:
                conn.execute(
                    """UPDATE accounts SET label=?, email=?, signing_secret=?, base_url=?,
                       enabled=?, weight=?, priority=?, max_concurrent=?, updated_at=? WHERE id=?""",
                    (label, email, secret, base_url, enabled, weight, priority, max_concurrent, t, existing["id"]),
                )
                aid = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO accounts(label,email,api_key,signing_secret,base_url,enabled,weight,
                       priority,max_concurrent,fail_count,cooldown_until,last_error,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,0,0,'',?,?)""",
                    (label, email, api_key, secret, base_url, enabled, weight, priority, max_concurrent, t, t),
                )
                aid = cur.lastrowid
            conn.commit()
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        finally:
            conn.close()
    d = row_to_dict(row) or {}
    d["enabled"] = bool(d.get("enabled"))
    return d


def patch_account(account_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = []
    vals: List[Any] = []
    for key in ("label", "email", "signing_secret", "base_url"):
        if key in payload:
            fields.append(f"{key}=?")
            vals.append(str(payload[key]))
    if "enabled" in payload:
        fields.append("enabled=?")
        vals.append(1 if payload["enabled"] else 0)
    if "weight" in payload:
        fields.append("weight=?")
        vals.append(max(1, int(payload["weight"])))
    if "priority" in payload:
        fields.append("priority=?")
        vals.append(int(payload["priority"]))
    if "max_concurrent" in payload:
        fields.append("max_concurrent=?")
        vals.append(max(1, int(payload["max_concurrent"])))
    if "reset_cooldown" in payload and payload["reset_cooldown"]:
        fields.extend(["cooldown_until=?", "fail_count=?", "last_error=?"])
        vals.extend([0, 0, ""])
    if not fields:
        raise ValueError("no fields to update")
    fields.append("updated_at=?")
    vals.append(now())
    vals.append(account_id)
    with _db_lock:
        conn = db()
        try:
            conn.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id=?", vals)
            conn.commit()
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        finally:
            conn.close()
    if not row:
        raise KeyError("account not found")
    d = row_to_dict(row) or {}
    d["enabled"] = bool(d.get("enabled"))
    return d


def delete_account(account_id: int) -> None:
    with _db_lock:
        conn = db()
        try:
            conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            conn.commit()
        finally:
            conn.close()


def mark_account_success(account_id: int) -> None:
    with _db_lock:
        conn = db()
        try:
            conn.execute(
                """UPDATE accounts SET fail_count=0, cooldown_until=0, last_error='',
                   last_used_at=?, updated_at=? WHERE id=?""",
                (now(), now(), account_id),
            )
            conn.commit()
        finally:
            conn.close()


def mark_account_failure(account_id: int, error: str, status: int = 0) -> None:
    with _db_lock:
        conn = db()
        try:
            row = conn.execute("SELECT fail_count FROM accounts WHERE id=?", (account_id,)).fetchone()
            fc = int((row["fail_count"] if row else 0) or 0) + 1
            # only cooldown on hard failures
            cool = 0.0
            if status in (401, 402, 403, 429) or status >= 500 or status == 0:
                cool = now() + min(COOLDOWN_MAX, COOLDOWN_BASE * (2 ** min(fc - 1, 4)))
            conn.execute(
                """UPDATE accounts SET fail_count=?, cooldown_until=?, last_error=?,
                   last_used_at=?, updated_at=? WHERE id=?""",
                (fc, cool, (error or "")[:500], now(), now(), account_id),
            )
            conn.commit()
        finally:
            conn.close()
    # grok2api-like: drop sticky bindings for auth/rate failures
    if status in (401, 402, 403, 429):
        _scheduler.sticky_clear_account(int(account_id))


class AccountLease:
    """Per-request lease holding an upstream account concurrency slot."""

    def __init__(self, account: Dict[str, Any], release_fn: Any):
        self.account = account
        self._release_fn = release_fn
        self._released = False

    @property
    def id(self) -> int:
        return int(self.account["id"])

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._release_fn()
        except Exception:
            pass


class AccountScheduler:
    """grok2api-inspired scheduler: concurrency lease + load score + sticky session."""

    def __init__(self) -> None:
        self._cv = threading.Condition(threading.Lock())
        self._inflight: Dict[int, int] = {}
        self._last_selected: Dict[int, float] = {}
        # affinity_key -> (account_id, expires_at)
        self._sticky: Dict[str, Tuple[int, float]] = {}

    def inflight(self, account_id: int) -> int:
        with self._cv:
            return int(self._inflight.get(int(account_id), 0))

    def _limit_of(self, account: Dict[str, Any]) -> int:
        try:
            return max(1, int(account.get("max_concurrent") or DEFAULT_MAX_CONCURRENT))
        except Exception:
            return DEFAULT_MAX_CONCURRENT

    def _try_claim_locked(self, account: Dict[str, Any]) -> Optional[AccountLease]:
        aid = int(account["id"])
        limit = self._limit_of(account)
        cur = int(self._inflight.get(aid, 0))
        if cur >= limit:
            return None
        self._inflight[aid] = cur + 1
        self._last_selected[aid] = now()

        def _release() -> None:
            with self._cv:
                left = int(self._inflight.get(aid, 1)) - 1
                if left <= 0:
                    self._inflight.pop(aid, None)
                else:
                    self._inflight[aid] = left
                self._cv.notify_all()

        return AccountLease(account, _release)

    def sticky_get(self, key: str) -> Optional[int]:
        key = (key or "").strip()
        if not key:
            return None
        with self._cv:
            item = self._sticky.get(key)
            if not item:
                return None
            aid, exp = item
            if exp <= now():
                self._sticky.pop(key, None)
                return None
            # refresh TTL on hit
            self._sticky[key] = (aid, now() + STICKY_TTL)
            return int(aid)

    def sticky_bind(self, key: str, account_id: int) -> None:
        key = (key or "").strip()
        if not key:
            return
        with self._cv:
            self._sticky[key] = (int(account_id), now() + STICKY_TTL)

    def sticky_clear_account(self, account_id: int) -> None:
        aid = int(account_id)
        with self._cv:
            dead = [k for k, (v, _) in self._sticky.items() if int(v) == aid]
            for k in dead:
                self._sticky.pop(k, None)

    def _load_candidates(self) -> List[Dict[str, Any]]:
        t = now()
        with _db_lock:
            conn = db()
            try:
                rows = conn.execute(
                    """SELECT * FROM accounts
                       WHERE enabled=1 AND cooldown_until<=?
                       ORDER BY id ASC""",
                    (t,),
                ).fetchall()
            finally:
                conn.close()
        return [row_to_dict(r) or {} for r in rows]

    def _score_key(self, account: Dict[str, Any]) -> Tuple[Any, ...]:
        aid = int(account["id"])
        with self._cv:
            inflight = int(self._inflight.get(aid, 0))
            last_sel = float(self._last_selected.get(aid, 0.0))
        priority = int(account.get("priority") or 0)
        weight = max(1, int(account.get("weight") or 1))
        # lower tuple wins: low load -> high priority/weight -> least recently selected -> stable id
        return (inflight, -priority, -weight, last_sel, aid)

    def acquire(self, affinity_key: str = "", excluded: Optional[set] = None) -> AccountLease:
        excluded = excluded or set()
        affinity_key = (affinity_key or "").strip()
        deadline = now() + max(0.0, CAPACITY_WAIT)

        while True:
            candidates = [c for c in self._load_candidates() if int(c.get("id") or 0) not in excluded]
            if not candidates:
                # distinguish empty vs all cooling
                with _db_lock:
                    conn = db()
                    try:
                        total = conn.execute("SELECT COUNT(*) c FROM accounts WHERE enabled=1").fetchone()["c"]
                        cooling = conn.execute(
                            "SELECT COUNT(*) c FROM accounts WHERE enabled=1 AND cooldown_until>?",
                            (now(),),
                        ).fetchone()["c"]
                    finally:
                        conn.close()
                if total == 0:
                    raise RuntimeError("号池为空")
                if cooling >= total:
                    raise RuntimeError("可用上游账号正在冷却")
                raise RuntimeError("没有可用上游账号")

            by_id = {int(c["id"]): c for c in candidates}

            # 1) sticky prefer
            sticky_id = self.sticky_get(affinity_key) if affinity_key else None
            sticky_available = bool(sticky_id and sticky_id in by_id)
            ordered: List[Dict[str, Any]] = []
            if sticky_available:
                ordered.append(by_id[sticky_id])
                rest = [c for c in candidates if int(c["id"]) != sticky_id]
            else:
                # sticky missing / cooling / disabled / excluded → pick by score; rebind on claim
                rest = list(candidates)
            rest.sort(key=self._score_key)
            ordered.extend(rest)

            # 2) try claim in order
            saturated = 0
            for acc in ordered:
                with self._cv:
                    lease = self._try_claim_locked(acc)
                if lease is None:
                    saturated += 1
                    continue
                # bind sticky on successful claim
                if affinity_key:
                    if sticky_available and sticky_id != lease.id:
                        # temporary concurrent borrow only — keep sticky owner
                        pass
                    else:
                        # empty sticky, sticky hit, or sticky unavailable → bind/rebind
                        self.sticky_bind(affinity_key, lease.id)
                return lease

            # all saturated
            if CAPACITY_WAIT <= 0:
                raise RuntimeError("可用上游账号均达到并发上限")
            remaining = deadline - now()
            if remaining <= 0:
                raise RuntimeError("可用上游账号均达到并发上限")
            with self._cv:
                self._cv.wait(timeout=min(0.15, remaining))


_scheduler = AccountScheduler()


def acquire_account(affinity_key: str = "", excluded: Optional[set] = None) -> AccountLease:
    return _scheduler.acquire(affinity_key=affinity_key, excluded=excluded)


def list_client_keys() -> List[Dict[str, Any]]:
    with _db_lock:
        conn = db()
        try:
            rows = conn.execute("SELECT * FROM client_keys ORDER BY id DESC").fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = row_to_dict(r) or {}
        d["enabled"] = bool(d.get("enabled"))
        d["token_preview"] = (d.get("token") or "")[:12] + "…"
        out.append(d)
    return out


def create_client_key(name: str = "") -> Dict[str, Any]:
    token = "sk-mc-" + secrets.token_urlsafe(32)
    t = now()
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute(
                """INSERT INTO client_keys(name, token, enabled, request_count, created_at)
                   VALUES(?,?,1,0,?)""",
                (name or "key", token, t),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM client_keys WHERE id=?", (cur.lastrowid,)).fetchone()
        finally:
            conn.close()
    d = row_to_dict(row) or {}
    d["enabled"] = True
    return d


def patch_client_key(key_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    fields, vals = [], []
    if "name" in payload:
        fields.append("name=?"); vals.append(str(payload["name"]))
    if "enabled" in payload:
        fields.append("enabled=?"); vals.append(1 if payload["enabled"] else 0)
    if not fields:
        raise ValueError("no fields")
    vals.append(key_id)
    with _db_lock:
        conn = db()
        try:
            conn.execute(f"UPDATE client_keys SET {', '.join(fields)} WHERE id=?", vals)
            conn.commit()
            row = conn.execute("SELECT * FROM client_keys WHERE id=?", (key_id,)).fetchone()
        finally:
            conn.close()
    if not row:
        raise KeyError("key not found")
    d = row_to_dict(row) or {}
    d["enabled"] = bool(d.get("enabled"))
    return d


def delete_client_key(key_id: int) -> None:
    with _db_lock:
        conn = db()
        try:
            conn.execute("DELETE FROM client_keys WHERE id=?", (key_id,))
            conn.commit()
        finally:
            conn.close()


def auth_client_key(token: Optional[str]) -> Dict[str, Any]:
    if not token:
        raise PermissionError("missing api key")
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    with _db_lock:
        conn = db()
        try:
            row = conn.execute(
                "SELECT * FROM client_keys WHERE token=? AND enabled=1", (token,)
            ).fetchone()
            if not row:
                raise PermissionError("invalid api key")
            conn.execute(
                "UPDATE client_keys SET request_count=request_count+1, last_used_at=? WHERE id=?",
                (now(), row["id"]),
            )
            conn.commit()
            d = row_to_dict(row) or {}
        finally:
            conn.close()
    return d


def log_request(client_key_id: Optional[int], account_id: Optional[int], path: str,
                model: str, status: int, latency_ms: int, error: str = "") -> None:
    with _db_lock:
        conn = db()
        try:
            conn.execute(
                """INSERT INTO request_logs(created_at, client_key_id, account_id, path, model, status, latency_ms, error)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (now(), client_key_id, account_id, path, model, status, latency_ms, (error or "")[:500]),
            )
            # keep last 2000
            conn.execute(
                "DELETE FROM request_logs WHERE id NOT IN (SELECT id FROM request_logs ORDER BY id DESC LIMIT 2000)"
            )
            conn.commit()
        finally:
            conn.close()


def stats() -> Dict[str, Any]:
    t = now()
    with _db_lock:
        conn = db()
        try:
            acc_total = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
            acc_en = conn.execute("SELECT COUNT(*) c FROM accounts WHERE enabled=1").fetchone()["c"]
            acc_cool = conn.execute(
                "SELECT COUNT(*) c FROM accounts WHERE enabled=1 AND cooldown_until>?", (t,)
            ).fetchone()["c"]
            keys = conn.execute("SELECT COUNT(*) c FROM client_keys WHERE enabled=1").fetchone()["c"]
            last24 = conn.execute(
                "SELECT COUNT(*) c FROM request_logs WHERE created_at>=?", (t - 86400,)
            ).fetchone()["c"]
            err24 = conn.execute(
                "SELECT COUNT(*) c FROM request_logs WHERE created_at>=? AND status>=400",
                (t - 86400,),
            ).fetchone()["c"]
            recent = conn.execute(
                """SELECT r.*, a.email AS account_email, a.label AS account_label, k.name AS key_name
                   FROM request_logs r
                   LEFT JOIN accounts a ON a.id=r.account_id
                   LEFT JOIN client_keys k ON k.id=r.client_key_id
                   ORDER BY r.id DESC LIMIT 30"""
            ).fetchall()
        finally:
            conn.close()
    return {
        "accounts_total": acc_total,
        "accounts_enabled": acc_en,
        "accounts_cooldown": acc_cool,
        "client_keys_enabled": keys,
        "requests_24h": last24,
        "errors_24h": err24,
        "inflight_total": sum(_scheduler._inflight.values()) if hasattr(_scheduler, "_inflight") else 0,
        "sticky_sessions": len(getattr(_scheduler, "_sticky", {}) or {}),
        "recent": [row_to_dict(r) for r in recent],
    }


def _normalize_mint_payload(session_or_payload: Any, **kwargs: Any) -> Dict[str, str]:
    """Accept plain session string or rich payload from manual auth."""
    session = ""
    cookie_header = ""
    preferred_web = ""
    page_url = ""
    if isinstance(session_or_payload, dict):
        session = str(session_or_payload.get("session") or "").strip()
        cookie_header = str(session_or_payload.get("cookie_header") or "").strip()
        preferred_web = str(session_or_payload.get("preferred_web") or "").strip()
        page_url = str(session_or_payload.get("page_url") or "").strip()
    else:
        session = str(session_or_payload or "").strip()
        cookie_header = str(kwargs.get("cookie_header") or "").strip()
        preferred_web = str(kwargs.get("preferred_web") or "").strip()
        page_url = str(kwargs.get("page_url") or "").strip()

    if session.lower().startswith("monkeycode_ai_session="):
        # full cookie line pasted
        if not cookie_header:
            cookie_header = session
        session = session.split("=", 1)[1].strip()
    # pasted multi-cookie string
    if "monkeycode_ai_session=" in session and ";" in session:
        cookie_header = session
        for part in session.split(";"):
            part = part.strip()
            if part.lower().startswith("monkeycode_ai_session="):
                session = part.split("=", 1)[1].strip()
                break
    if not session and cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.lower().startswith("monkeycode_ai_session="):
                session = part.split("=", 1)[1].strip()
                break
    if not cookie_header and session:
        cookie_header = "monkeycode_ai_session=" + session
    elif cookie_header and session and "monkeycode_ai_session=" not in cookie_header.lower():
        cookie_header = "monkeycode_ai_session=" + session + "; " + cookie_header
    return {
        "session": session,
        "cookie_header": cookie_header,
        "preferred_web": preferred_web.rstrip("/"),
        "page_url": page_url,
    }


def _http_json(method: str, url: str, headers: Dict[str, str], data: Optional[bytes] = None) -> Tuple[int, Any, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with http_open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if hasattr(e, "read") else str(e)
        code = int(e.code)
    except Exception as e:
        return 0, None, str(e)
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {"raw": raw[:500]}
    return code, obj, raw[:500]


def mint_from_session(session_or_payload: Any = "", **kwargs: Any) -> Dict[str, Any]:
    # Manual auth may already mint inside Chrome page (Windows-safe path)
    if isinstance(session_or_payload, dict) and session_or_payload.get("pre_minted"):
        api_key = str(session_or_payload.get("api_key") or "").strip()
        signing_secret = str(session_or_payload.get("signing_secret") or "").strip()
        if not api_key or not signing_secret:
            raise ValueError("pre_minted payload missing api_key/signing_secret")
        user = session_or_payload.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        # Correct upstream by mint domain (page_url / preferred_web), not default-only
        origin_hint = (
            str(session_or_payload.get("page_url") or "")
            or str(session_or_payload.get("preferred_web") or "")
            or str(session_or_payload.get("web") or "")
        )
        base_url = upstream_base_for_web(origin_hint)
        account = upsert_account(
            {
                "label": user.get("name") or "",
                "email": user.get("email") or "",
                "api_key": api_key,
                "signing_secret": signing_secret,
                "base_url": base_url,
                "enabled": True,
            }
        )
        log(
            f"mint upsert pre_minted email={account.get('email') or ''} id={account.get('id')} "
            f"origin={origin_hint!r} base_url={base_url}"
        )
        return {
            "user": user,
            "key_id": session_or_payload.get("key_id"),
            "account": account,
            "web": origin_hint or "in-page",
            "mint_via": "browser-cdp",
            "base_url": base_url,
        }

    p = _normalize_mint_payload(session_or_payload, **kwargs)
    session = p["session"]
    cookie_header = p["cookie_header"]
    preferred_web = p["preferred_web"]
    page_url = p.get("page_url") or ""
    if not session and not cookie_header:
        raise ValueError("session empty")

    # host order: preferred first
    hosts: List[str] = []
    if preferred_web:
        hosts.append(preferred_web)
    for h in MC_WEB_HOSTS:
        if h not in hosts:
            hosts.append(h)

    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    last_err: Optional[str] = None
    for web in hosts:
        headers = {
            "Cookie": cookie_header,
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": web,
            "Referer": web + "/console/tasks",
        }
        try:
            code1, status, raw1 = _http_json("GET", web + "/api/v1/users/status", headers)
            if code1 == 401 or code1 == 403:
                raise RuntimeError(f"status {code1}: {(raw1 or '')[:180]}")
            if code1 == 0:
                raise RuntimeError(raw1 or "status request failed")
            if code1 >= 400:
                raise RuntimeError(f"status HTTP {code1}: {(raw1 or '')[:180]}")
            user = ((status or {}).get("data") or {}).get("user") or {}
            # create key
            headers2 = dict(headers)
            headers2["Content-Type"] = "application/json"
            code2, created, raw2 = _http_json(
                "POST",
                web + "/api/v1/users/ohmyagent/api-keys",
                headers2,
                data=b"{}",
            )
            if code2 == 401 or code2 == 403:
                raise RuntimeError(f"api-keys {code2}: {(raw2 or '')[:180]}")
            if code2 == 0:
                raise RuntimeError(raw2 or "api-keys request failed")
            if code2 >= 400:
                raise RuntimeError(f"api-keys HTTP {code2}: {(raw2 or '')[:180]}")
            data = (created or {}).get("data") or {}
            if not data.get("api_key") or not data.get("signing_secret"):
                raise RuntimeError("mint failed: " + json.dumps(created, ensure_ascii=False)[:300])
            # Fix type by mint host: .com token → .com proxy, .net → .net proxy
            base_url = upstream_base_for_web(web or page_url or preferred_web)
            account = upsert_account(
                {
                    "label": user.get("name") or "",
                    "email": user.get("email") or "",
                    "api_key": data["api_key"],
                    "signing_secret": data["signing_secret"],
                    "base_url": base_url,
                    "enabled": True,
                }
            )
            log(
                f"mint ok via {web} email={account.get('email') or ''} "
                f"base_url={base_url} "
                f"cookie_names={len(cookie_header.split(';'))} session_len={len(session)}"
            )
            return {
                "user": user,
                "key_id": data.get("id"),
                "account": account,
                "web": web,
                "base_url": base_url,
            }
        except Exception as e:
            last_err = str(e)
            log(f"mint via {web} failed: {e}")
            continue
    raise RuntimeError(f"mint failed on all hosts: {last_err}")


def test_account_connection(
    account_id: int,
    model: str = "deepseek-v4-flash",
    prompt: str = "hi",
) -> Dict[str, Any]:
    """Probe a single pool account against its own base_url (for admin UI test modal)."""
    acc = get_account(int(account_id))
    if not acc:
        raise KeyError("account not found")
    model = normalize_model(str(model or "deepseek-v4-flash"))
    prompt = (prompt or "hi").strip() or "hi"
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": 32,
        "stream": False,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    t0 = now()
    base = (acc.get("base_url") or UPSTREAM_DEFAULT).rstrip("/")
    status, hdrs, data = upstream_messages(acc, body, stream=False)
    latency = int((now() - t0) * 1000)
    raw = data if isinstance(data, (bytes, bytearray)) else b""
    if not isinstance(data, (bytes, bytearray)):
        try:
            raw = data.read()
            data.close()
        except Exception:
            raw = b""
    text = raw.decode("utf-8", "replace") if raw else ""
    preview = ""
    ok = 200 <= int(status) < 300
    if ok:
        try:
            obj = json.loads(text)
            # anthropic message content
            parts = []
            for c in (obj.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(str(c.get("text") or ""))
                elif isinstance(c, dict) and c.get("type") == "thinking":
                    # skip thinking noise for short preview
                    pass
            preview = "\n".join(p for p in parts if p).strip()
            if not preview:
                preview = text[:400]
        except Exception:
            preview = text[:400]
        mark_account_success(int(account_id))
    else:
        mark_account_failure(int(account_id), text[:300], int(status))
        preview = text[:500] or f"HTTP {status}"
    return {
        "ok": ok,
        "account_id": int(account_id),
        "email": acc.get("email") or "",
        "label": acc.get("label") or "",
        "enabled": bool(acc.get("enabled")),
        "base_url": base,
        "model": model,
        "prompt": prompt,
        "status": int(status),
        "latency_ms": latency,
        "api_key_preview": (acc.get("api_key") or "")[:12] + "…",
        "preview": preview,
        "raw": text[:2000],
    }


def upstream_messages(account: Dict[str, Any], body: Dict[str, Any], stream: bool):
    """Single upstream call using account.base_url (set correctly at mint time). No 401 retry fan-out."""
    sign_text = ensure_system(body)
    body["model"] = normalize_model(str(body.get("model") or ""))
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    base = (account.get("base_url") or UPSTREAM_DEFAULT).rstrip("/")
    url = base + "/messages"
    key_prefix = (account.get("api_key") or "")[:14]
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": USER_AGENT,
        "x-api-key": account["api_key"],
        "Authorization": "Bearer " + account["api_key"],
        "anthropic-version": "2023-06-01",
        "X-OhMyAgent-Signature": sign_system(account["signing_secret"], sign_text),
    }
    log(
        f"upstream_messages account_id={account.get('id')} key={key_prefix}… "
        f"base_url={base} model={body.get('model')} sign_len={len(sign_text)}"
    )
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        resp = http_open(req, timeout=REQUEST_TIMEOUT)
        return resp.status, dict(resp.headers.items()), resp
    except urllib.error.HTTPError as e:
        raw = e.read()
        log(
            f"upstream_messages HTTP {e.code} account_id={account.get('id')} "
            f"base_url={base} key={key_prefix}… body={raw[:200]!r}"
        )
        return e.code, dict(e.headers.items()), raw
    except Exception as e:
        log(f"upstream_messages connect fail account_id={account.get('id')} base_url={base}: {e}")
        return 502, {"Content-Type": "application/json"}, json.dumps(
            {
                "error": {
                    "message": f"upstream connect failed ({base}): {e}",
                    "type": "upstream_error",
                    "account_id": account.get("id"),
                    "base_url": base,
                }
            }
        ).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mc2api/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"{self.address_string()} {fmt % args}")

    def _read_body(self) -> bytes:
        # Cache: body stream can be read only once
        cached = getattr(self, "_cached_body", None)
        if cached is not None:
            return cached
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        self._cached_body = raw
        return raw

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _format_all_headers(self) -> str:
        """Dump every request header, including duplicates / raw form."""
        msg = self.headers
        out: List[str] = []
        # 1) plain items()
        out.append("----- headers.items() -----")
        n = 0
        try:
            for k, v in msg.items():
                out.append(f"{k}: {v}")
                n += 1
        except Exception as e:
            out.append(f"(items error: {e})")
        if n == 0:
            out.append("(none)")

        # 2) get_all for each unique key (catches multi-value)
        out.append("----- headers.get_all() -----")
        seen = set()
        try:
            for k in list(msg.keys()):
                kl = str(k).lower()
                if kl in seen:
                    continue
                seen.add(kl)
                vals = msg.get_all(k) or []
                if not vals:
                    continue
                if len(vals) == 1:
                    out.append(f"{k}: {vals[0]}")
                else:
                    for i, v in enumerate(vals):
                        out.append(f"{k} [{i}]: {v}")
        except Exception as e:
            out.append(f"(get_all error: {e})")

        # 3) raw_items if available (preserves original casing / order better)
        if hasattr(msg, "raw_items"):
            out.append("----- headers.raw_items() -----")
            try:
                for k, v in msg.raw_items():  # type: ignore[attr-defined]
                    out.append(f"{k}: {v}")
            except Exception as e:
                out.append(f"(raw_items error: {e})")

        # 4) as_string full block
        out.append("----- headers.as_string() -----")
        try:
            s = msg.as_string()
            out.append(s.rstrip("\n") if s else "(empty)")
        except Exception as e:
            out.append(f"(as_string error: {e})")

        # 5) key list
        try:
            keys = list(msg.keys())
            out.append("----- header key list -----")
            out.append(", ".join(keys) if keys else "(none)")
        except Exception:
            pass
        return "\n".join(out)

    def _dump_all_headers(self, where: str = "") -> None:
        """Dedicated loud dump of all request headers."""
        try:
            title = f"ALL REQUEST HEADERS [{self.command} {self.path}]"
            if where:
                title += f" ({where})"
            text = "\n".join(
                [
                    f"time: {iso()}",
                    f"client: {self.address_string()}",
                    f"request_line: {self.command} {self.path} {self.request_version}",
                    self._format_all_headers(),
                ]
            )
            log_block(title, text)
        except Exception as e:
            log(f"dump headers failed: {e}")

    def _dump_incoming_request(self, where: str, *, include_body: bool = True) -> None:
        """Full request dump for debugging client 401 / path issues."""
        try:
            # Always dump headers first as its own block
            self._dump_all_headers(where)

            raw_path = self.path
            norm = self._normalize_path(urlparse(raw_path).path)
            token = self._parse_auth_token()
            body_txt = ""
            body_len = 0
            if include_body and self.command in ("POST", "PUT", "PATCH"):
                raw = self._read_body()
                body_len = len(raw)
                # hard cap to avoid multi-MB paste blowing disk
                max_n = int(os.environ.get("MC_CONSOLE_LOG_BODY_MAX", "200000"))
                if body_len > max_n:
                    body_txt = raw[:max_n].decode("utf-8", "replace") + f"\n... [truncated body {body_len} bytes, max={max_n}]"
                else:
                    body_txt = raw.decode("utf-8", "replace") if raw else ""
                # pretty json if possible
                if body_txt.strip().startswith(("{", "[")):
                    try:
                        body_txt = json.dumps(json.loads(body_txt), ensure_ascii=False, indent=2)
                    except Exception:
                        pass
            lines = [
                f"time: {iso()}",
                f"client: {self.address_string()}",
                f"where: {where}",
                f"method: {self.command}",
                f"raw_path: {raw_path}",
                f"normalized_path: {norm}",
                f"http_version: {self.request_version}",
                f"parsed_api_key: {token!r}",
                f"parsed_api_key_len: {len(token) if token else 0}",
                "",
                self._format_all_headers(),
            ]
            if include_body and self.command in ("POST", "PUT", "PATCH"):
                lines.append(f"body_bytes: {body_len}")
                lines.append("body:")
                lines.append(body_txt if body_txt != "" else "  (empty)")
            log_block(f"FULL REQUEST [{self.command} {norm}]", "\n".join(lines))
        except Exception as e:
            log(f"dump request failed: {e}")

    def _dump_auth_result(self, ok: bool, detail: str = "", client: Optional[Dict[str, Any]] = None) -> None:
        token = self._parse_auth_token()
        info = {
            "ok": ok,
            "detail": detail,
            "token": token,
            "token_len": len(token) if token else 0,
            "client_key_id": (client or {}).get("id"),
            "client_key_name": (client or {}).get("name"),
            "path": self.path,
        }
        log_block("AUTH RESULT", json.dumps(info, ensure_ascii=False, indent=2))

    def _dump_response(self, code: int, body: bytes, content_type: str = "") -> None:
        max_n = int(os.environ.get("MC_CONSOLE_LOG_BODY_MAX", "200000"))
        raw = body or b""
        text = raw[:max_n].decode("utf-8", "replace")
        if len(raw) > max_n:
            text += f"\n... [truncated response {len(raw)} bytes]"
        if text.strip().startswith(("{", "[")):
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except Exception:
                pass
        log_block(
            f"FULL RESPONSE [{code} {self.command} {self.path}]",
            "\n".join(
                [
                    f"status: {code}",
                    f"content_type: {content_type}",
                    f"body_bytes: {len(raw)}",
                    "body:",
                    text if text != "" else "  (empty)",
                ]
            ),
        )

    def _send(self, code: int, body: bytes, content_type: str = "application/json",
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        # Dump gateway responses fully when debug path
        try:
            p = self._normalize_path(urlparse(self.path).path)
            if p.startswith("/v1") or p in ("/messages", "/chat/completions", "/models"):
                self._dump_response(code, body, content_type)
        except Exception:
            pass
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _cors(self) -> None:
        # local tool friendly
        pass

    def _parse_auth_token(self) -> Optional[str]:
        # Common client headers (OpenAI-compatible / Chinese IDE plugins)
        header_names = (
            "Authorization",
            "authorization",
            "x-api-key",
            "X-Api-Key",
            "api-key",
            "Api-Key",
            "API-Key",
            "openai-api-key",
            "OpenAI-Api-Key",
            "x-openai-api-key",
        )
        for name in header_names:
            raw = self.headers.get(name)
            if raw is None:
                continue
            tok = str(raw).strip()
            if not tok:
                continue
            # Authorization: Bearer sk-... / Token sk-... / just sk-...
            low = tok.lower()
            for prefix in ("bearer ", "token "):
                if low.startswith(prefix):
                    tok = tok[len(prefix):].strip()
                    break
            if tok:
                return tok
        return None

    def _normalize_path(self, path: str) -> str:
        """Normalize client path quirks: trailing slash, double /v1."""
        path = (path or "/").split("?", 1)[0]
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        # some clients set base host-only and still prefix /v1 again
        if path.startswith("/v1/v1/"):
            path = "/v1/" + path[len("/v1/v1/") :]
        elif path == "/v1/v1":
            path = "/v1"
        return path or "/"

    def _require_admin(self) -> bool:
        # Local console: admin APIs are open on loopback-oriented deployment.
        return True

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, x-api-key, api-key, Api-Key, OpenAI-Api-Key, "
            "openai-api-key, X-Admin-Token, anthropic-version",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = self._normalize_path(parsed.path)

        if path in ("/", "/admin", "/admin/"):
            return self._serve_admin()
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])

        if path in ("/healthz", "/health"):
            st = stats()
            return self._send_json(200, {"ok": True, "service": "mc2api", **{k: st[k] for k in (
                "accounts_total", "accounts_enabled", "client_keys_enabled", "requests_24h"
            )}})

        # Base URL root — many clients probe GET /v1 and require 200
        if path in ("/v1", "/v1/"):
            return self._gateway_root()

        if path.startswith("/v1") or path in ("/models",):
            self._dump_all_headers("do_GET")

        if path in ("/v1/models", "/models"):
            return self._gateway_models()

        # public meta for UI bootstrap (no secrets)
        if path in ("/admin/api/meta", "/v1/gateway-info"):
            return self._admin_get("/admin/api/meta")

        if path.startswith("/admin/api/"):
            if not self._require_admin():
                return
            return self._admin_get(path)

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self._normalize_path(urlparse(self.path).path)
        # Dump headers for every gateway-ish POST (even 404 paths)
        if path.startswith("/v1") or path in ("/messages", "/chat/completions", "/models"):
            self._dump_all_headers("do_POST")
        # Some clients POST to /v1 by mistake; point them to real paths
        if path in ("/v1", "/v1/"):
            return self._send_json(200, {
                "ok": True,
                "service": "mc2api",
                "message": "Use /v1/messages or /v1/chat/completions",
                "endpoints": {
                    "messages": "/v1/messages",
                    "chat_completions": "/v1/chat/completions",
                    "models": "/v1/models",
                },
            })
        if path in ("/v1/messages", "/messages"):
            return self._gateway_messages()
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self._gateway_chat()
        if path.startswith("/admin/api/"):
            if not self._require_admin():
                return
            return self._admin_post(path)
        self._send_json(404, {"error": "not found", "path": path})

    def do_PATCH(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/admin/api/"):
            if not self._require_admin():
                return
            return self._admin_patch(path)
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/admin/api/"):
            if not self._require_admin():
                return
            return self._admin_delete(path)
        self._send_json(404, {"error": "not found"})

    def _serve_admin(self) -> None:
        html_path = STATIC_DIR / "admin.html"
        if not html_path.exists():
            return self._send(500, b"admin.html missing", "text/plain")
        data = html_path.read_bytes()
        self._send(200, data, "text/html; charset=utf-8")

    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        fp = (STATIC_DIR / rel).resolve()
        if not str(fp).startswith(str(STATIC_DIR.resolve())) or not fp.exists():
            return self._send_json(404, {"error": "not found"})
        ctype = "application/octet-stream"
        if fp.suffix == ".css":
            ctype = "text/css"
        elif fp.suffix == ".js":
            ctype = "application/javascript"
        elif fp.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        self._send(200, fp.read_bytes(), ctype)

    # ----- admin API -----
    def _admin_get(self, path: str) -> None:
        if path == "/admin/api/stats":
            return self._send_json(200, stats())
        if path == "/admin/api/accounts":
            return self._send_json(200, {"items": list_accounts(include_secret=False)})
        if path == "/admin/api/accounts/full":
            # secrets for export
            return self._send_json(200, {"items": list_accounts(include_secret=True)})
        if path == "/admin/api/keys":
            items = list_client_keys()
            # hide full token after creation except preview
            safe = []
            for it in items:
                x = dict(it)
                # keep full token visible in admin for copy convenience
                safe.append(x)
            return self._send_json(200, {"items": safe})
        if path == "/admin/api/meta":
            px = get_proxy_config()
            return self._send_json(200, {
                "upstream_default": UPSTREAM_DEFAULT,
                "listen": f"http://{HOST}:{PORT}",
                "gateway_base": f"http://{HOST}:{PORT}/v1",
                "models_aliases": MODEL_ALIASES,
                "proxy": {
                    "enabled": px.get("enabled"),
                    "mode": px.get("mode"),
                    "url_masked": px.get("url_masked") or "",
                },
            })
        if path == "/admin/api/proxy":
            return self._send_json(200, get_proxy_config())
        if path == "/admin/api/accounts/manual-auth/status":
            q = parse_qs(urlparse(self.path).query)
            jid = (q.get("id") or [""])[0]
            if not jid or _manual_auth is None:
                return self._send_json(400, {"error": "missing id"})
            job = _manual_auth.get(jid)
            if not job:
                return self._send_json(404, {"error": "job not found"})
            return self._send_json(200, {"ok": True, "job": job.to_public()})
        if path == "/admin/api/accounts/manual-auth/active":
            if _manual_auth is None:
                return self._send_json(200, {"items": []})
            return self._send_json(200, {"items": _manual_auth.list_active()})
        self._send_json(404, {"error": "not found"})

    def _admin_post(self, path: str) -> None:
        try:
            body = self._read_json()
        except Exception as e:
            return self._send_json(400, {"error": f"invalid json: {e}"})
        try:
            if path == "/admin/api/accounts":
                acc = upsert_account(body)
                return self._send_json(200, {"ok": True, "account": {
                    **acc, "api_key": (acc.get("api_key") or "")[:10] + "…", "signing_secret": "***"
                }})
            if path == "/admin/api/accounts/import":
                # body can be one object or {accounts:[...]} or raw credentials file
                items = body.get("accounts") if isinstance(body.get("accounts"), list) else [body]
                saved = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    # accept nested key fields
                    if "key" in it and isinstance(it["key"], dict):
                        it = {
                            "email": (it.get("user") or {}).get("email") or it.get("email"),
                            "label": (it.get("user") or {}).get("name") or it.get("label"),
                            "api_key": it["key"].get("api_key"),
                            "signing_secret": it["key"].get("signing_secret"),
                            "base_url": it.get("base_url") or UPSTREAM_DEFAULT,
                        }
                    acc = upsert_account(it)
                    saved.append(acc.get("id"))
                return self._send_json(200, {"ok": True, "imported": len(saved), "ids": saved})
            if path == "/admin/api/accounts/mint-session":
                session = body.get("session") or body.get("cookie") or body.get("monkeycode_ai_session") or ""
                result = mint_from_session(str(session))
                acc = result["account"]
                return self._send_json(200, {
                    "ok": True,
                    "user": result.get("user"),
                    "account_id": acc.get("id"),
                    "email": acc.get("email"),
                    "api_key_preview": (acc.get("api_key") or "")[:10] + "…",
                })
            if path == "/admin/api/accounts/manual-auth/start":
                if _manual_auth is None:
                    return self._send_json(503, {"error": "manual auth not ready"})
                job = _manual_auth.start()
                return self._send_json(200, {"ok": True, "job": job.to_public()})
            if path == "/admin/api/accounts/manual-auth/cancel":
                if _manual_auth is None:
                    return self._send_json(503, {"error": "manual auth not ready"})
                jid = str(body.get("id") or "")
                if not jid:
                    return self._send_json(400, {"error": "missing id"})
                job = _manual_auth.cancel(jid)
                return self._send_json(200, {"ok": True, "job": job.to_public()})
            if path == "/admin/api/keys":
                item = create_client_key(str(body.get("name") or "default"))
                return self._send_json(200, {"ok": True, "key": item})
            if path == "/admin/api/proxy":
                cfg = save_proxy_config(body if isinstance(body, dict) else {})
                return self._send_json(200, {"ok": True, "proxy": cfg})
            if path == "/admin/api/proxy/test":
                body = body if isinstance(body, dict) else {}
                result = test_proxy_egress(
                    enabled=body["enabled"] if "enabled" in body else None,
                    url=body.get("url") if "url" in body else None,
                )
                return self._send_json(200, {"ok": bool(result.get("ok")), "result": result})
            # POST /admin/api/accounts/{id}/test
            if path.startswith("/admin/api/accounts/") and path.endswith("/test"):
                mid = path[len("/admin/api/accounts/") : -len("/test")]
                aid = int(mid)
                body = body if isinstance(body, dict) else {}
                result = test_account_connection(
                    aid,
                    model=str(body.get("model") or "deepseek-v4-flash"),
                    prompt=str(body.get("prompt") or body.get("message") or "hi"),
                )
                return self._send_json(200, {"ok": bool(result.get("ok")), "result": result})
            self._send_json(404, {"error": "not found"})
        except KeyError as e:
            return self._send_json(404, {"error": str(e) or "not found"})
        except Exception as e:
            return self._send_json(400, {"error": str(e)})

    def _admin_patch(self, path: str) -> None:
        try:
            body = self._read_json()
        except Exception as e:
            return self._send_json(400, {"error": f"invalid json: {e}"})
        try:
            if path == "/admin/api/proxy":
                cfg = save_proxy_config(body if isinstance(body, dict) else {})
                return self._send_json(200, {"ok": True, "proxy": cfg})
            if path.startswith("/admin/api/accounts/"):
                aid = int(path.rsplit("/", 1)[-1])
                acc = patch_account(aid, body)
                return self._send_json(200, {"ok": True, "account": {
                    **acc, "api_key": (acc.get("api_key") or "")[:10] + "…", "signing_secret": "***"
                }})
            if path.startswith("/admin/api/keys/"):
                kid = int(path.rsplit("/", 1)[-1])
                item = patch_client_key(kid, body)
                return self._send_json(200, {"ok": True, "key": item})
            self._send_json(404, {"error": "not found"})
        except KeyError:
            self._send_json(404, {"error": "not found"})
        except Exception as e:
            self._send_json(400, {"error": str(e)})

    def _admin_delete(self, path: str) -> None:
        try:
            if path.startswith("/admin/api/accounts/"):
                aid = int(path.rsplit("/", 1)[-1])
                delete_account(aid)
                return self._send_json(200, {"ok": True})
            if path.startswith("/admin/api/keys/"):
                kid = int(path.rsplit("/", 1)[-1])
                delete_client_key(kid)
                return self._send_json(200, {"ok": True})
            self._send_json(404, {"error": "not found"})
        except Exception as e:
            self._send_json(400, {"error": str(e)})

    # ----- gateway -----
    def _gateway_root(self) -> None:
        """OpenAI-compatible base URL probe (GET /v1)."""
        self._send_json(200, {
            "ok": True,
            "service": "mc2api",
            "object": "api",
            "version": "v1",
            "gateway_base": f"http://{HOST}:{PORT}/v1",
            "admin": f"http://{HOST}:{PORT}/admin",
            "endpoints": {
                "models": "/v1/models",
                "chat_completions": "/v1/chat/completions",
                "messages": "/v1/messages",
                "health": "/healthz",
            },
            "auth": "Authorization: Bearer sk-mc-...  or  x-api-key: sk-mc-...",
            "message": "mc2api gateway is running. Set this URL as API Base URL in your client.",
        })

    def _gateway_models(self) -> None:
        self._dump_incoming_request("gateway_models", include_body=False)
        try:
            client = auth_client_key(self._parse_auth_token())
            self._dump_auth_result(True, "ok", client)
        except PermissionError as e:
            self._dump_auth_result(False, str(e))
            log_request(None, None, self._normalize_path(urlparse(self.path).path), "", 401, 0, str(e))
            return self._send_json(401, {"error": {"message": str(e), "type": "auth_error"}})
        ids = list(dict.fromkeys(list(MODEL_ALIASES.keys()) + list(MODEL_ALIASES.values())))
        self._send_json(200, {
            "object": "list",
            "data": [{"id": mid, "object": "model", "created": int(now()), "owned_by": "mc2api"} for mid in ids],
        })


    def _affinity_key(self, client: Dict[str, Any]) -> str:
        # Prefer explicit session header; fallback sticky-by-client-key (grok2api-like).
        for h in ("X-Session-Id", "x-session-id", "X-Client-Session", "x-client-session"):
            v = (self.headers.get(h) or "").strip()
            if v:
                return v
        return "ck:%s" % (client.get("id") or "0")

    def _gateway_messages(self) -> None:
        t0 = now()
        self._dump_incoming_request("gateway_messages", include_body=True)
        client = None
        lease = None
        path = "/v1/messages"
        try:
            client = auth_client_key(self._parse_auth_token())
            self._dump_auth_result(True, "ok", client)
        except PermissionError as e:
            self._dump_auth_result(False, str(e))
            log_request(None, None, path, "", 401, int((now() - t0) * 1000), str(e))
            return self._send_json(401, {"error": {"message": str(e), "type": "auth_error"}})
        try:
            body = self._read_json()
        except Exception as e:
            log_request(client.get("id"), None, path, "", 400, int((now() - t0) * 1000), f"invalid json: {e}")
            return self._send_json(400, {"error": {"message": f"invalid json: {e}"}})
        stream = bool(body.get("stream"))
        model = str(body.get("model") or "")
        log(f"gateway_messages model={model!r} stream={stream} body_keys={list(body.keys())}")
        try:
            lease = acquire_account(affinity_key=self._affinity_key(client))
            account = lease.account
            log(f"gateway_messages acquired account_id={account.get('id')} email={account.get('email')}")
        except Exception as e:
            log_request(client.get("id"), None, path, model, 503, int((now()-t0)*1000), str(e))
            return self._send_json(503, {"error": {"message": str(e), "type": "no_account"}})

        try:
            status, hdrs, data = upstream_messages(account, body, stream=stream)
            latency = int((now() - t0) * 1000)
            if status >= 400:
                err = data if isinstance(data, (bytes, bytearray)) else b""
                if not isinstance(data, (bytes, bytearray)):
                    try:
                        err = data.read()
                        data.close()
                    except Exception:
                        err = b""
                mark_account_failure(int(account["id"]), err.decode("utf-8", "replace")[:300], status)
                log_request(client.get("id"), account.get("id"), path, model, status, latency, err.decode("utf-8", "replace")[:200])
                log(f"gateway_messages upstream_error status={status} account_id={account.get('id')} err={err[:300]!r}")
                ct = hdrs.get("Content-Type") or hdrs.get("content-type") or "application/json"
                return self._send(status, bytes(err), ct)

            mark_account_success(int(account["id"]))
            log_request(client.get("id"), account.get("id"), path, model, status, latency, "")
            if stream and not isinstance(data, (bytes, bytearray)):
                # keep lease until stream finishes
                try:
                    return self._proxy_stream(status, hdrs, data)
                finally:
                    pass
            raw = data if isinstance(data, (bytes, bytearray)) else data.read()
            if not isinstance(data, (bytes, bytearray)):
                try:
                    data.close()
                except Exception:
                    pass
            ct = hdrs.get("Content-Type") or hdrs.get("content-type") or "application/json"
            self._send(status, bytes(raw), ct)
        finally:
            if lease is not None:
                lease.release()

    def _gateway_chat(self) -> None:
        t0 = now()
        self._dump_incoming_request("gateway_chat", include_body=True)
        client = None
        lease = None
        path = "/v1/chat/completions"
        try:
            client = auth_client_key(self._parse_auth_token())
            self._dump_auth_result(True, "ok", client)
        except PermissionError as e:
            self._dump_auth_result(False, str(e))
            log_request(None, None, path, "", 401, int((now() - t0) * 1000), str(e))
            return self._send_json(401, {"error": {"message": str(e), "type": "auth_error"}})
        try:
            body = self._read_json()
        except Exception as e:
            log_request(client.get("id"), None, path, "", 400, int((now() - t0) * 1000), f"invalid json: {e}")
            return self._send_json(400, {"error": {"message": f"invalid json: {e}"}})
        model = str(body.get("model") or "")
        log(f"gateway_chat model={model!r} stream={bool(body.get('stream'))} body_keys={list(body.keys())}")
        try:
            lease = acquire_account(affinity_key=self._affinity_key(client))
            account = lease.account
            log(f"gateway_chat acquired account_id={account.get('id')} email={account.get('email')}")
        except Exception as e:
            log_request(client.get("id"), None, path, model, 503, int((now()-t0)*1000), str(e))
            return self._send_json(503, {"error": {"message": str(e), "type": "no_account"}})

        try:
            anthropic_body = openai_to_anthropic(body)
            stream = bool(anthropic_body.get("stream"))
            status, hdrs, data = upstream_messages(account, anthropic_body, stream=stream)
            latency = int((now() - t0) * 1000)
            if status >= 400:
                err = data if isinstance(data, (bytes, bytearray)) else b""
                if not isinstance(data, (bytes, bytearray)):
                    try:
                        err = data.read(); data.close()
                    except Exception:
                        err = b""
                mark_account_failure(int(account["id"]), err.decode("utf-8", "replace")[:300], status)
                log_request(client.get("id"), account.get("id"), path, model, status, latency, err.decode("utf-8", "replace")[:200])
                log(f"gateway_chat upstream_error status={status} account_id={account.get('id')} err={err[:300]!r}")
                try:
                    obj = json.loads(err.decode("utf-8", "replace"))
                except Exception:
                    obj = {"error": {"message": err.decode("utf-8", "replace")[:500], "type": "upstream_error"}}
                return self._send_json(status, obj)

            mark_account_success(int(account["id"]))
            log_request(client.get("id"), account.get("id"), path, model, status, latency, "")
            out_model = anthropic_body.get("model") or model
            if stream and not isinstance(data, (bytes, bytearray)):
                return self._stream_openai_from_anthropic(str(out_model), data)
            raw = data if isinstance(data, (bytes, bytearray)) else data.read()
            if not isinstance(data, (bytes, bytearray)):
                try:
                    data.close()
                except Exception:
                    pass
            try:
                msg = json.loads(bytes(raw).decode("utf-8"))
            except Exception:
                return self._send(502, bytes(raw), "application/json")
            self._send_json(200, anthropic_to_openai(msg, str(out_model)))
        finally:
            if lease is not None:
                lease.release()

    def _proxy_stream(self, status: int, hdrs: Dict[str, str], resp: Any) -> None:
        self.send_response(status)
        ct = hdrs.get("Content-Type") or hdrs.get("content-type") or "text/event-stream"
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _stream_openai_from_anthropic(self, model: str, resp: Any) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(now())

        def emit(obj: Dict[str, Any]) -> None:
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        emit({
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        })
        buf = ""
        finish = "stop"
        try:
            while True:
                chunk = resp.read(1024)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload)
                    except Exception:
                        continue
                    et = evt.get("type")
                    if et == "content_block_delta":
                        delta = evt.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            emit({
                                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                                "choices": [{"index": 0, "delta": {"content": delta["text"]}, "finish_reason": None}],
                            })
                    elif et == "message_delta":
                        sr = (evt.get("delta") or {}).get("stop_reason")
                        if sr == "max_tokens":
                            finish = "length"
                        elif sr == "tool_use":
                            finish = "tool_calls"
        finally:
            try:
                resp.close()
            except Exception:
                pass
        emit({
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        })
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def maybe_seed() -> None:
    """Import known local credential files if pool empty."""
    with _db_lock:
        conn = db()
        try:
            n = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
        finally:
            conn.close()
    if n:
        return
    candidates = [
        Path.home() / ".cache/monkeycode-web-auth/out/latest_credentials.json",
        Path.home() / "Library/Application Support/com.chaitin.baizhi.monkeycode/monkeycode-ohmyagent-key.json",
    ]
    settings = Path.home() / "Library/Application Support/com.chaitin.baizhi.monkeycode/ohmyagent/settings.json"
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        api_key = data.get("api_key")
        secret = data.get("signing_secret")
        if not api_key or not secret:
            continue
        email = data.get("email") or ""
        label = data.get("label") or data.get("name") or ""
        # enrich email from status cookie file name
        try:
            upsert_account({
                "api_key": api_key,
                "signing_secret": secret,
                "email": email,
                "label": label,
                "base_url": data.get("base_url") or UPSTREAM_DEFAULT,
                "enabled": True,
            })
            log(f"seeded account from {p}")
        except Exception as e:
            log(f"seed skip {p}: {e}")
    # also seed all out/*.json
    out_dir = Path.home() / ".cache/monkeycode-web-auth/out"
    if out_dir.exists():
        for p in sorted(out_dir.glob("*.json")):
            if p.name == "latest_credentials.json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("api_key") and data.get("signing_secret"):
                    upsert_account({
                        "api_key": data["api_key"],
                        "signing_secret": data["signing_secret"],
                        "email": data.get("email") or "",
                        "label": data.get("label") or data.get("name") or "",
                        "base_url": data.get("base_url") or UPSTREAM_DEFAULT,
                        "enabled": True,
                    })
            except Exception:
                pass


def main() -> None:
    global _manual_auth
    init_db()
    maybe_seed()
    try:
        px = _sync_proxy_state(force=True)
        if px.get("enabled"):
            log(f"egress proxy enabled: {mask_proxy_url(str(px.get('url') or ''))}")
        else:
            log("egress proxy: direct")
    except Exception as e:
        log(f"egress proxy load failed: {e}")
    _manual_auth = ManualAuthManager(
        work_root=DATA_DIR / "chrome-auth",
        mint_callback=mint_from_session,
        timeout=int(os.environ.get("MC_CONSOLE_AUTH_TIMEOUT", "900")),
    )
    # ensure at least one client key for convenience if none
    if not list_client_keys():
        k = create_client_key("default")
        log(f"created default client key: {k['token']}")
        (DATA_DIR / "default_client_key.txt").write_text(k["token"] + "\n", encoding="utf-8")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"listen http://{HOST}:{PORT}")
    log(f"admin UI  http://{HOST}:{PORT}/admin")
    log(f"gateway   http://{HOST}:{PORT}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
