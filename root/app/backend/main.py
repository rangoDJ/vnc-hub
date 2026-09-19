import os
import json
import uuid
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Request, Response, UploadFile, File, Form
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import (
    AUTH_MODE,
    BASIC_AUTH_USER,
    BASIC_AUTH_PASSWORD,
    OIDC_ISSUER_URL,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_REDIRECT_URI,
    OIDC_SCOPES,
    get_oidc_config,
    get_current_user,
    create_session_cookie,
    clear_session_cookie
)
from rdp_manager import rdp_manager
from file_manager import (
    list_files,
    save_uploaded_file,
    get_file_for_download,
    delete_path
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("server")

app = FastAPI(title="Selkies RDP Gateway", version="1.0.0")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
PROFILES_FILE = CONFIG_DIR / "profiles.json"
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", "/app/frontend"))

# Pydantic models
class RDPProfile(BaseModel):
    id: Optional[str] = None
    name: str
    host: str
    port: int = 3389
    username: Optional[str] = ""
    password: Optional[str] = ""
    domain: Optional[str] = ""
    resolution: str = "dynamic" # dynamic, 1920x1080, 2560x1440, 3840x2160
    enable_audio: bool = True
    enable_clipboard: bool = True
    enable_drive: bool = True
    ignore_cert: bool = True

class ConnectRequest(BaseModel):
    profile_id: Optional[str] = None
    custom: Optional[RDPProfile] = None

class KeyActionRequest(BaseModel):
    key: str # "ctrl_alt_del", "super", "alt_tab"

class LoginRequest(BaseModel):
    username: str
    password: str

# Helper functions for profiles storage
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
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=2)

# ----------------- Authentication Routes -----------------

@app.get("/auth/status")
async def auth_status(request: Request):
    try:
        user = get_current_user(request)
        return {"authenticated": True, "user": user["username"], "auth_mode": AUTH_MODE}
    except HTTPException:
        return {"authenticated": False, "user": None, "auth_mode": AUTH_MODE}

@app.post("/auth/login")
async def basic_login(req: LoginRequest, response: Response):
    if AUTH_MODE != "basic":
        raise HTTPException(status_code=400, detail=f"Basic auth is not enabled (mode is {AUTH_MODE})")
    
    if req.username == BASIC_AUTH_USER and req.password == BASIC_AUTH_PASSWORD:
        user_data = {"username": req.username, "auth_mode": "basic"}
        create_session_cookie(response, user_data)
        return {"success": True, "message": "Login successful"}
    
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

@app.get("/auth/login/oidc")
async def oidc_login():
    if AUTH_MODE != "oidc":
        raise HTTPException(status_code=400, detail="OIDC authentication is not enabled")
    
    config = await get_oidc_config()
    auth_endpoint = config.get("authorization_endpoint")
    if not auth_endpoint:
        raise HTTPException(status_code=500, detail="Authorization endpoint not found in OIDC provider")
    
    import urllib.parse
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": OIDC_SCOPES,
        "state": uuid.uuid4().hex
    }
    url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)

@app.get("/auth/callback")
async def oidc_callback(code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(f"/login.html?error={error or 'missing_code'}")
    
    import httpx
    config = await get_oidc_config()
    token_endpoint = config.get("token_endpoint")
    userinfo_endpoint = config.get("userinfo_endpoint")

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
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
            return RedirectResponse("/login.html?error=token_exchange_failed")
        
        tokens = token_resp.json()
        access_token = tokens.get("access_token")
        
        # Fetch user info
        username = "authentik_user"
        email = ""
        if userinfo_endpoint and access_token:
            user_resp = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if user_resp.status_code == 200:
                uinfo = user_resp.json()
                username = uinfo.get("preferred_username") or uinfo.get("email") or uinfo.get("name", "user")
                email = uinfo.get("email", "")

    response = RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    create_session_cookie(response, {"username": username, "email": email, "auth_mode": "oidc"})
    return response

@app.post("/auth/logout")
@app.get("/auth/logout")
async def logout(response: Response):
    clear_session_cookie(response)
    return RedirectResponse("/login.html", status_code=status.HTTP_302_FOUND)

@app.get("/auth/verify")
async def auth_verify(request: Request):
    """Endpoint called by Nginx auth_request to protect Selkies stream."""
    try:
        get_current_user(request)
        return Response(status_code=200)
    except HTTPException:
        return Response(status_code=401)

# ----------------- Profiles API -----------------

@app.get("/api/profiles")
def get_profiles(user: dict = Depends(get_current_user)):
    profiles = load_profiles()
    # Mask passwords when returning profiles list
    masked = []
    for p in profiles:
        cp = p.copy()
        if cp.get("password"):
            cp["has_password"] = True
            cp["password"] = "••••••••"
        else:
            cp["has_password"] = False
        masked.append(cp)
    return masked

@app.post("/api/profiles")
def save_profile(profile: RDPProfile, user: dict = Depends(get_current_user)):
    profiles = load_profiles()
    if not profile.id:
        profile.id = str(uuid.uuid4())
        profiles.append(profile.dict())
    else:
        for idx, existing in enumerate(profiles):
            if existing["id"] == profile.id:
                # Retain existing password if omitted or masked in update
                if profile.password == "••••••••":
                    profile.password = existing.get("password", "")
                profiles[idx] = profile.dict()
                break
        else:
            profiles.append(profile.dict())

    save_profiles(profiles)
    return {"success": True, "id": profile.id}

@app.delete("/api/profiles/{profile_id}")
def remove_profile(profile_id: str, user: dict = Depends(get_current_user)):
    profiles = load_profiles()
    profiles = [p for p in profiles if p["id"] != profile_id]
    save_profiles(profiles)
    return {"success": True}

# ----------------- RDP Session Control API -----------------

@app.post("/api/session/connect")
def connect_session(req: ConnectRequest, user: dict = Depends(get_current_user)):
    config_dict = None
    if req.profile_id:
        profiles = load_profiles()
        for p in profiles:
            if p["id"] == req.profile_id:
                config_dict = p
                break
        if not config_dict:
            raise HTTPException(status_code=404, detail="Profile not found")
    elif req.custom:
        config_dict = req.custom.dict()
    else:
        raise HTTPException(status_code=400, detail="Missing connection parameters")

    result = rdp_manager.connect(config_dict)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result

@app.post("/api/session/disconnect")
def disconnect_session(user: dict = Depends(get_current_user)):
    return rdp_manager.disconnect()

@app.get("/api/session/status")
def session_status(user: dict = Depends(get_current_user)):
    return rdp_manager.get_status()

@app.post("/api/session/send-keys")
def session_send_keys(req: KeyActionRequest, user: dict = Depends(get_current_user)):
    result = rdp_manager.send_keys(req.key)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result

# ----------------- Shared Files API -----------------

@app.get("/api/files")
def api_list_files(path: str = "", user: dict = Depends(get_current_user)):
    return list_files(path)

@app.post("/api/files/upload")
async def api_upload_file(file: UploadFile = File(...), path: str = Form(""), user: dict = Depends(get_current_user)):
    return await save_uploaded_file(file, path)

@app.get("/api/files/download")
def api_download_file(path: str, user: dict = Depends(get_current_user)):
    return get_file_for_download(path)

@app.delete("/api/files")
def api_delete_file(path: str, user: dict = Depends(get_current_user)):
    return delete_path(path)

# ----------------- Frontend & Static Routes -----------------

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
