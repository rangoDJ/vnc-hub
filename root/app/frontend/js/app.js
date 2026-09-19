// State
let currentStatus = "disconnected";
let isStreamView = false;
let statusPollTimer = null;
let savedProfiles = [];

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
const enableAudioCheck = document.getElementById("enable-audio");
const enableClipboardCheck = document.getElementById("enable-clipboard");
const enableDriveCheck = document.getElementById("enable-drive");
const ignoreCertCheck = document.getElementById("ignore-cert");
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
const btnOpenClipboard = document.getElementById("btn-open-clipboard");
const btnCloseClipboard = document.getElementById("btn-close-clipboard");

// Floating Toolbar Elements
const tbBtnDisconnect = document.getElementById("tb-btn-disconnect");
const tbBtnDashboard = document.getElementById("tb-btn-dashboard");
const tbBtnCad = document.getElementById("tb-btn-cad");
const tbBtnSuper = document.getElementById("tb-btn-super");
const tbBtnAltTab = document.getElementById("tb-btn-alt-tab");
const tbBtnFiles = document.getElementById("tb-btn-files");
const tbBtnClipboard = document.getElementById("tb-btn-clipboard");
const tbBtnFullscreen = document.getElementById("tb-btn-fullscreen");

// Initialize Application
async function initApp() {
    setupEventListeners();
    await checkAuth();
    await loadProfiles();
    startStatusPolling();
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
    tbBtnFiles.addEventListener("click", () => openDrawer(fileDrawer));
    tbBtnClipboard.addEventListener("click", () => openDrawer(clipboardDrawer));
    tbBtnFullscreen.addEventListener("click", toggleFullscreen);

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
    btnRefreshFiles.addEventListener("click", loadFiles);

    btnOpenClipboard.addEventListener("click", () => openDrawer(clipboardDrawer));
    btnCloseClipboard.addEventListener("click", () => closeDrawer(clipboardDrawer));
    drawerBackdrop.addEventListener("click", closeAllDrawers);

    // File Upload Zone
    setupFileUpload();

    // Clipboard Send
    document.getElementById("btn-clip-send").addEventListener("click", async () => {
        const text = document.getElementById("clip-text").value;
        if (text) {
            try {
                await navigator.clipboard.writeText(text);
                alert("Text copied to clipboard!");
            } catch (err) {
                alert("Could not write to local clipboard: " + err);
            }
        }
    });

    document.getElementById("btn-clip-clear").addEventListener("click", () => {
        document.getElementById("clip-text").value = "";
    });
}

// ----------------- Connection & Session Management -----------------

function getFormConfig() {
    return {
        id: profileIdInput.value || null,
        name: profileNameInput.value.trim() || hostInput.value.trim(),
        host: hostInput.value.trim(),
        port: parseInt(portInput.value) || 3389,
        username: usernameInput.value.trim(),
        password: passwordInput.value,
        domain: domainInput.value.trim(),
        resolution: resolutionSelect.value,
        enable_audio: enableAudioCheck.checked,
        enable_clipboard: enableClipboardCheck.checked,
        enable_drive: enableDriveCheck.checked,
        ignore_cert: ignoreCertCheck.checked
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

    try {
        const res = await fetch("/api/session/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (res.ok) {
            switchView(true); // Switch to stream view
            reloadIframe();
        } else {
            showAlert(data.detail || "Failed to initiate RDP session", "error");
        }
    } catch (e) {
        showAlert("Network error trying to connect", "error");
    }
}

async function handleDisconnect() {
    try {
        await fetch("/api/session/disconnect", { method: "POST" });
        switchView(false); // Return to dashboard
    } catch (e) {
        console.error("Disconnect error", e);
    }
}

async function sendSpecialKey(key) {
    try {
        const res = await fetch("/api/session/send-keys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key })
        });
        if (!res.ok) {
            const data = await res.json();
            console.warn("Send keys failed:", data.detail);
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
        const res = await fetch("/api/session/status");
        if (!res.ok) return;
        const data = await res.json();
        updateStatusBadge(data.status, data.target);

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
        }
    } catch (e) {
        // Silent poll fail
    }
}

function updateStatusBadge(status, target) {
    currentStatus = status;
    sessionBadge.className = "badge";
    badgeStatusText.textContent = status;

    if (status === "connected") {
        sessionBadge.classList.add("badge-connected");
        if (target) {
            navTargetHost.textContent = target;
            navTargetHost.classList.remove("hidden");
        }
    } else if (status === "connecting") {
        sessionBadge.classList.add("badge-connecting");
    } else if (status === "error") {
        sessionBadge.classList.add("badge-error");
    } else {
        sessionBadge.classList.add("badge-idle");
        navTargetHost.classList.add("hidden");
    }
}

// ----------------- Profiles Management -----------------

async function loadProfiles() {
    try {
        const res = await fetch("/api/profiles");
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

    profilesList.innerHTML = savedProfiles.map(p => `
        <div class="profile-card" data-id="${p.id}">
            <div class="profile-info">
                <h4>${escapeHtml(p.name || p.host)}</h4>
                <p>${escapeHtml(p.username ? p.username + '@' : '')}${escapeHtml(p.host)}:${p.port || 3389} (${p.resolution})</p>
            </div>
            <div class="profile-actions">
                <button class="btn btn-primary btn-sm btn-prof-connect" data-id="${p.id}">Connect</button>
                <button class="btn btn-secondary btn-sm btn-prof-edit" data-id="${p.id}">Edit</button>
                <button class="btn btn-danger btn-sm btn-prof-del" data-id="${p.id}">✕</button>
            </div>
        </div>
    `).join("");

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

async function handleSaveProfile() {
    const config = getFormConfig();
    if (!config.host) {
        showAlert("Host is required to save a profile", "error");
        return;
    }

    try {
        const res = await fetch("/api/profiles", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(config)
        });
        if (res.ok) {
            await loadProfiles();
            showAlert("Profile saved successfully!", "success");
        }
    } catch (e) {
        showAlert("Failed to save profile", "error");
    }
}

function editProfile(profileId) {
    const p = savedProfiles.find(x => x.id === profileId);
    if (!p) return;
    profileIdInput.value = p.id;
    profileNameInput.value = p.name || "";
    hostInput.value = p.host;
    portInput.value = p.port || 3389;
    usernameInput.value = p.username || "";
    passwordInput.value = p.password || "";
    domainInput.value = p.domain || "";
    resolutionSelect.value = p.resolution || "dynamic";
    enableAudioCheck.checked = p.enable_audio !== false;
    enableClipboardCheck.checked = p.enable_clipboard !== false;
    enableDriveCheck.checked = p.enable_drive !== false;
    ignoreCertCheck.checked = p.ignore_cert !== false;

    btnResetForm.classList.remove("hidden");
    document.getElementById("form-title").textContent = `Editing: ${p.name || p.host}`;
}

async function deleteProfile(profileId) {
    if (!confirm("Are you sure you want to delete this profile?")) return;
    try {
        await fetch(`/api/profiles/${profileId}`, { method: "DELETE" });
        await loadProfiles();
    } catch (e) {
        console.error("Failed to delete profile", e);
    }
}

function resetForm() {
    rdpForm.reset();
    profileIdInput.value = "";
    btnResetForm.classList.add("hidden");
    document.getElementById("form-title").textContent = "Connection Configuration";
}

// ----------------- View & Fullscreen Management -----------------

function switchView(toStream) {
    isStreamView = toStream;
    if (toStream) {
        dashboardView.classList.add("hidden");
        streamView.classList.remove("hidden");
        toggleViewText.textContent = "Back to Dashboard";
    } else {
        streamView.classList.add("hidden");
        dashboardView.classList.remove("hidden");
        toggleViewText.textContent = "Go to Stream";
    }
}

function toggleView() {
    switchView(!isStreamView);
}

function reloadIframe() {
    // Force iframe reload to bind cleanly to active session
    selkiesIframe.src = "/stream/?t=" + Date.now();
}

function toggleFullscreen() {
    const container = document.getElementById("stream-container");
    if (!document.fullscreenElement) {
        container.requestFullscreen().catch(err => {
            alert(`Error entering fullscreen: ${err.message}`);
        });
    } else {
        document.exitFullscreen();
    }
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
    });
}

async function uploadFiles(files) {
    const progressContainer = document.getElementById("upload-progress");
    const progressFill = document.getElementById("progress-fill");
    const progressText = document.getElementById("progress-text");

    progressContainer.classList.remove("hidden");

    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        progressText.textContent = `Uploading ${file.name} (${i + 1}/${files.length})...`;
        progressFill.style.width = `${((i + 1) / files.length) * 100}%`;

        const formData = new FormData();
        formData.append("file", file);

        try {
            await fetch("/api/files/upload", {
                method: "POST",
                body: formData
            });
        } catch (e) {
            console.error(`Failed to upload ${file.name}:`, e);
        }
    }

    setTimeout(() => {
        progressContainer.classList.add("hidden");
        progressFill.style.width = "0%";
    }, 1000);

    await loadFiles();
}

async function loadFiles() {
    const table = document.getElementById("file-list-table");
    try {
        const res = await fetch("/api/files");
        if (!res.ok) return;
        const files = await res.json();

        if (files.length === 0) {
            table.innerHTML = `<div class="empty-state">No files in shared folder. Upload files above or save to <code>\\tsclient\\SharedFolder</code> in Windows.</div>`;
            return;
        }

        table.innerHTML = files.map(f => `
            <div class="file-item">
                <div class="file-item-info">
                    <span>${f.is_dir ? '📁' : '📄'}</span>
                    <div>
                        <strong>${escapeHtml(f.name)}</strong>
                        <div style="font-size: 0.75rem; color: var(--text-muted);">${f.size_formatted}</div>
                    </div>
                </div>
                <div>
                    ${!f.is_dir ? `<a href="/api/files/download?path=${encodeURIComponent(f.path)}" class="btn btn-secondary btn-xs" download>Download</a>` : ''}
                    <button class="btn btn-danger btn-xs btn-file-del" data-path="${escapeHtml(f.path)}">✕</button>
                </div>
            </div>
        `).join("");

        document.querySelectorAll(".btn-file-del").forEach(b => {
            b.addEventListener("click", () => deleteFile(b.dataset.path));
        });
    } catch (e) {
        table.innerHTML = `<div class="empty-state">Error loading files</div>`;
    }
}

async function deleteFile(path) {
    if (!confirm(`Delete ${path}?`)) return;
    try {
        await fetch(`/api/files?path=${encodeURIComponent(path)}`, { method: "DELETE" });
        await loadFiles();
    } catch (e) {
        console.error("Delete error", e);
    }
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
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// Kickoff
document.addEventListener("DOMContentLoaded", initApp);
