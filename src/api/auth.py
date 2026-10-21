"""
Login for access from outside this Mac (tunnel / Vercel). On this machine (loopback client + loopback Host) nothing
changes. From anywhere else: every page and API needs a signed session cookie, and the session is READ-ONLY — any
request that is not GET/HEAD is refused (buying, selling, Start/Stop and the kill switch stay on the Mac).
Wrong passwords: after LOGIN_MAX_FAILS in a row, login is locked for LOGIN_LOCK_MIN minutes for everyone
(global, because behind a tunnel the client IP can be spoofed).
"""
import hashlib
import hmac
import html
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.api.paper_routes import LOCAL_CLIENTS, LOCAL_HOSTNAMES

COOKIE = "kairos_s"
SESSION_S = 7 * 24 * 3600
LOGIN_MAX_FAILS, LOGIN_LOCK_MIN = 5, 15
USER = os.getenv("KAIROS_LOGIN_USER", "root")
_SALT = b"kairos-login-v1"
PASS_HASH = os.getenv("KAIROS_LOGIN_HASH", "")      # hex pbkdf2 of the password (scripts: see hash_password)
_fails: list[float] = []
_locked_until = 0.0


def hash_password(pw: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), _SALT, 200_000).hex()


def _secret() -> bytes:
    """Signing key, kept across restarts (sessions survive a restart) in a git-ignored file."""
    env = os.getenv("KAIROS_SESSION_SECRET")
    if env:
        return env.encode()
    f = Path(os.getenv("KAIROS_SESSION_FILE", ".session_secret"))
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip().encode()


def make_session(user: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_S)
    msg = f"{user}.{exp}"
    return f"{msg}.{hmac.new(_secret(), msg.encode(), 'sha256').hexdigest()}"


def valid_session(token: str | None, now: float | None = None) -> bool:
    try:
        user, exp, sig = (token or "").rsplit(".", 2)
    except ValueError:
        return False
    good = hmac.new(_secret(), f"{user}.{exp}".encode(), "sha256").hexdigest()
    return hmac.compare_digest(sig, good) and user == USER and int(exp) > (now or time.time())


def check_login(user: str, pw: str, now: float | None = None) -> str:
    """'ok', 'bad' or 'locked'."""
    global _locked_until
    now = now or time.time()
    if now < _locked_until:
        return "locked"
    if PASS_HASH and hmac.compare_digest(user, USER) and hmac.compare_digest(hash_password(pw), PASS_HASH):
        _fails.clear()
        return "ok"
    _fails[:] = [t for t in _fails if t > now - LOGIN_LOCK_MIN * 60] + [now]
    if len(_fails) >= LOGIN_MAX_FAILS:
        _locked_until, _fails[:] = now + LOGIN_LOCK_MIN * 60, []
        return "locked"
    return "bad"


def is_local(request: Request) -> bool:
    client = request.client.host if request.client else ""
    return client in LOCAL_CLIENTS and (request.url.hostname or "") in LOCAL_HOSTNAMES \
        and "x-forwarded-for" not in request.headers and "cf-connecting-ip" not in request.headers


def _page(msg: str = "") -> HTMLResponse:
    err = f'<p class="err" role="alert">{html.escape(msg)}</p>' if msg else ""
    return HTMLResponse(f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kairos — entrar</title>
<link rel="icon" href="/static/favicon.svg?v=2"><style>
:root{{--bg:#0b0e0d;--panel:#121715;--border:#24302b;--text:#e6ece9;--muted:#8a9a93;--accent:#6fd3a0;--red:#ef7b7b}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,sans-serif;padding:16px}}form{{width:100%;max-width:340px;background:var(--panel);
border:1px solid var(--border);border-radius:16px;padding:28px}}h1{{margin:0 0 4px;font-size:22px}}p{{margin:0 0 20px;color:var(--muted);font-size:13px}}
label{{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px}}input{{width:100%;padding:12px;border-radius:10px;
border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:16px}}button{{width:100%;margin-top:20px;padding:13px;
border:0;border-radius:10px;background:var(--accent);color:#06130d;font-weight:700;font-size:15px;cursor:pointer}}
.err{{color:var(--red);margin:12px 0 0}}</style></head><body><form method="post" action="/login">
<h1>Kairos</h1><p>Acesso remoto · somente leitura</p>
<label for="u">Usuário</label><input id="u" name="user" autocomplete="username" autocapitalize="off" required>
<label for="p">Senha</label><input id="p" name="password" type="password" autocomplete="current-password" required>
{err}<button type="submit">Entrar</button></form></body></html>""", status_code=401 if msg else 200)


async def middleware(request: Request, call_next):
    if is_local(request):
        return await call_next(request)
    path = request.url.path
    if path == "/login":
        if request.method == "POST":
            form = parse_qs((await request.body()).decode(errors="ignore"))
            res = check_login(form.get("user", [""])[0], form.get("password", [""])[0])
            if res != "ok":
                return _page("Muitas tentativas erradas — login bloqueado por 15 minutos." if res == "locked"
                             else "Usuário ou senha incorretos.")
            r = RedirectResponse("/", status_code=303)
            r.set_cookie(COOKIE, make_session(USER), max_age=SESSION_S, httponly=True, secure=True, samesite="lax")
            return r
        return _page()
    if path == "/logout":
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE)
        return r
    if path.startswith("/static/favicon"):
        return await call_next(request)
    if not valid_session(request.cookies.get(COOKIE)):
        if path.startswith("/api"):
            return JSONResponse({"detail": "login necessário"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    if request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": "acesso remoto é somente leitura — use o Mac para operar"}, status_code=403)
    return await call_next(request)
