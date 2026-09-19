import os
import secrets
import logging
from typing import Optional, Dict, Any
from pathlib import Path
from fastapi import Request, HTTPException, status, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import httpx

logger = logging.getLogger("auth")

# Authentication configuration from environment
AUTH_MODE = os.environ.get("AUTH_MODE", "none").lower() # none, basic, oidc, forward_auth
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "admin")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "password")

# OIDC / Authentik configuration
OIDC_ISSUER_URL = os.environ.get("OIDC_ISSUER_URL", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile")

# Forward Auth headers (Authentik Proxy, Authelia, Traefik, Cloudflare Access)
FORWARD_AUTH_HEADER = os.environ.get("FORWARD_AUTH_HEADER", "X-authentik-username")

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
        return new_secret
    except Exception:
        return secrets.token_hex(32)

SECRET_KEY = get_or_create_secret_key()
serializer = URLSafeTimedSerializer(SECRET_KEY)
COOKIE_NAME = "selkies_session"
MAX_AGE = 86400 * 7 # 7 days session lifetime

# Cache OIDC OpenID configuration
_oidc_config: Optional[Dict[str, Any]] = None

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
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
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

def create_session_cookie(response: Response, user_data: dict):
    token = serializer.dumps(user_data)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False # set True if strictly HTTPS
    )

def clear_session_cookie(response: Response):
    response.delete_cookie(key=COOKIE_NAME)

def get_current_user(request: Request) -> Dict[str, Any]:
    """Dependency that resolves and validates the current user based on AUTH_MODE."""
    if AUTH_MODE == "none":
        return {"authenticated": True, "username": "guest", "auth_mode": "none"}
    
    # 1. Check Forward Auth header (Authentik Outpost / Traefik / Authelia)
    if AUTH_MODE == "forward_auth":
        forward_headers = [
            FORWARD_AUTH_HEADER,
            "X-authentik-username",
            "Remote-User",
            "X-Forwarded-User",
            "X-Forwarded-Email"
        ]
        for hdr in forward_headers:
            val = request.headers.get(hdr)
            if val:
                return {
                    "authenticated": True,
                    "username": val,
                    "email": request.headers.get("X-authentik-email", ""),
                    "auth_mode": "forward_auth"
                }
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Forward authentication header missing")

    # 2. Check Session Cookie (Basic Auth / OIDC)
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token:
        try:
            data = serializer.loads(cookie_token, max_age=MAX_AGE)
            return {
                "authenticated": True,
                "username": data.get("username", "user"),
                "email": data.get("email", ""),
                "auth_mode": data.get("auth_mode", AUTH_MODE)
            }
        except (BadSignature, SignatureExpired):
            pass

    # 3. Check HTTP Authorization header (Basic Auth fallback)
    auth_header = request.headers.get("Authorization")
    if AUTH_MODE == "basic" and auth_header and auth_header.startswith("Basic "):
        import base64
        try:
            encoded = auth_header.split(" ", 1)[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            u, p = decoded.split(":", 1)
            if u == BASIC_AUTH_USER and p == BASIC_AUTH_PASSWORD:
                return {"authenticated": True, "username": u, "auth_mode": "basic"}
        except Exception:
            pass

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
