import os
import shutil
import subprocess
import tempfile
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

logger = logging.getLogger("session_manager")

SHARED_DIR = os.environ.get("SHARED_DIR", "/shared")
DEFAULT_PORTS = {"rdp": 3389, "vnc": 5900, "ssh": 22}
PROTOCOL_NAMES = {"rdp": "RDP", "vnc": "VNC", "ssh": "SSH"}

# WM_CLASS values given to client windows so we can detect when a session is actually up
RDP_WM_CLASS = "selkies-rdp"
SSH_WM_CLASS = "VncHubTerm"
# Log fragments that mean an RDP connection failed even if FreeRDP exits cleanly
RDP_ERROR_MARKERS = ("ERRCONNECT", "Authentication only, exit status", "LOGON_FAILURE")

# Only these named key actions may be injected into the X display, per protocol
KEY_MAP = {
    "rdp": {
        # FreeRDP translates Ctrl+Alt+End into Ctrl+Alt+Del for the remote Windows host
        "ctrl_alt_del": ["Control_L+Alt_L+End"],
        "super": ["Super_L"],
        "alt_tab": ["Alt_L+Tab"],
    },
    "vnc": {
        "ctrl_alt_del": ["Control_L+Alt_L+Delete"],
        "super": ["Super_L"],
        "alt_tab": ["Alt_L+Tab"],
    },
    "ssh": {},
}

# sshpass / ssh exit codes that mean the connection itself failed
SSH_ERRORS = {
    5: "SSH login failed: incorrect password",
    6: "SSH host key is unknown",
    255: "SSH connection failed (see the terminal output for details)",
}

# Runs the client, records its exit code, and keeps the terminal open on failure so the
# user can read the error. The command is passed as positional args, never interpolated.
SSH_WRAPPER = (
    '"$@"; rc=$?; printf "%s" "$rc" > "$VNCHUB_RC_FILE"; '
    'if [ "$rc" -ne 0 ]; then printf "\\n[session ended with exit code %s - press Enter to close]" "$rc"; read _; fi'
)


@dataclass
class Launch:
    """Everything needed to start one client process."""
    cmd: List[str]
    target: str
    stdin_text: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    wm_class: Optional[str] = None   # find the window by WM_CLASS...
    match_pid: bool = False          # ...or by the client's _NET_WM_PID
    workdir: Optional[str] = None    # private temp dir, removed when the session ends
    rc_file: Optional[str] = None    # exit code written by the SSH wrapper


class ConfigError(Exception):
    pass


class SessionManager:
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.protocol: Optional[str] = None
        self.status: str = "disconnected" # disconnected, connecting, connected, error
        self.last_error: Optional[str] = None
        self.log_history: List[str] = []
        self.max_logs: int = 100
        self.current_target: Optional[str] = None
        self.start_time: Optional[float] = None
        self.lock = threading.Lock()
        self._user_disconnected: set = set()
        self.rdp_binary = "xfreerdp"
        self.supports_args_from = False
        self._find_xfreerdp_binary()

    def _find_xfreerdp_binary(self):
        for candidate in ["/usr/bin/xfreerdp3", "/usr/bin/xfreerdp", "xfreerdp3", "xfreerdp"]:
            try:
                out = subprocess.run(
                    [candidate, "--help"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10
                ).stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            self.rdp_binary = candidate
            # /args-from lets us pass the password over stdin instead of the world-readable argv
            self.supports_args_from = "/args-from" in out
            logger.info(f"Using FreeRDP binary: {self.rdp_binary} (args-from supported: {self.supports_args_from})")
            return

    def _env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = os.environ.get("DISPLAY", ":1")
        return env

    def _append_log(self, line: str):
        line = line.strip()
        if not line:
            return
        with self.lock:
            self.log_history.append(line)
            if len(self.log_history) > self.max_logs:
                self.log_history.pop(0)

    # ----------------- Command builders -----------------

    def _build_rdp(self, config: Dict[str, Any]) -> Launch:
        host = config["host"]
        port = config["port"]
        username = config.get("username", "")
        password = config.get("password", "")
        domain = config.get("domain", "")
        resolution = config.get("resolution", "dynamic")

        target_address = f"{host}:{port}"
        args = [f"/v:{target_address}", f"/wm-class:{RDP_WM_CLASS}"]

        if username:
            args.append(f"/u:{username}")
        if password:
            args.append(f"/p:{password}")
        if domain:
            args.append(f"/d:{domain}")

        # Audio Redirection to the container's PulseAudio server
        if config.get("enable_audio", True):
            args.append("/sound:sys:pulse")

        # Bidirectional Clipboard Synchronization
        if config.get("enable_clipboard", True):
            args.append("+clipboard")

        # Drive Redirection: mount container shared folder as RDP SharedFolder
        if config.get("enable_drive", True):
            os.makedirs(SHARED_DIR, exist_ok=True)
            args.append(f"/drive:SharedFolder,{SHARED_DIR}")

        # Resolution & Fullscreen mode
        if resolution == "dynamic" or not resolution:
            args.append("/dynamic-resolution")
        elif "x" in resolution:
            args.append(f"/size:{resolution}")
        args.append("/f") # Fullscreen mode inside virtual display

        # Windows display scaling (DPI); FreeRDP resends it on every dynamic resize
        desktop_scale = int(config.get("desktop_scale") or 100)
        if desktop_scale > 100:
            device_scale = 100 if desktop_scale < 140 else 140 if desktop_scale < 180 else 180
            args.extend([f"/scale-desktop:{min(desktop_scale, 500)}", f"/scale-device:{device_scale}"])

        # Graphics pipeline with FreeRDP's best available codec. AVC444/AVC420 values are only
        # accepted by builds with H.264 (Ubuntu's freerdp3 has none) and fail argument parsing.
        args.extend([
            "/gfx",
            "/network:auto",
            "+auto-reconnect",
            "/auto-reconnect-max-retries:10"
        ])

        if config.get("ignore_cert", True):
            args.append("/cert:ignore")

        if self.supports_args_from:
            if any("\n" in a or "\r" in a for a in args):
                raise ConfigError("Connection fields must not contain line breaks")
            return Launch(
                cmd=[self.rdp_binary, "/args-from:stdin"], target=target_address,
                stdin_text="\n".join(args) + "\n", wm_class=RDP_WM_CLASS
            )
        logger.warning("FreeRDP lacks /args-from; password will be visible in the process list")
        return Launch(cmd=[self.rdp_binary] + args, target=target_address, wm_class=RDP_WM_CLASS)

    def _build_vnc(self, config: Dict[str, Any]) -> Launch:
        host = config["host"]
        port = config["port"]
        password = config.get("password", "")
        workdir = tempfile.mkdtemp(prefix="vnchub-")

        cmd = [
            "xtigervncviewer",
            "-FullScreen",
            "-RemoteResize",
            # Exit with the error on stderr instead of opening dialogs nobody can dismiss
            "-AlertOnFatalError=0",
            "-ReconnectOnError=0",
        ]
        if config.get("view_only"):
            cmd += ["-ViewOnly", "-Shared"]

        if password:
            # tigervncpasswd -f turns the plain password (stdin) into the viewer's password file
            try:
                obfuscated = subprocess.run(
                    ["tigervncpasswd", "-f"], input=password.encode("utf-8"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10
                ).stdout
            except FileNotFoundError:
                shutil.rmtree(workdir, ignore_errors=True)
                raise ConfigError("tigervncpasswd not installed in container")
            except subprocess.CalledProcessError as e:
                shutil.rmtree(workdir, ignore_errors=True)
                raise ConfigError(f"Could not encode VNC password: {e.stderr.decode(errors='replace').strip()}")
            passwd_file = os.path.join(workdir, "passwd")
            fd = os.open(passwd_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(obfuscated)
            cmd += ["-PasswordFile", passwd_file]

        cmd.append(f"{host}::{port}")
        return Launch(cmd=cmd, target=f"{host}:{port}", match_pid=True, workdir=workdir)

    def _build_ssh(self, config: Dict[str, Any]) -> Launch:
        host = config["host"]
        port = config["port"]
        username = config.get("username", "")
        password = config.get("password", "")
        ssh_key = (config.get("ssh_key") or "").strip()
        font_size = int(config.get("font_size") or 12)

        workdir = tempfile.mkdtemp(prefix="vnchub-")
        rc_file = os.path.join(workdir, "rc")
        env: Dict[str, str] = {"VNCHUB_RC_FILE": rc_file}

        ssh_cmd = [
            "ssh", "-p", str(port),
            # Trust a host on first use, but refuse if its key later changes
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30",
        ]
        if username:
            ssh_cmd += ["-l", username]
        if ssh_key:
            if "PRIVATE KEY" not in ssh_key:
                shutil.rmtree(workdir, ignore_errors=True)
                raise ConfigError("SSH key must be a PEM/OpenSSH private key")
            key_file = os.path.join(workdir, "id_key")
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(ssh_key.replace("\r\n", "\n") + "\n")
            ssh_cmd += ["-i", key_file, "-o", "IdentitiesOnly=yes"]
        # "--" ends ssh options, so the host can never be read as a flag
        ssh_cmd += ["--", host]

        if password:
            # sshpass -e reads the password from $SSHPASS (only readable by our own user)
            env["SSHPASS"] = password
            ssh_cmd = ["sshpass", "-e"] + ssh_cmd

        title = f"SSH {username + '@' if username else ''}{host}"
        cmd = [
            "xterm",
            "-class", SSH_WM_CLASS,
            "-fullscreen",
            "-title", title,
            "-fa", "DejaVu Sans Mono",
            "-fs", str(font_size),
            "-bg", "#0b0f19",
            "-fg", "#e2e8f0",
            "+sb",
            "-sl", "10000",
            "-xrm", "*selectToClipboard: true",
            "-xrm", "*metaSendsEscape: true",
            "-xrm", "*termName: xterm-256color",
            "-e", "sh", "-c", SSH_WRAPPER, "vnchub-ssh",
        ] + ssh_cmd
        return Launch(
            cmd=cmd, target=f"{host}:{port}", env=env,
            wm_class=SSH_WM_CLASS, workdir=workdir, rc_file=rc_file
        )

    # ----------------- Session lifecycle -----------------

    def _window_exists(self, launch: Launch, proc: subprocess.Popen) -> Optional[bool]:
        searches = []
        if launch.match_pid:
            searches.append(["--pid", str(proc.pid)])
        if launch.wm_class:
            searches.append(["--class", launch.wm_class])
        try:
            for args in searches:
                res = subprocess.run(
                    ["xdotool", "search"] + args,
                    env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5
                )
                if res.returncode == 0 and res.stdout.strip():
                    return True
            return False
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            return False

    def _watch_connected(self, proc: subprocess.Popen, launch: Launch):
        """Mark the session connected once the client has opened its window."""
        started = time.time()
        while proc.poll() is None:
            with self.lock:
                if self.process is not proc or self.status != "connecting":
                    return
            found = self._window_exists(launch, proc)
            if found is None:
                # xdotool unavailable: fall back to a simple liveness heuristic
                if time.time() - started < 3:
                    time.sleep(0.5)
                    continue
                found = True
            if found:
                with self.lock:
                    if self.process is proc and self.status == "connecting":
                        self.status = "connected"
                        self.start_time = time.time()
                return
            time.sleep(0.5)

    def _session_error(self, protocol: str, launch: Launch, return_code: int,
                       error_line: Optional[str], last_line: Optional[str]) -> Optional[str]:
        """Why the session failed, or None if it ended normally."""
        if protocol == "rdp":
            if error_line or return_code != 0:
                return error_line or f"FreeRDP process exited with code {return_code}"
            return None
        if protocol == "ssh":
            try:
                with open(launch.rc_file) as f:
                    ssh_rc = int(f.read().strip() or 0)
            except (OSError, ValueError, TypeError):
                return None # terminal closed before ssh finished
            return SSH_ERRORS.get(ssh_rc)
        if return_code != 0:
            return last_line or f"VNC viewer exited with code {return_code}"
        return None

    def _monitor_process(self, proc: subprocess.Popen, launch: Launch, protocol: str):
        error_line = None
        last_line = None
        for line in iter(proc.stdout.readline, ''):
            self._append_log(line)
            if line.strip():
                last_line = line.strip()
            if protocol == "rdp" and any(marker in line for marker in RDP_ERROR_MARKERS):
                error_line = line.strip()
                with self.lock:
                    if self.process is proc:
                        self.last_error = error_line

        return_code = proc.wait()
        logger.info(f"{PROTOCOL_NAMES[protocol]} session terminated with code {return_code}")
        failure = self._session_error(protocol, launch, return_code, error_line, last_line)
        if launch.workdir:
            shutil.rmtree(launch.workdir, ignore_errors=True)

        with self.lock:
            user_disconnected = proc in self._user_disconnected
            self._user_disconnected.discard(proc)
            if self.process is not proc:
                # A newer session has replaced this one; leave its state alone
                return
            if not user_disconnected and failure:
                self.status = "error"
                self.last_error = failure
            else:
                self.status = "disconnected"
            self.process = None
            self.protocol = None
            self.current_target = None
            self.start_time = None

    def connect(self, config: Dict[str, Any]) -> Dict[str, Any]:
        protocol = config.get("protocol") or "rdp"
        builder = {"rdp": self._build_rdp, "vnc": self._build_vnc, "ssh": self._build_ssh}.get(protocol)
        if not builder:
            return {"success": False, "message": f"Unsupported protocol: {protocol}"}

        with self.lock:
            if self.process and self.process.poll() is None:
                return {"success": False, "message": "A session is already running"}
            if not config.get("host"):
                return {"success": False, "message": "Host IP or hostname is required"}

            config = dict(config)
            config["port"] = config.get("port") or DEFAULT_PORTS[protocol]
            try:
                launch = builder(config)
            except ConfigError as e:
                return {"success": False, "message": str(e)}

            name = PROTOCOL_NAMES[protocol]
            logger.info(f"Starting {name} session to {launch.target}...")
            self.log_history.clear()
            self.last_error = None
            self.protocol = protocol
            self.current_target = launch.target
            self.start_time = None
            self.status = "connecting"

            env = self._env()
            env.update(launch.env)
            try:
                proc = subprocess.Popen(
                    launch.cmd,
                    stdin=subprocess.PIPE if launch.stdin_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env
                )
                if launch.stdin_text is not None:
                    proc.stdin.write(launch.stdin_text)
                    proc.stdin.close()
            except Exception as e:
                if launch.workdir:
                    shutil.rmtree(launch.workdir, ignore_errors=True)
                self.status = "error"
                self.last_error = str(e)
                self.protocol = None
                self.current_target = None
                logger.error(f"Failed to launch {name} client: {e}")
                return {"success": False, "message": str(e)}

            self.process = proc
            threading.Thread(target=self._monitor_process, args=(proc, launch, protocol), daemon=True).start()
            threading.Thread(target=self._watch_connected, args=(proc, launch), daemon=True).start()
            return {"success": True, "message": f"Connecting to {launch.target} ({name})..."}

    def disconnect(self) -> Dict[str, Any]:
        with self.lock:
            proc = self.process
            if not proc or proc.poll() is not None:
                self.status = "disconnected"
                self.protocol = None
                self.current_target = None
                self.start_time = None
                return {"success": True, "message": "No active session"}
            self._user_disconnected.add(proc)
            self.status = "disconnected"
            self.current_target = None
            self.start_time = None

        # Terminate outside the lock so status polling isn't blocked
        logger.info("Disconnecting active session...")
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return {"success": True, "message": "Disconnected successfully"}

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            uptime = int(time.time() - self.start_time) if (self.start_time and self.status == "connected") else 0
            return {
                "status": self.status,
                "protocol": self.protocol,
                "target": self.current_target,
                "uptime_seconds": uptime,
                "last_error": self.last_error,
                "recent_logs": self.log_history[-20:]
            }

    def send_keys(self, key_combination: str) -> Dict[str, Any]:
        """Inject special key events (Ctrl+Alt+Del, Windows Key, etc.) into the virtual X11 display."""
        with self.lock:
            protocol = self.protocol or "rdp"
        keys_to_send = KEY_MAP.get(protocol, {}).get(key_combination.lower())
        if not keys_to_send:
            return {"success": False, "message": f"Key action {key_combination} is not available for {PROTOCOL_NAMES[protocol]}"}
        try:
            for k in keys_to_send:
                subprocess.run(["xdotool", "key", k], env=self._env(), check=True, timeout=5)
            return {"success": True, "message": f"Sent keys: {key_combination}"}
        except FileNotFoundError:
            return {"success": False, "message": "xdotool not installed in container"}
        except Exception as e:
            return {"success": False, "message": f"Failed to send keys: {e}"}

    def set_clipboard(self, text: str) -> Dict[str, Any]:
        """Put text on the X11 CLIPBOARD selection; the RDP/VNC client syncs it to the remote side."""
        try:
            # xclip forks to own the selection, so its output must not be piped back to us
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-i"],
                input=text.encode("utf-8"), env=self._env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5
            )
            return {"success": True, "message": "Clipboard updated"}
        except FileNotFoundError:
            return {"success": False, "message": "xclip not installed in container"}
        except Exception as e:
            return {"success": False, "message": f"Failed to set clipboard: {e}"}

# Global singleton manager
session_manager = SessionManager()
