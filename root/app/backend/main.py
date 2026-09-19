import os
import json
import uuid
import logging
import threading
import urllib.parse
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Request, Response, UploadFile, File, Form
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import (
    AUTH_MODE,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_REDIRECT_URI,
    OIDC_SCOPES,
    get_oidc_config,
    oidc_http_client,
    get_current_user,
    check_basic_credentials,
    check_login_rate_limit,
    record_login_failure,
    clear_login_failures,
    client_ip,
    create_session_cookie,
    clear_session_cookie,
    create_state_cookie,
    verify_state_cookie,
    clear_state_cookie
)
from session_manager import session_manager
from file_manager import (
    list_files,
    save_uploaded_file,
    create_folder,
    get_file_for_download,
    delete_path
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("server")

app = FastAPI(title="Selkies RDP Gateway", version="1.0.0")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
PROFILES_FILE = CONFIG_DIR / "profiles.json"
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", "/app/frontend"))

PASSWORD_MASK = "••••••••"
SINGLE_LINE = r"^[^\r\n]*$"

# Profile fields that hold secrets: masked when listed, kept when the mask is sent back
SECRET_FIELDS = ("password", "ssh_key")

# Pydantic models
class ConnectionProfile(BaseModel):
    id: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    protocol: str = Field(default="rdp", pattern=r"^(rdp|vnc|ssh)$")
    name: str = Field(default="", max_length=100, pattern=SINGLE_LINE)
    # Must not start with "-", so a host can never be parsed as a client option
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9\[][A-Za-z0-9._:\[\]-]*$")
    # None means the protocol's default port (3389 / 5900 / 22)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default="", max_length=256, pattern=SINGLE_LINE)
    password: Optional[str] = Field(default="", max_length=512, pattern=SINGLE_LINE)
    # RDP
    domain: Optional[str] = Field(default="", max_length=256, pattern=SINGLE_LINE)
    resolution: str = Field(default="dynamic", pattern=r"^(dynamic|\d{3,5}x\d{3,5})$")
    # Windows display scaling: "auto" follows the browser's devicePixelRatio, or a fixed percentage
    scale: str = Field(default="auto", pattern=r"^(auto|100|125|150|175|200|225|250|300)$")
    enable_audio: bool = True
    enable_clipboard: bool = True
    enable_drive: bool = True
    ignore_cert: bool = True
    # VNC
    view_only: bool = False
    # SSH
    ssh_key: Optional[str] = Field(default="", max_length=16384)
    font_size: int = Field(default=12, ge=6, le=48)

class ConnectRequest(BaseModel):
    profile_id: Optional[str] = None
    custom: Optional[ConnectionProfile] = None
    # window.devicePixelRatio of the viewing browser, used when the profile's scale is "auto"
    device_pixel_ratio: Optional[float] = Field(default=None, ge=0.5, le=5)

class KeyActionRequest(BaseModel):
    key: str # "ctrl_alt_del", "super", "alt_tab"

class ClipboardRequest(BaseModel):
    text: str = Field(max_length=1_000_000)

class FolderRequest(BaseModel):
    path: str = ""
    name: str = Field(min_length=1, max_length=255)

class LoginRequest(BaseModel):
    username: str
    password: str

# Helper functions for profiles storage
_profiles_lock = threading.Lock()

def load_profiles() -> List[Dict[str, Any]]:
    if not PROFILES_FILE.exists():
        return []
    try:
        with open(PROFILES_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read profiles: {e}")
        return []

def save_profiles(profiles: List[Dict[str, Any]]):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write, readable only by the service user (file contains RDP passwords)
    tmp = PROFILES_FILE.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(profiles, f, indent=2)
    os.replace(tmp, PROFILES_FILE)
    os.chmod(PROFILES_FILE, 0o600)

def resolve_desktop_scale(scale: Optional[str], device_pixel_ratio: Optional[float]) -> int:
    """Windows desktop scale percentage (100-500) for a profile's scale setting."""
    if scale and scale != "auto":
        return int(scale)
    if not device_pixel_ratio:
        return 100
    # Selkies streams at the browser's physical pixel density, so match Windows scaling to it
    percent = round(device_pixel_ratio * 100 / 25) * 25
    return max(100, min(500, percent))

def find_profile(profiles: List[Dict[str, Any]], profile_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not profile_id:
        return None
    return next((p for p in profiles if p.get("id") == profile_id), None)

# ----------------- Authentication Routes -----------------

@app.get("/auth/status")
async def auth_status(request: Request):
    try:
        user = get_current_user(request)
        return {"authenticated": True, "user": user["username"], "auth_mode": AUTH_MODE}
    except HTTPException:
        return {"authenticated": False, "user": None, "auth_mode": AUTH_MODE}

@app.post("/auth/login")
async def basic_login(req: LoginRequest, request: Request, response: Response):
    if AUTH_MODE != "basic":
        raise HTTPException(status_code=400, detail=f"Basic auth is not enabled (mode is {AUTH_MODE})")

    ip = client_ip(request)
    check_login_rate_limit(ip)
    if check_basic_credentials(req.username, req.password):
        clear_login_failures(ip)
        create_session_cookie(response, {"username": req.username, "auth_mode": "basic"}, request)
        return {"success": True, "message": "Login successful"}

    record_login_failure(ip)
    logger.warning("Failed basic login for user %r from %s", req.username, ip)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

@app.get("/auth/login/oidc")
async def oidc_login(request: Request):
    if AUTH_MODE != "oidc":
        raise HTTPException(status_code=400, detail="OIDC authentication is not enabled")

    config = await get_oidc_config()
    auth_endpoint = config.get("authorization_endpoint")
    if not auth_endpoint:
        raise HTTPException(status_code=500, detail="Authorization endpoint not found in OIDC provider")

    state = uuid.uuid4().hex
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": OIDC_SCOPES,
        "state": state
    }
    response = RedirectResponse(f"{auth_endpoint}?{urllib.parse.urlencode(params)}")
    create_state_cookie(response, state, request)
    return response

def _login_error(code: str) -> RedirectResponse:
    response = RedirectResponse(f"/login.html?error={urllib.parse.quote(code)}")
    clear_state_cookie(response)
    return response

@app.get("/auth/callback")
async def oidc_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if AUTH_MODE != "oidc":
        raise HTTPException(status_code=400, detail="OIDC authentication is not enabled")
    if error or not code:
        return _login_error(error or "missing_code")
    if not verify_state_cookie(request, state):
        logger.warning("OIDC callback with invalid or missing state from %s", client_ip(request))
        return _login_error("invalid_state")

    config = await get_oidc_config()
    token_endpoint = config.get("token_endpoint")
    userinfo_endpoint = config.get("userinfo_endpoint")
    if not token_endpoint:
        return _login_error("token_endpoint_missing")

    async with oidc_http_client() as client:
        # Exchange code for tokens
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": OIDC_CLIENT_ID,
                "client_secret": OIDC_CLIENT_SECRET,
                "redirect_uri": OIDC_REDIRECT_URI
            }
        )
        if token_resp.status_code != 200:
            logger.error(f"Failed OIDC token exchange: {token_resp.text}")
            return _login_error("token_exchange_failed")

        access_token = token_resp.json().get("access_token")
        if not userinfo_endpoint or not access_token:
            return _login_error("userinfo_unavailable")

        user_resp = await client.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if user_resp.status_code != 200:
            logger.error(f"Failed OIDC userinfo request: {user_resp.status_code}")
            return _login_error("userinfo_failed")
        uinfo = user_resp.json()
        username = uinfo.get("preferred_username") or uinfo.get("email") or uinfo.get("name") or uinfo.get("sub", "user")
        email = uinfo.get("email", "")

    response = RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    clear_state_cookie(response)
    create_session_cookie(response, {"username": username, "email": email, "auth_mode": "oidc"}, request)
    return response

@app.post("/auth/logout")
@app.get("/auth/logout")
async def logout():
    # Cookie must be cleared on the response actually returned, not an injected one
    response = RedirectResponse("/login.html", status_code=status.HTTP_302_FOUND)
    clear_session_cookie(response)
    return response

@app.get("/auth/verify")
async def auth_verify(request: Request):
    """Endpoint called by Nginx auth_request to protect the WebUI and Selkies stream."""
    try:
        get_current_user(request)
        return Response(status_code=200)
    except HTTPException:
        return Response(status_code=401)

# ----------------- Profiles API -----------------

@app.get("/api/profiles")
def get_profiles(user: dict = Depends(get_current_user)):
    profiles = load_profiles()
    # Mask secrets when returning profiles list
    masked = []
    for p in profiles:
        cp = p.copy()
        cp.setdefault("protocol", "rdp") # profiles saved before multi-protocol support
        for secret in SECRET_FIELDS:
            cp[f"has_{secret}"] = bool(cp.get(secret))
            if cp.get(secret):
                cp[secret] = PASSWORD_MASK
        masked.append(cp)
    return masked

def restore_masked_secrets(data: Dict[str, Any], stored: Optional[Dict[str, Any]]):
    """Swap masked placeholders sent back by the UI for the stored secret values."""
    for secret in SECRET_FIELDS:
        if data.get(secret) == PASSWORD_MASK:
            data[secret] = stored.get(secret, "") if stored else ""

@app.post("/api/profiles")
def save_profile(profile: ConnectionProfile, user: dict = Depends(get_current_user)):
    with _profiles_lock:
        profiles = load_profiles()
        existing = find_profile(profiles, profile.id)
        for secret in SECRET_FIELDS:
            if getattr(profile, secret) == PASSWORD_MASK:
                setattr(profile, secret, existing.get(secret, "") if existing else "")
        if not profile.id:
            profile.id = str(uuid.uuid4())
        data = profile.model_dump()
        if existing:
            profiles[profiles.index(existing)] = data
        else:
            profiles.append(data)
        save_profiles(profiles)
    return {"success": True, "id": profile.id}

@app.delete("/api/profiles/{profile_id}")
def remove_profile(profile_id: str, user: dict = Depends(get_current_user)):
    with _profiles_lock:
        profiles = [p for p in load_profiles() if p.get("id") != profile_id]
        save_profiles(profiles)
    return {"success": True}

# ----------------- Session Control API -----------------

@app.post("/api/session/connect")
def connect_session(req: ConnectRequest, user: dict = Depends(get_current_user)):
    if req.profile_id:
        config_dict = find_profile(load_profiles(), req.profile_id)
        if not config_dict:
            raise HTTPException(status_code=404, detail="Profile not found")
    elif req.custom:
        config_dict = req.custom.model_dump()
        # Form was loaded from a saved profile and its secrets left untouched
        if any(config_dict.get(s) == PASSWORD_MASK for s in SECRET_FIELDS):
            restore_masked_secrets(config_dict, find_profile(load_profiles(), req.custom.id))
    else:
        raise HTTPException(status_code=400, detail="Missing connection parameters")

    config_dict = dict(config_dict)
    config_dict.setdefault("protocol", "rdp")
    config_dict["desktop_scale"] = resolve_desktop_scale(config_dict.get("scale"), req.device_pixel_ratio)

    logger.info("User %s starting %s session to %s", user.get("username"),
                config_dict["protocol"].upper(), config_dict.get("host"))
    result = session_manager.connect(config_dict)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result

@app.post("/api/session/disconnect")
def disconnect_session(user: dict = Depends(get_current_user)):
    return session_manager.disconnect()

@app.get("/api/session/status")
def session_status(user: dict = Depends(get_current_user)):
    return session_manager.get_status()

@app.post("/api/session/send-keys")
def session_send_keys(req: KeyActionRequest, user: dict = Depends(get_current_user)):
    result = session_manager.send_keys(req.key)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result

@app.post("/api/session/clipboard")
def session_clipboard(req: ClipboardRequest, user: dict = Depends(get_current_user)):
    result = session_manager.set_clipboard(req.text)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result

# ----------------- Shared Files API -----------------

@app.get("/api/files")
def api_list_files(path: str = "", user: dict = Depends(get_current_user)):
    return list_files(path)

@app.post("/api/files/upload")
async def api_upload_file(
    file: UploadFile = File(...),
    path: str = Form(""),
    overwrite: bool = Form(False),
    user: dict = Depends(get_current_user)
):
    return await save_uploaded_file(file, path, overwrite)

@app.post("/api/files/folder")
def api_create_folder(req: FolderRequest, user: dict = Depends(get_current_user)):
    return create_folder(req.path, req.name)

@app.get("/api/files/download")
def api_download_file(path: str, user: dict = Depends(get_current_user)):
    return get_file_for_download(path)

@app.delete("/api/files")
def api_delete_file(path: str, user: dict = Depends(get_current_user)):
    return delete_path(path)

# ----------------- Frontend & Static Routes -----------------
# In the container nginx serves these directly; kept for running the backend standalone.

@app.get("/")
async def root_index(request: Request):
    if AUTH_MODE != "none":
        try:
            get_current_user(request)
        except HTTPException:
            return RedirectResponse("/login.html")
    return FileResponse(FRONTEND_DIR / "index.html")

@app.get("/login.html")
async def login_page(request: Request):
    if AUTH_MODE == "none":
        return RedirectResponse("/")
    try:
        get_current_user(request)
        return RedirectResponse("/")
    except HTTPException:
        return FileResponse(FRONTEND_DIR / "login.html")

# Mount frontend assets (CSS, JS, images)
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")
