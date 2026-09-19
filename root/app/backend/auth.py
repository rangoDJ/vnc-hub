import os
import time
import base64
import secrets
import logging
import ipaddress
import threading
from typing import Optional, Dict, Any, List
from pathlib import Path
from fastapi import Request, HTTPException, status, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import httpx

logger = logging.getLogger("auth")

def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

# Authentication configuration from environment
AUTH_MODE = os.environ.get("AUTH_MODE", "none").lower() # none, basic, oidc, forward_auth
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "admin")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")

# OIDC / Authentik configuration
OIDC_ISSUER_URL = os.environ.get("OIDC_ISSUER_URL", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile")
# TLS verification for the OIDC provider; only disable for self-signed test setups
OIDC_VERIFY_SSL = _env_bool("OIDC_VERIFY_SSL", True)

# Forward Auth header (Authentik Proxy, Authelia, Traefik, Cloudflare Access)
FORWARD_AUTH_HEADER = os.environ.get("FORWARD_AUTH_HEADER", "X-authentik-username")
FORWARD_AUTH_EMAIL_HEADER = os.environ.get("FORWARD_AUTH_EMAIL_HEADER", "X-authentik-email")
# Comma-separated IPs/CIDRs of the reverse proxy allowed to set the forward auth header
FORWARD_AUTH_TRUSTED_PROXIES = os.environ.get("FORWARD_AUTH_TRUSTED_PROXIES", "")

# Session cookie "Secure" flag: auto (follow request scheme), true, false
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "auto").lower()

# Secret key persistence for session cookies
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SECRET_KEY_FILE = CONFIG_DIR / ".session_secret"

def get_or_create_secret_key() -> str:
    env_secret = os.environ.get("SECRET_KEY")
    if env_secret:
        return env_secret
    try:
        if SECRET_KEY_FILE.exists():
            return SECRET_KEY_FILE.read_text().strip()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        new_secret = secrets.token_hex(32)
        SECRET_KEY_FILE.write_text(new_secret)
        SECRET_KEY_FILE.chmod(0o600)
        return new_secret
    except Exception as e:
        logger.warning("Could not persist session secret (%s); sessions will reset on restart", e)
        return secrets.token_hex(32)

SECRET_KEY = get_or_create_secret_key()
serializer = URLSafeTimedSerializer(SECRET_KEY)
state_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="oidc-state")
COOKIE_NAME = "selkies_session"
STATE_COOKIE_NAME = "selkies_oidc_state"
STATE_MAX_AGE = 600
MAX_AGE = 86400 * 7 # 7 days session lifetime

def _parse_networks(raw: str) -> List[ipaddress._BaseNetwork]:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.error("Ignoring invalid FORWARD_AUTH_TRUSTED_PROXIES entry: %s", part)
    return nets

TRUSTED_PROXY_NETS = _parse_networks(FORWARD_AUTH_TRUSTED_PROXIES)

# Startup configuration sanity checks
if AUTH_MODE == "basic" and not BASIC_AUTH_PASSWORD:
    logger.error("AUTH_MODE=basic but BASIC_AUTH_PASSWORD is empty; all logins will be rejected")
if AUTH_MODE == "forward_auth" and not TRUSTED_PROXY_NETS:
    logger.error("AUTH_MODE=forward_auth but FORWARD_AUTH_TRUSTED_PROXIES is empty; all requests will be rejected")
if AUTH_MODE == "oidc" and not OIDC_VERIFY_SSL:
    logger.warning("OIDC_VERIFY_SSL is disabled; OIDC traffic is vulnerable to interception")

def client_ip(request: Request) -> str:
    """Peer address as seen by nginx (X-Real-IP is always overwritten by our nginx config)."""
    return request.headers.get("X-Real-IP") or (request.client.host if request.client else "")

def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXY_NETS)

def check_basic_credentials(username: str, password: str) -> bool:
    if not BASIC_AUTH_PASSWORD:
        return False
    user_ok = secrets.compare_digest(username.encode("utf-8"), BASIC_AUTH_USER.encode("utf-8"))
    pass_ok = secrets.compare_digest(password.encode("utf-8"), BASIC_AUTH_PASSWORD.encode("utf-8"))
    return user_ok and pass_ok

# ----------------- Login rate limiting -----------------

LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
_failed_logins: Dict[str, List[float]] = {}
_failed_lock = threading.Lock()

def _recent_failures(ip: str, now: float) -> List[float]:
    attempts = [t for t in _failed_logins.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if attempts:
        _failed_logins[ip] = attempts
    else:
        _failed_logins.pop(ip, None)
    return attempts

def check_login_rate_limit(ip: str):
    with _failed_lock:
        if len(_recent_failures(ip, time.time())) >= LOGIN_MAX_FAILURES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. Try again later."
            )

def record_login_failure(ip: str):
    with _failed_lock:
        now = time.time()
        _recent_failures(ip, now)
        _failed_logins.setdefault(ip, []).append(now)

def clear_login_failures(ip: str):
    with _failed_lock:
        _failed_logins.pop(ip, None)

# ----------------- OIDC -----------------

# Cache OIDC OpenID configuration
_oidc_config: Optional[Dict[str, Any]] = None

def oidc_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=OIDC_VERIFY_SSL, timeout=10.0)

async def get_oidc_config() -> Dict[str, Any]:
    global _oidc_config
    if _oidc_config is not None:
        return _oidc_config
    if not OIDC_ISSUER_URL:
        raise HTTPException(status_code=500, detail="OIDC_ISSUER_URL not configured")

    discovery_urls = [
        f"{OIDC_ISSUER_URL}/.well-known/openid-configuration",
        f"{OIDC_ISSUER_URL}/application/o/.well-known/openid-configuration"
    ]
    async with oidc_http_client() as client:
        for url in discovery_urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    _oidc_config = resp.json()
                    logger.info("Discovered OIDC endpoints from %s", url)
                    return _oidc_config
            except Exception as e:
                logger.warning("Failed OIDC discovery on %s: %s", url, e)
    raise HTTPException(status_code=500, detail="Could not retrieve OIDC configuration from issuer")

# ----------------- Cookies -----------------

def _cookie_secure(request: Optional[Request]) -> bool:
    if COOKIE_SECURE in ("1", "true", "yes", "on"):
        return True
    if COOKIE_SECURE in ("0", "false", "no", "off"):
        return False
    if request is None:
        return False
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"

def create_session_cookie(response: Response, user_data: dict, request: Optional[Request] = None):
    token = serializer.dumps(user_data)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request)
    )

def clear_session_cookie(response: Response):
    response.delete_cookie(key=COOKIE_NAME)

def create_state_cookie(response: Response, state: str, request: Optional[Request] = None):
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=state_serializer.dumps(state),
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request)
    )

def verify_state_cookie(request: Request, state: Optional[str]) -> bool:
    token = request.cookies.get(STATE_COOKIE_NAME)
    if not token or not state:
        return False
    try:
        expected = state_serializer.loads(token, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return secrets.compare_digest(str(expected), state)

def clear_state_cookie(response: Response):
    response.delete_cookie(key=STATE_COOKIE_NAME)

# ----------------- User resolution -----------------

def get_current_user(request: Request) -> Dict[str, Any]:
    """Dependency that resolves and validates the current user based on AUTH_MODE."""
    if AUTH_MODE == "none":
        return {"authenticated": True, "username": "guest", "auth_mode": "none"}

    # 1. Forward Auth header (Authentik Outpost / Traefik / Authelia), only from a trusted proxy
    if AUTH_MODE == "forward_auth":
        val = request.headers.get(FORWARD_AUTH_HEADER)
        if val:
            ip = client_ip(request)
            if not _is_trusted_proxy(ip):
                logger.warning("Rejected forward auth header from untrusted address %s", ip)
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Untrusted forward authentication source")
            return {
                "authenticated": True,
                "username": val,
                "email": request.headers.get(FORWARD_AUTH_EMAIL_HEADER, ""),
                "auth_mode": "forward_auth"
            }
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Forward authentication header missing")

    # 2. Session Cookie (Basic Auth / OIDC)
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token:
        try:
            data = serializer.loads(cookie_token, max_age=MAX_AGE)
            if data.get("auth_mode") == AUTH_MODE:
                return {
                    "authenticated": True,
                    "username": data.get("username", "user"),
                    "email": data.get("email", ""),
                    "auth_mode": AUTH_MODE
                }
        except (BadSignature, SignatureExpired):
            pass

    # 3. HTTP Authorization header (Basic Auth fallback for scripts)
    auth_header = request.headers.get("Authorization")
    if AUTH_MODE == "basic" and auth_header and auth_header.startswith("Basic "):
        ip = client_ip(request)
        check_login_rate_limit(ip)
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            u, p = decoded.split(":", 1)
        except Exception:
            u, p = "", ""
        if check_basic_credentials(u, p):
            clear_login_failures(ip)
            return {"authenticated": True, "username": u, "auth_mode": "basic"}
        record_login_failure(ip)

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
