# Selkies RDP Gateway with WebUI, Multi-GPU, File Sharing & Authentik SSO

A high-performance, low-latency WebRTC remote desktop container that connects to Windows machines via **FreeRDP 3** and streams the display directly to your browser using **Selkies**. Equipped with a modern **WebUI configuration dashboard**, persistent profile storage, **two-way file sharing**, and **Authentik SSO / Basic Auth**.

---

## Key Features

- ⚡ **Ultra-Low Latency Streaming**: Powered by **Selkies-GStreamer** WebRTC, delivering responsive 60fps streaming with minimal lag.
- 🎯 **Multi-Encoder Acceleration**:
  - **CPU Software Encoding**: Default fallback (`x264` / `openh264`) that runs on any host.
  - **AMD & Intel GPU Acceleration**: Hardware VA-API encoding via `/dev/dri`.
  - **NVIDIA GPU Acceleration**: Hardware NVENC encoding via `nvidia-container-toolkit`.
- 🔐 **Comprehensive Authentication**:
  - **Single Sign-On (SSO)**: Native OIDC with **Authentik**, Keycloak, Google Workspace, Microsoft Entra ID, or Okta.
  - **Forward Auth / Reverse Proxy**: Out-of-the-box header trust for Authentik Proxy Outpost, Traefik, Authelia, and Cloudflare Access.
  - **Basic Auth**: Standard username and password protection with secure signed session cookies.
- 📁 **Bidirectional File Sharing**:
  - FreeRDP drive redirection mounts the container's `/shared` directory as `\\tsclient\SharedFolder` in Windows.
  - Integrated **WebUI File Manager drawer** for drag-and-drop file uploads and instant downloads from the remote Windows machine.
- 🔊 **Audio Playback**: FreeRDP sound redirection (`/sound:sys:pulse`) routed through PulseAudio and WebRTC directly to your browser.
- 📋 **Clipboard Synchronization**: Bidirectional text and clipboard sync between local browser and Windows, with an on-screen clipboard helper.
- 🎮 **In-Stream Floating Toolbar**:
  - One-click **Disconnect** (terminates RDP session cleanly and returns to the dashboard).
  - Special key injection: **Ctrl + Alt + Del**, **Windows Key (⊞)**, and **Alt + Tab**.
  - Slide-out drawers for **Files** and **Clipboard**.
  - True fullscreen toggle.

---

## Architecture Overview

```mermaid
flowchart TD
    subgraph Client["Client Browser"]
        WebUI["WebUI Dashboard & File Manager"]
        Stream["Embedded Selkies WebRTC Stream"]
    end

    subgraph Container["Selkies RDP Docker Container (:8080)"]
        subgraph WebProxy["Nginx Reverse Proxy & Auth Guard"]
            API["FastAPI Backend (Session & File Controller)"]
            AuthEngine["Auth Module (Basic / OIDC / Forward Auth)"]
            Files["/shared (Shared Files) & /config (Profiles)"]
        end

        subgraph StreamingEngine["Selkies Streaming Core"]
            X11["Virtual X11 Display (:1)"]
            Pulse["PulseAudio Server"]
            GStreamer["GStreamer Pipeline"]
            Encoders{"Encoder Selection"}
        end

        subgraph FreeRDPClient["FreeRDP 3 (xfreerdp)"]
            RDPProcess["xfreerdp /v:host /sound /clipboard /drive:SharedFolder,/shared"]
        end
    end

    subgraph Hardware["Host Hardware"]
        CPU["CPU Software (x264)"]
        NV["NVIDIA (NVENC)"]
        AMD["AMD / Intel (VA-API)"]
    end

    subgraph Windows["Remote Target"]
        Win["Windows PC (Port 3389 / RDP)"]
    end

    Client -->|HTTP / WebRTC| WebProxy
    WebProxy --> API
    API --> AuthEngine
    API --> Files
    API -->|Spawns & Monitors| RDPProcess

    GStreamer --> Encoders
    Encoders --> CPU
    Encoders --> NV
    Encoders --> AMD

    RDPProcess -->|Renders Display| X11
    RDPProcess -->|Redirects Audio| Pulse
    RDPProcess -->|Mounts Shared Folder| Files
    RDPProcess -->|RDP Protocol (TLS/NLA)| Win

    X11 --> GStreamer
    Pulse --> GStreamer
```

---

## Quick Start

### 1. Clone & Prepare
```bash
git clone https://github.com/rangoDJ/vnc-hub.git
cd vnc-hub
cp .env.example .env
```

### 2. Launch the Container
The image `ghcr.io/rangodj/vnc-hub:latest` (linux/amd64 + linux/arm64) is built by GitHub Actions on every push to `main`. Tags `sha-<commit>` and, for `vX.Y.Z` git tags, `X.Y.Z` / `X.Y` are also published.
```bash
docker compose pull
docker compose up -d
```

Access the WebUI at:
```
http://<your-server-ip>:8080
https://<your-server-ip>:8443   (self-signed; required for browser clipboard access)
```

---

## Hardware Acceleration (GPU Setup)

The container uses `SELKIES_ENCODER=h264enc`, which automatically detects and uses hardware encoders (NVENC or VA-API) if passed, and cleanly falls back to CPU software encoding (`x264`) if not.

### Profile A: CPU Software Encoding (Default)
No extra configuration required! The default `docker-compose.yml` runs anywhere without GPU requirements.

### Profile B: AMD Radeon or Intel Iris/Arc/UHD (VA-API)
1. Ensure the user running Docker is in the `video` and `render` groups.
2. In `docker-compose.yml`, uncomment the `devices:` block and the `DRINODE` / `DRI_NODE` lines inside the existing `environment:` list (don't add a second `environment:` key — YAML would discard the first one):
```yaml
devices:
  - /dev/dri:/dev/dri
```

### Profile C: NVIDIA GPU (NVENC)
1. Install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host.
2. In `docker-compose.yml`, uncomment the `deploy:` block and the `NVIDIA_*` lines inside the existing `environment:` list:
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```

---

## Authentication Configuration

Edit `.env` to select your preferred authentication model:

### Mode 1: No Authentication (`AUTH_MODE=none`)
Direct open access to the dashboard. Ideal for isolated local subnets or systems behind Tailscale/WireGuard.
```ini
AUTH_MODE=none
```

### Mode 2: Basic Authentication (`AUTH_MODE=basic`)
Form-based login using credentials configured in your `.env`:
```ini
AUTH_MODE=basic
BASIC_AUTH_USER=admin
BASIC_AUTH_PASSWORD=YourSecurePasswordHere!
```
There is no default password: logins are rejected until `BASIC_AUTH_PASSWORD` is set. After 10 failed attempts an IP is locked out for 15 minutes.

### Mode 3: Authentik Single Sign-On (OIDC)
Direct integration with Authentik via OpenID Connect:

1. **In Authentik Admin Interface**:
   - Go to **Applications** -> **Providers** -> **Create Provider**.
   - Type: **OAuth2/OpenID Provider**.
   - Name: `Selkies RDP`.
   - Client type: **Confidential**.
   - Redirect URIs: `http://<your-server-ip-or-domain>:8080/auth/callback`.
   - Scopes: ensure `openid`, `email`, `profile` are selected.
   - Note the **Client ID** and **Client Secret**.
   - Go to **Applications** -> **Create Application**.
   - Attach the application to your new `Selkies RDP` provider.
2. **In `.env`**:
```ini
AUTH_MODE=oidc
OIDC_ISSUER_URL=https://authentik.yourdomain.com/application/o/selkies-rdp/
OIDC_CLIENT_ID=your_authentik_client_id
OIDC_CLIENT_SECRET=your_authentik_client_secret
OIDC_REDIRECT_URI=http://<your-server-ip-or-domain>:8080/auth/callback
```

### Mode 4: Authentik Proxy Outpost / Forward Auth (`AUTH_MODE=forward_auth`)
If you place this container behind an Authentik Embedded/Proxy Outpost, Traefik ForwardAuth, Authelia, or Cloudflare Access:
```ini
AUTH_MODE=forward_auth
FORWARD_AUTH_HEADER=X-authentik-username
FORWARD_AUTH_TRUSTED_PROXIES=172.18.0.0/16
```
The application will automatically recognize the authenticated user and grant access without a secondary login prompt.

`FORWARD_AUTH_TRUSTED_PROXIES` is **required**: the header is only honoured when the request comes from one of these IPs/CIDRs (your proxy's address or Docker network). Otherwise anyone reaching the container port directly could forge the header. Also avoid publishing the container port publicly in this mode.

---

## How File Sharing Works

File sharing between your client browser and the remote Windows machine is bidirectional and automatic:

1. **In Windows**:
   - Open **This PC** (File Explorer).
   - Under **Network locations** / **Redirected drives**, you will see **`SharedFolder on <client>`** (path `\\tsclient\SharedFolder`).
   - Any files placed into this folder in Windows are saved into the container's `/shared` directory.
2. **In the WebUI**:
   - Click the **📁 Shared Files** button in the top navigation bar or the in-stream floating toolbar.
   - **Upload**: Drag-and-drop any file into the drawer to make it instantly accessible inside Windows.
   - **Download**: Click **Download** next to any file saved from Windows.
3. **On the Docker Host**:
   - Files are stored on your host in `./shared` (mounted volume).

---

## Windows Machine Setup

To connect to a Windows machine:
1. On the Windows computer, go to **Settings** -> **System** -> **Remote Desktop** and toggle **Enable Remote Desktop** to **On**.
2. If connecting with a local account without a password, Windows Remote Desktop may block it by default. Ensure the account has a password.
3. If Network Level Authentication (NLA) is enabled (default), provide the valid Windows Username and Password in the WebUI.
4. If using a self-signed certificate on Windows (standard for Windows Pro), keep the **🛡️ Bypass SSL/NLA Warnings** checkbox enabled.

---

## Directory Structure

```
selkies-vnc/
├── Dockerfile                        # Multi-stage image with FreeRDP 3, VA-API & Python
├── docker-compose.yml                # CPU, AMD/Intel VA-API, and NVIDIA profiles
├── .env.example                      # Template for authentication and GPU settings
├── .gitignore
├── root/
│   ├── defaults/
│   │   ├── autostart                 # Virtual X11 session desktop startup
│   │   └── default.conf              # Nginx proxy for WebUI, API, and Selkies stream
│   ├── etc/
│   │   └── s6-overlay/s6-rc.d/
│   │       ├── 02-rdp-webui/         # FastAPI backend service supervisor
│   │       └── user/contents.d/
│   └── app/
│       ├── backend/
│       │   ├── main.py               # FastAPI router and static server
│       │   ├── auth.py               # Basic Auth, Authentik OIDC & Forward Auth
│       │   ├── rdp_manager.py        # FreeRDP process supervisor & key injector
│       │   ├── file_manager.py       # Upload/download API for /shared
│       │   └── requirements.txt
│       └── frontend/
│           ├── index.html            # Single-Page App (Dashboard & Stream View)
│           ├── login.html            # Login portal (Basic Auth / Authentik SSO)
│           ├── css/style.css         # Dark glassmorphism styling
│           └── js/app.js             # Client state, WebRTC iframe, and toolbar controls
├── config/                           # Persistent volume: saved profiles and secrets
├── shared/                           # Persistent volume: shared files with Windows
└── README.md
```

---

## Troubleshooting

- **Black screen after clicking Connect**:
  Check the **Connection Logs & Diagnostics** accordion on the dashboard. It displays live output from `xfreerdp3`. Common reasons include incorrect Windows credentials or Remote Desktop not being enabled on the target PC.
- **Audio not playing**:
  Ensure the **🔊 Audio Playback** toggle is enabled before connecting, and that your browser allows autoplay on the gateway URL.
- **Ctrl + Alt + Del**:
  Click the **Ctrl+Alt+Del** button on the floating stream toolbar. It sends the key sequence (`Ctrl+Alt+End`) recognized by FreeRDP to trigger the Windows security screen without triggering your local host's task manager.
