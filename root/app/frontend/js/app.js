// State
let currentStatus = "disconnected";
let isStreamView = false;
let statusPollTimer = null;
let savedProfiles = [];
let currentFilePath = "";

// DOM Elements
const dashboardView = document.getElementById("dashboard-view");
const streamView = document.getElementById("stream-view");
const selkiesIframe = document.getElementById("selkies-iframe");
const btnToggleView = document.getElementById("btn-toggle-view");
const toggleViewText = document.getElementById("toggle-view-text");
const sessionBadge = document.getElementById("session-badge");
const badgeStatusText = document.getElementById("badge-status-text");
const navTargetHost = document.getElementById("nav-target-host");
const userProfile = document.getElementById("user-profile");
const userDisplayName = document.getElementById("user-display-name");

// Form Elements
const rdpForm = document.getElementById("rdp-config-form");
const profileIdInput = document.getElementById("profile-id");
const profileNameInput = document.getElementById("profile-name");
const hostInput = document.getElementById("host");
const portInput = document.getElementById("port");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const domainInput = document.getElementById("domain");
const resolutionSelect = document.getElementById("resolution");
const scaleSelect = document.getElementById("scale");
const enableAudioCheck = document.getElementById("enable-audio");
const enableClipboardCheck = document.getElementById("enable-clipboard");
const enableDriveCheck = document.getElementById("enable-drive");
const ignoreCertCheck = document.getElementById("ignore-cert");
const viewOnlyCheck = document.getElementById("view-only");
const sshKeyInput = document.getElementById("ssh-key");
const fontSizeSelect = document.getElementById("font-size");
const passwordLabel = document.getElementById("password-label");

const DEFAULT_PORTS = { rdp: 3389, vnc: 5900, ssh: 22 };
const PROTOCOL_UI = {
    rdp: { password: "Password", username: "Administrator", host: "192.168.1.150 or pc.local" },
    vnc: { password: "VNC Password", username: "", host: "192.168.1.50 or nas.local" },
    ssh: { password: "Password", username: "root", host: "192.168.1.10 or server.local" },
};
const btnSaveProfile = document.getElementById("btn-save-profile");
const btnResetForm = document.getElementById("btn-reset-form");
const connectionAlert = document.getElementById("connection-alert");
const profilesList = document.getElementById("profiles-list");
const profilesCount = document.getElementById("profiles-count");
const logsContent = document.getElementById("logs-content");
const logsToggle = document.getElementById("logs-toggle");
const logsBody = document.getElementById("logs-body");

// Drawers
const fileDrawer = document.getElementById("file-drawer");
const clipboardDrawer = document.getElementById("clipboard-drawer");
const drawerBackdrop = document.getElementById("drawer-backdrop");
const btnOpenFiles = document.getElementById("btn-open-files");
const btnCloseFiles = document.getElementById("btn-close-files");
const btnRefreshFiles = document.getElementById("btn-refresh-files");
const btnFilesUp = document.getElementById("btn-files-up");
const btnNewFolder = document.getElementById("btn-new-folder");
const fileBreadcrumb = document.getElementById("file-breadcrumb");
const fileAlert = document.getElementById("file-alert");
const btnOpenClipboard = document.getElementById("btn-open-clipboard");
const btnCloseClipboard = document.getElementById("btn-close-clipboard");
const clipStatus = document.getElementById("clip-status");

// Floating Toolbar Elements
const streamToolbar = document.getElementById("stream-toolbar");
const tbBtnDisconnect = document.getElementById("tb-btn-disconnect");
const tbBtnDashboard = document.getElementById("tb-btn-dashboard");
const tbBtnCad = document.getElementById("tb-btn-cad");
const tbBtnSuper = document.getElementById("tb-btn-super");
const tbBtnAltTab = document.getElementById("tb-btn-alt-tab");
const tbBtnFiles = document.getElementById("tb-btn-files");
const tbBtnClipboard = document.getElementById("tb-btn-clipboard");
const tbBtnFullscreen = document.getElementById("tb-btn-fullscreen");
const tbBtnCollapse = document.getElementById("tb-btn-collapse");
const toolbarExpand = document.getElementById("toolbar-expand");
const TOOLBAR_COLLAPSED_KEY = "vnchub.toolbarCollapsed";
const NAV_COLLAPSED_KEY = "vnchub.navCollapsed";

// Initialize Application
async function initApp() {
    setupEventListeners();
    await checkAuth();
    await loadProfiles();
    startStatusPolling();
}

// Fetch wrapper: sends the user to the login page when the session has expired
async function apiFetch(url, options = {}) {
    const res = await fetch(url, options);
    if (res.status === 401) {
        window.location.href = "/login.html";
        throw new Error("Not authenticated");
    }
    return res;
}

// Turn a FastAPI error body (string or validation error list) into readable text
async function errorMessage(res, fallback) {
    try {
        const data = await res.json();
        if (typeof data.detail === "string") return data.detail;
        if (Array.isArray(data.detail)) {
            return data.detail.map(d => `${(d.loc || []).slice(-1)[0] || "field"}: ${d.msg}`).join("; ");
        }
    } catch (e) {
        // non-JSON body
    }
    return fallback;
}

// Check Authentication Status
async function checkAuth() {
    try {
        const res = await fetch("/auth/status");
        if (!res.ok) return;
        const data = await res.json();
        if (data.authenticated && data.auth_mode !== "none") {
            userProfile.classList.remove("hidden");
            userDisplayName.textContent = data.user;
        }
    } catch (e) {
        console.warn("Could not check auth status", e);
    }
}

// Setup Event Listeners
function setupEventListeners() {
    // Form submission (Connect)
    document.querySelectorAll('input[name="protocol"]').forEach(r => r.addEventListener("change", onProtocolChange));
    applyProtocol();

    rdpForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        await handleConnect();
    });

    // Save profile button
    btnSaveProfile.addEventListener("click", async () => {
        await handleSaveProfile();
    });

    // Reset form
    btnResetForm.addEventListener("click", () => {
        resetForm();
    });

    // Toggle password visibility
    const btnTogglePassword = document.getElementById("btn-toggle-password");
    btnTogglePassword.addEventListener("click", () => {
        passwordInput.type = passwordInput.type === "password" ? "text" : "password";
    });

    // Switch between Dashboard & Stream
    btnToggleView.addEventListener("click", () => {
        toggleView();
    });

    // Toolbar buttons
    tbBtnDisconnect.addEventListener("click", handleDisconnect);
    tbBtnDashboard.addEventListener("click", () => switchView(false));
    tbBtnCad.addEventListener("click", () => sendSpecialKey("ctrl_alt_del"));
    tbBtnSuper.addEventListener("click", () => sendSpecialKey("super"));
    tbBtnAltTab.addEventListener("click", () => sendSpecialKey("alt_tab"));
    tbBtnFiles.addEventListener("click", () => {
        openDrawer(fileDrawer);
        loadFiles();
    });
    tbBtnClipboard.addEventListener("click", () => openDrawer(clipboardDrawer));
    tbBtnFullscreen.addEventListener("click", toggleFullscreen);
    tbBtnCollapse.addEventListener("click", () => setToolbarCollapsed(true));
    toolbarExpand.addEventListener("click", () => setToolbarCollapsed(false));
    setToolbarCollapsed(loadPref(TOOLBAR_COLLAPSED_KEY));
    document.getElementById("btn-collapse-nav").addEventListener("click", () => setNavCollapsed(true));
    document.getElementById("nav-expand").addEventListener("click", () => setNavCollapsed(false));
    setNavCollapsed(loadPref(NAV_COLLAPSED_KEY));
    setupToolbarDrag();

    // Logs accordion toggle
    logsToggle.addEventListener("click", () => {
        logsBody.classList.toggle("hidden");
    });

    // Drawers
    btnOpenFiles.addEventListener("click", () => {
        openDrawer(fileDrawer);
        loadFiles();
    });
    btnCloseFiles.addEventListener("click", () => closeDrawer(fileDrawer));
    btnRefreshFiles.addEventListener("click", () => loadFiles());
    btnFilesUp.addEventListener("click", () => {
        const parts = currentFilePath.split("/").filter(Boolean);
        parts.pop();
        loadFiles(parts.join("/"));
    });
    btnNewFolder.addEventListener("click", handleNewFolder);

    btnOpenClipboard.addEventListener("click", () => openDrawer(clipboardDrawer));
    btnCloseClipboard.addEventListener("click", () => closeDrawer(clipboardDrawer));
    drawerBackdrop.addEventListener("click", closeAllDrawers);

    // File Upload Zone
    setupFileUpload();

    // Clipboard Send: push to the remote session's clipboard, and the local one when the browser allows it
    document.getElementById("btn-clip-send").addEventListener("click", async () => {
        const text = document.getElementById("clip-text").value;
        if (!text) return;
        const results = [];
        try {
            const res = await apiFetch("/api/session/clipboard", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text })
            });
            results.push(res.ok ? "Sent to Windows clipboard." : `Remote clipboard failed: ${await errorMessage(res, "unknown error")}`);
        } catch (e) {
            results.push("Remote clipboard failed: network error.");
        }
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                results.push("Copied locally.");
            } catch (err) {
                // local copy is a convenience only
            }
        }
        clipStatus.textContent = results.join(" ");
        clipStatus.classList.remove("hidden");
    });

    document.getElementById("btn-clip-clear").addEventListener("click", () => {
        document.getElementById("clip-text").value = "";
        clipStatus.classList.add("hidden");
    });
}

// ----------------- Connection & Session Management -----------------

function getProtocol() {
    const checked = document.querySelector('input[name="protocol"]:checked');
    return checked ? checked.value : "rdp";
}

function setProtocol(protocol) {
    const radio = document.querySelector(`input[name="protocol"][value="${protocol}"]`);
    if (radio) radio.checked = true;
    applyProtocol();
}

// Show only the fields that apply to the selected protocol (elements tagged data-for="rdp ssh")
function applyProtocol() {
    const protocol = getProtocol();
    document.querySelectorAll("#rdp-config-form [data-for]").forEach(el => {
        el.classList.toggle("hidden", !el.dataset.for.split(" ").includes(protocol));
    });
    const ui = PROTOCOL_UI[protocol];
    passwordLabel.textContent = ui.password;
    usernameInput.placeholder = ui.username;
    hostInput.placeholder = ui.host;
    portInput.placeholder = DEFAULT_PORTS[protocol];
}

function onProtocolChange() {
    // A port left at another protocol's default follows the switch; custom ports are kept
    if (Object.values(DEFAULT_PORTS).includes(parseInt(portInput.value))) portInput.value = "";
    applyProtocol();
}

function getFormConfig() {
    return {
        id: profileIdInput.value || null,
        protocol: getProtocol(),
        name: profileNameInput.value.trim() || hostInput.value.trim(),
        host: hostInput.value.trim(),
        port: parseInt(portInput.value) || null, // null = protocol default
        username: usernameInput.value.trim(),
        password: passwordInput.value,
        domain: domainInput.value.trim(),
        resolution: resolutionSelect.value,
        scale: scaleSelect.value,
        enable_audio: enableAudioCheck.checked,
        enable_clipboard: enableClipboardCheck.checked,
        enable_drive: enableDriveCheck.checked,
        ignore_cert: ignoreCertCheck.checked,
        view_only: viewOnlyCheck.checked,
        ssh_key: sshKeyInput.value,
        font_size: parseInt(fontSizeSelect.value) || 12
    };
}

async function handleConnect(profileId = null) {
    hideAlert();
    let body = {};
    if (profileId) {
        body = { profile_id: profileId };
    } else {
        const custom = getFormConfig();
        if (!custom.host) {
            showAlert("Please provide a Windows host IP or address", "error");
            return;
        }
        body = { custom: custom };
    }
    // Lets the server match Windows display scaling to this screen when the profile uses "auto"
    body.device_pixel_ratio = window.devicePixelRatio || 1;

    try {
        const res = await apiFetch("/api/session/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        if (res.ok) {
            currentStatus = "connecting";
            const wasLoaded = isStreamLoaded();
            switchView(true); // Switch to stream view (loads the stream if it isn't yet)
            if (wasLoaded) reloadIframe(); // rebind an already-open stream to the new session
        } else {
            showAlert(await errorMessage(res, "Failed to initiate RDP session"), "error");
        }
    } catch (e) {
        showAlert("Network error trying to connect", "error");
    }
}

async function handleDisconnect() {
    try {
        await apiFetch("/api/session/disconnect", { method: "POST" });
        switchView(false); // Return to dashboard
        unloadStream();
    } catch (e) {
        console.error("Disconnect error", e);
    }
}

async function sendSpecialKey(key) {
    try {
        const res = await apiFetch("/api/session/send-keys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key })
        });
        if (!res.ok) {
            console.warn("Send keys failed:", await errorMessage(res, "unknown error"));
        }
    } catch (e) {
        console.error("Send keys error:", e);
    }
}

// ----------------- Status Polling -----------------

function startStatusPolling() {
    if (statusPollTimer) clearInterval(statusPollTimer);
    pollStatus();
    statusPollTimer = setInterval(pollStatus, 2000);
}

async function pollStatus() {
    try {
        const res = await apiFetch("/api/session/status");
        if (!res.ok) return;
        const data = await res.json();
        const previousStatus = currentStatus;
        updateStatusBadge(data.status, data.target, data.protocol);
        // Ctrl+Alt+Del / Win / Alt+Tab only make sense for graphical desktops
        document.querySelectorAll("[data-keys-group]").forEach(el => {
            el.classList.toggle("hidden", data.protocol === "ssh");
        });

        // Update logs
        if (data.recent_logs && data.recent_logs.length > 0) {
            logsContent.textContent = data.recent_logs.join("\n");
        }

        // Show/hide view toggle button based on whether connected
        if (data.status === "connected" || data.status === "connecting") {
            btnToggleView.classList.remove("hidden");
        } else {
            btnToggleView.classList.add("hidden");
            if (isStreamView) switchView(false);
            unloadStream();
            // Explain why the session ended instead of silently returning to the dashboard
            if (data.status === "error" && (previousStatus === "connecting" || previousStatus === "connected")) {
                showAlert(`Connection failed: ${data.last_error || "unknown error"} (see Connection Logs)`, "error");
            }
        }
    } catch (e) {
        // Silent poll fail
    }
}

function updateStatusBadge(status, target, protocol) {
    currentStatus = status;
    sessionBadge.className = "badge";
    badgeStatusText.textContent = status;

    if (status === "connected") {
        sessionBadge.classList.add("badge-connected");
        if (target) {
            navTargetHost.textContent = protocol ? `${protocol.toUpperCase()} · ${target}` : target;
            navTargetHost.classList.remove("hidden");
        }
    } else if (status === "connecting") {
        sessionBadge.classList.add("badge-connecting");
    } else if (status === "error") {
        sessionBadge.classList.add("badge-error");
        navTargetHost.classList.add("hidden");
    } else {
        sessionBadge.classList.add("badge-idle");
        navTargetHost.classList.add("hidden");
    }
}

// ----------------- Profiles Management -----------------

async function loadProfiles() {
    try {
        const res = await apiFetch("/api/profiles");
        if (!res.ok) return;
        savedProfiles = await res.json();
        renderProfiles();
    } catch (e) {
        console.error("Failed to load profiles", e);
    }
}

function renderProfiles() {
    profilesCount.textContent = savedProfiles.length;
    if (savedProfiles.length === 0) {
        profilesList.innerHTML = `<div class="empty-state">No saved profiles yet. Configure and click "Save Profile" above.</div>`;
        return;
    }

    profilesList.innerHTML = savedProfiles.map(p => {
        const protocol = DEFAULT_PORTS[p.protocol] ? p.protocol : "rdp";
        return `
        <div class="profile-card" data-id="${escapeHtml(p.id)}">
            <div class="profile-info">
                <h4><span class="protocol-tag ${protocol}">${protocol.toUpperCase()}</span>${escapeHtml(p.name || p.host)}</h4>
                <p>${escapeHtml(p.username ? p.username + '@' : '')}${escapeHtml(p.host)}:${escapeHtml(p.port || DEFAULT_PORTS[protocol])}${profileDetail(p, protocol)}</p>
            </div>
            <div class="profile-actions">
                <button class="btn btn-primary btn-sm btn-prof-connect" data-id="${escapeHtml(p.id)}">Connect</button>
                <button class="btn btn-secondary btn-sm btn-prof-edit" data-id="${escapeHtml(p.id)}">Edit</button>
                <button class="btn btn-danger btn-sm btn-prof-del" data-id="${escapeHtml(p.id)}">✕</button>
            </div>
        </div>
    `;
    }).join("");

    // Attach listeners to profile action buttons
    document.querySelectorAll(".btn-prof-connect").forEach(b => {
        b.addEventListener("click", () => handleConnect(b.dataset.id));
    });
    document.querySelectorAll(".btn-prof-edit").forEach(b => {
        b.addEventListener("click", () => editProfile(b.dataset.id));
    });
    document.querySelectorAll(".btn-prof-del").forEach(b => {
        b.addEventListener("click", () => deleteProfile(b.dataset.id));
    });
}

// Short protocol-specific summary shown under a saved profile
function profileDetail(p, protocol) {
    if (protocol === "rdp") {
        const scale = p.scale && p.scale !== "auto" ? escapeHtml(p.scale) + "%" : "auto";
        return ` (${escapeHtml(p.resolution || "dynamic")}, ${scale} scaling)`;
    }
    if (protocol === "vnc") return p.view_only ? " (view only)" : "";
    return p.has_ssh_key ? " (key auth)" : "";
}

async function handleSaveProfile() {
    const config = getFormConfig();
    if (!config.host) {
        showAlert("Host is required to save a profile", "error");
        return;
    }

    try {
        const res = await apiFetch("/api/profiles", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(config)
        });
        if (!res.ok) {
            showAlert(await errorMessage(res, "Failed to save profile"), "error");
            return;
        }
        const data = await res.json();
        // Further saves update this profile instead of creating duplicates
        profileIdInput.value = data.id;
        btnResetForm.classList.remove("hidden");
        document.getElementById("form-title").textContent = `Editing: ${config.name}`;
        await loadProfiles();
        showAlert("Profile saved successfully!", "success");
    } catch (e) {
        showAlert("Failed to save profile", "error");
    }
}

function editProfile(profileId) {
    const p = savedProfiles.find(x => x.id === profileId);
    if (!p) return;
    hideAlert();
    setProtocol(p.protocol || "rdp");
    profileIdInput.value = p.id;
    profileNameInput.value = p.name || "";
    hostInput.value = p.host;
    portInput.value = p.port || "";
    usernameInput.value = p.username || "";
    passwordInput.value = p.password || "";
    domainInput.value = p.domain || "";
    resolutionSelect.value = p.resolution || "dynamic";
    scaleSelect.value = p.scale || "auto";
    enableAudioCheck.checked = p.enable_audio !== false;
    enableClipboardCheck.checked = p.enable_clipboard !== false;
    enableDriveCheck.checked = p.enable_drive !== false;
    ignoreCertCheck.checked = p.ignore_cert !== false;
    viewOnlyCheck.checked = p.view_only === true;
    sshKeyInput.value = p.ssh_key || "";
    fontSizeSelect.value = String(p.font_size || 12);

    btnResetForm.classList.remove("hidden");
    document.getElementById("form-title").textContent = `Editing: ${p.name || p.host}`;
}

async function deleteProfile(profileId) {
    if (!confirm("Are you sure you want to delete this profile?")) return;
    try {
        const res = await apiFetch(`/api/profiles/${encodeURIComponent(profileId)}`, { method: "DELETE" });
        if (!res.ok) {
            showAlert(await errorMessage(res, "Failed to delete profile"), "error");
            return;
        }
        if (profileIdInput.value === profileId) resetForm();
        await loadProfiles();
    } catch (e) {
        console.error("Failed to delete profile", e);
    }
}

function resetForm() {
    rdpForm.reset();
    applyProtocol(); // reset() puts the protocol back to RDP
    profileIdInput.value = "";
    btnResetForm.classList.add("hidden");
    document.getElementById("form-title").textContent = "Connection Configuration";
}

// ----------------- View & Fullscreen Management -----------------

function switchView(toStream) {
    isStreamView = toStream;
    document.body.classList.toggle("in-stream", toStream);
    if (toStream) {
        dashboardView.classList.add("hidden");
        streamView.classList.remove("hidden");
        toggleViewText.textContent = "Back to Dashboard";
        // Load only once visible: Selkies sizes the remote display from the iframe viewport
        if (!isStreamLoaded()) reloadIframe();
    } else {
        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
        streamView.classList.add("hidden");
        dashboardView.classList.remove("hidden");
        toggleViewText.textContent = "Go to Stream";
    }
}

function toggleView() {
    switchView(!isStreamView);
}

function isStreamLoaded() {
    return selkiesIframe.src.includes("/stream/");
}

// Drop the Selkies client when no session is running so it can't hold a hidden, wrongly sized display
function unloadStream() {
    if (isStreamLoaded()) selkiesIframe.src = "about:blank";
}

function reloadIframe() {
    // Force iframe reload to bind cleanly to active session
    selkiesIframe.src = "/stream/?t=" + Date.now();
}

function toggleFullscreen() {
    // Fullscreen the whole stream view so the toolbar stays available
    if (!document.fullscreenElement) {
        streamView.requestFullscreen().catch(err => {
            alert(`Error entering fullscreen: ${err.message}`);
        });
    } else {
        document.exitFullscreen();
    }
}

function loadPref(key) {
    try {
        return localStorage.getItem(key) === "1";
    } catch (e) {
        return false; // storage unavailable (private mode, blocked site data)
    }
}

function savePref(key, value) {
    try {
        localStorage.setItem(key, value ? "1" : "0");
    } catch (e) {
        // preference just won't persist
    }
}

function setToolbarCollapsed(collapsed) {
    streamToolbar.classList.toggle("hidden", collapsed);
    toolbarExpand.classList.toggle("hidden", !collapsed);
    savePref(TOOLBAR_COLLAPSED_KEY, collapsed);
}

// The top bar only hides while the stream view is showing (see body.in-stream in the CSS)
function setNavCollapsed(collapsed) {
    document.body.classList.toggle("nav-collapsed", collapsed);
    savePref(NAV_COLLAPSED_KEY, collapsed);
}

function setupToolbarDrag() {
    const handle = streamToolbar.querySelector(".toolbar-drag-handle");
    let offsetX = 0;
    let offsetY = 0;
    let dragging = false;

    handle.addEventListener("pointerdown", (e) => {
        const rect = streamToolbar.getBoundingClientRect();
        offsetX = e.clientX - rect.left;
        offsetY = e.clientY - rect.top;
        // Switch to fixed pixel positioning so the toolbar follows the pointer
        streamToolbar.style.position = "fixed";
        streamToolbar.style.transform = "none";
        streamToolbar.style.left = `${rect.left}px`;
        streamToolbar.style.top = `${rect.top}px`;
        dragging = true;
        // Capture keeps events flowing even when the pointer passes over the stream iframe
        handle.setPointerCapture(e.pointerId);
        handle.style.cursor = "grabbing";
        e.preventDefault();
    });

    handle.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const maxX = window.innerWidth - streamToolbar.offsetWidth;
        const maxY = window.innerHeight - streamToolbar.offsetHeight;
        streamToolbar.style.left = `${Math.min(Math.max(0, e.clientX - offsetX), maxX)}px`;
        streamToolbar.style.top = `${Math.min(Math.max(0, e.clientY - offsetY), maxY)}px`;
    });

    const stopDrag = (e) => {
        if (!dragging) return;
        dragging = false;
        handle.releasePointerCapture(e.pointerId);
        handle.style.cursor = "";
    };
    handle.addEventListener("pointerup", stopDrag);
    handle.addEventListener("pointercancel", stopDrag);
}

// ----------------- Shared Files Management -----------------

function setupFileUpload() {
    const dropzone = document.getElementById("upload-dropzone");
    const fileInput = document.getElementById("file-input");

    dropzone.addEventListener("click", () => fileInput.click());

    dropzone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropzone.classList.add("drag-over");
    });

    dropzone.addEventListener("dragleave", () => {
        dropzone.classList.remove("drag-over");
    });

    dropzone.addEventListener("drop", async (e) => {
        e.preventDefault();
        dropzone.classList.remove("drag-over");
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            await uploadFiles(e.dataTransfer.files);
        }
    });

    fileInput.addEventListener("change", async () => {
        if (fileInput.files && fileInput.files.length > 0) {
            await uploadFiles(fileInput.files);
        }
        fileInput.value = ""; // allow re-selecting the same file
    });
}

// XHR instead of fetch so we get real byte-level upload progress
function uploadOne(file, overwrite, onProgress) {
    return new Promise((resolve) => {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("path", currentFilePath);
        formData.append("overwrite", overwrite ? "true" : "false");

        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/files/upload");
        xhr.upload.addEventListener("progress", (e) => {
            if (e.lengthComputable) onProgress(e.loaded / e.total);
        });
        xhr.addEventListener("load", () => {
            let detail = null;
            try { detail = JSON.parse(xhr.responseText).detail; } catch (e) { /* ignore */ }
            resolve({ status: xhr.status, detail: typeof detail === "string" ? detail : null });
        });
        xhr.addEventListener("error", () => resolve({ status: 0, detail: "network error" }));
        xhr.send(formData);
    });
}

async function uploadFiles(files) {
    const progressContainer = document.getElementById("upload-progress");
    const progressFill = document.getElementById("progress-fill");
    const progressText = document.getElementById("progress-text");
    const errors = [];

    hideFileAlert();
    progressContainer.classList.remove("hidden");

    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const label = `Uploading ${file.name} (${i + 1}/${files.length})`;
        const onProgress = (fraction) => {
            progressFill.style.width = `${((i + fraction) / files.length) * 100}%`;
            progressText.textContent = `${label} — ${Math.round(fraction * 100)}%`;
        };
        onProgress(0);

        let result = await uploadOne(file, false, onProgress);
        if (result.status === 409 && confirm(`${file.name} already exists. Overwrite it?`)) {
            result = await uploadOne(file, true, onProgress);
        }
        if (result.status === 401) {
            window.location.href = "/login.html";
            return;
        }
        if (result.status < 200 || result.status >= 300) {
            if (result.status !== 409) errors.push(`${file.name}: ${result.detail || "HTTP " + result.status}`);
        }
    }

    setTimeout(() => {
        progressContainer.classList.add("hidden");
        progressFill.style.width = "0%";
    }, 1000);

    if (errors.length) showFileAlert(`Some uploads failed — ${errors.join("; ")}`);
    await loadFiles();
}

async function loadFiles(path = currentFilePath) {
    const table = document.getElementById("file-list-table");
    try {
        const res = await apiFetch(`/api/files?path=${encodeURIComponent(path)}`);
        if (!res.ok) {
            if (path) {
                // Folder vanished (deleted from Windows side?) — fall back to root
                currentFilePath = "";
                return loadFiles("");
            }
            table.innerHTML = `<div class="empty-state">${escapeHtml(await errorMessage(res, "Error loading files"))}</div>`;
            return;
        }
        currentFilePath = path;
        renderBreadcrumb();
        const files = await res.json();

        if (files.length === 0) {
            table.innerHTML = path
                ? `<div class="empty-state">This folder is empty.</div>`
                : `<div class="empty-state">No files in shared folder. Upload files above or save to <code>\\\\tsclient\\SharedFolder</code> in Windows.</div>`;
            return;
        }

        table.innerHTML = files.map(f => `
            <div class="file-item">
                <div class="file-item-info ${f.is_dir ? 'file-item-folder' : ''}" ${f.is_dir ? `data-path="${escapeHtml(f.path)}" title="Open folder"` : ''}>
                    <span>${f.is_dir ? '📁' : '📄'}</span>
                    <div>
                        <strong>${escapeHtml(f.name)}</strong>
                        <div style="font-size: 0.75rem; color: var(--text-muted);">${escapeHtml(f.size_formatted)}</div>
                    </div>
                </div>
                <div>
                    ${!f.is_dir ? `<a href="/api/files/download?path=${encodeURIComponent(f.path)}" class="btn btn-secondary btn-xs" download>Download</a>` : ''}
                    <button class="btn btn-danger btn-xs btn-file-del" data-path="${escapeHtml(f.path)}">✕</button>
                </div>
            </div>
        `).join("");

        table.querySelectorAll(".file-item-folder").forEach(el => {
            el.addEventListener("click", () => loadFiles(el.dataset.path));
        });
        table.querySelectorAll(".btn-file-del").forEach(b => {
            b.addEventListener("click", () => deleteFile(b.dataset.path));
        });
    } catch (e) {
        table.innerHTML = `<div class="empty-state">Error loading files</div>`;
    }
}

function renderBreadcrumb() {
    const parts = currentFilePath.split("/").filter(Boolean);
    fileBreadcrumb.textContent = "SharedFolder" + (parts.length ? " / " + parts.join(" / ") : "");
    btnFilesUp.disabled = parts.length === 0;
}

async function handleNewFolder() {
    const name = prompt("New folder name:");
    if (!name || !name.trim()) return;
    try {
        const res = await apiFetch("/api/files/folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: currentFilePath, name: name.trim() })
        });
        if (!res.ok) {
            showFileAlert(await errorMessage(res, "Could not create folder"));
            return;
        }
        hideFileAlert();
        await loadFiles();
    } catch (e) {
        showFileAlert("Could not create folder");
    }
}

async function deleteFile(path) {
    if (!confirm(`Delete ${path}?`)) return;
    try {
        const res = await apiFetch(`/api/files?path=${encodeURIComponent(path)}`, { method: "DELETE" });
        if (!res.ok) {
            showFileAlert(await errorMessage(res, "Delete failed"));
            return;
        }
        hideFileAlert();
        await loadFiles();
    } catch (e) {
        console.error("Delete error", e);
    }
}

function showFileAlert(message) {
    fileAlert.textContent = message;
    fileAlert.className = "alert-box alert-error";
}

function hideFileAlert() {
    fileAlert.classList.add("hidden");
}

// ----------------- Drawer Helpers -----------------

function openDrawer(drawer) {
    closeAllDrawers();
    drawer.classList.add("open");
    drawerBackdrop.classList.remove("hidden");
}

function closeDrawer(drawer) {
    drawer.classList.remove("open");
    drawerBackdrop.classList.add("hidden");
}

function closeAllDrawers() {
    fileDrawer.classList.remove("open");
    clipboardDrawer.classList.remove("open");
    drawerBackdrop.classList.add("hidden");
}

// ----------------- Utility Helpers -----------------

function showAlert(message, type = "error") {
    connectionAlert.textContent = message;
    connectionAlert.className = `alert-box alert-${type}`;
    connectionAlert.classList.remove("hidden");
}

function hideAlert() {
    connectionAlert.classList.add("hidden");
}

function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// Kickoff
document.addEventListener("DOMContentLoaded", initApp);
