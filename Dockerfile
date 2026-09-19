# syntax=docker/dockerfile:1
FROM ghcr.io/linuxserver/baseimage-selkies:ubuntu-noble

LABEL maintainer="kodi"
LABEL description="Selkies WebRTC remote desktop gateway with FreeRDP, multi-GPU encoding (NVIDIA/AMD/Intel/CPU), Authentik SSO & Basic Auth, and WebUI File Sharing."

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/venv \
    PATH="/app/venv/bin:$PATH"

# Install FreeRDP 3, GPU drivers for VA-API, Python 3, and utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    freerdp3-x11 \
    mesa-va-drivers \
    libva2 \
    libva-drm2 \
    vainfo \
    python3 \
    python3-pip \
    python3-venv \
    procps \
    curl \
    jq \
    feh \
    xdotool \
    xclip \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment for Python FastAPI WebUI & API
RUN python3 -m venv /app/venv

# Install Python backend dependencies
COPY root/app/backend/requirements.txt /tmp/requirements.txt
RUN /app/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# Ensure symlink for xfreerdp exists for universal compatibility
RUN if [ ! -f /usr/bin/xfreerdp ] && [ -f /usr/bin/xfreerdp3 ]; then \
        ln -s /usr/bin/xfreerdp3 /usr/bin/xfreerdp; \
    fi

# Create shared folder for RDP drive redirection & web file manager
RUN mkdir -p /shared /config /app/backend /app/frontend

# Copy root configuration files
COPY root/ /

# Set executable permissions for service scripts and autostart
RUN chmod +x \
    /defaults/autostart \
    /etc/s6-overlay/s6-rc.d/02-rdp-webui/run

# Volumes
VOLUME ["/config", "/shared"]

# Port 3000 (HTTP) and 3001 (HTTPS, self-signed) serve the WebUI, API, and Selkies stream
EXPOSE 3000 3001
