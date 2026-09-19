import os
import subprocess
import threading
import time
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("rdp_manager")

SHARED_DIR = os.environ.get("SHARED_DIR", "/shared")
# WM_CLASS given to the FreeRDP window so we can detect when the session is actually up
WM_CLASS = "selkies-rdp"
# Log fragments that mean the connection failed even if FreeRDP exits cleanly
ERROR_MARKERS = ("ERRCONNECT", "Authentication only, exit status", "LOGON_FAILURE")

# Only these named key actions may be injected into the X display
KEY_MAP = {
    # FreeRDP translates Ctrl+Alt+End into Ctrl+Alt+Del for the remote Windows host
    "ctrl_alt_del": ["Control_L+Alt_L+End"],
    "super": ["Super_L"],
    "alt_tab": ["Alt_L+Tab"],
}

class RDPManager:
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.status: str = "disconnected" # disconnected, connecting, connected, error
        self.last_error: Optional[str] = None
        self.log_history: List[str] = []
        self.max_logs: int = 100
        self.current_target: Optional[str] = None
        self.start_time: Optional[float] = None
        self.lock = threading.Lock()
        self._user_disconnected: set = set()
        self.binary = "xfreerdp"
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
            self.binary = candidate
            # /args-from lets us pass the password over stdin instead of the world-readable argv
            self.supports_args_from = "/args-from" in out
            logger.info(f"Using FreeRDP binary: {self.binary} (args-from supported: {self.supports_args_from})")
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

    def _window_exists(self) -> Optional[bool]:
        try:
            res = subprocess.run(
                ["xdotool", "search", "--class", WM_CLASS],
                env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5
            )
            return res.returncode == 0 and bool(res.stdout.strip())
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            return False

    def _watch_connected(self, proc: subprocess.Popen):
        """Mark the session connected once FreeRDP has opened its window (i.e. auth succeeded)."""
        started = time.time()
        while proc.poll() is None:
            with self.lock:
                if self.process is not proc or self.status != "connecting":
                    return
            found = self._window_exists()
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

    def _monitor_process(self, proc: subprocess.Popen):
        error_line = None
        for line in iter(proc.stdout.readline, ''):
            self._append_log(line)
            if any(marker in line for marker in ERROR_MARKERS):
                error_line = line.strip()
                with self.lock:
                    if self.process is proc:
                        self.last_error = error_line

        return_code = proc.wait()
        logger.info(f"FreeRDP session terminated with code {return_code}")
        with self.lock:
            user_disconnected = proc in self._user_disconnected
            self._user_disconnected.discard(proc)
            if self.process is not proc:
                # A newer session has replaced this one; leave its state alone
                return
            if user_disconnected:
                self.status = "disconnected"
            elif error_line or return_code != 0:
                self.status = "error"
                self.last_error = error_line or f"FreeRDP process exited with code {return_code}"
            else:
                self.status = "disconnected"
            self.process = None
            self.current_target = None
            self.start_time = None

    def connect(self, config: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if self.process and self.process.poll() is None:
                return {"success": False, "message": "An RDP session is already running"}

            host = config.get("host")
            if not host:
                return {"success": False, "message": "Host IP or hostname is required"}

            port = config.get("port", 3389)
            username = config.get("username", "")
            password = config.get("password", "")
            domain = config.get("domain", "")
            resolution = config.get("resolution", "dynamic")
            enable_audio = config.get("enable_audio", True)
            enable_clipboard = config.get("enable_clipboard", True)
            enable_drive = config.get("enable_drive", True)
            ignore_cert = config.get("ignore_cert", True)

            # Build FreeRDP command line arguments
            target_address = f"{host}:{port}" if port else host
            args = [f"/v:{target_address}", f"/wm-class:{WM_CLASS}"]

            if username:
                args.append(f"/u:{username}")
            if password:
                args.append(f"/p:{password}")
            if domain:
                args.append(f"/d:{domain}")

            # Audio Redirection to the container's PulseAudio server
            if enable_audio:
                args.append("/sound:sys:pulse")

            # Bidirectional Clipboard Synchronization
            if enable_clipboard:
                args.append("+clipboard")

            # Drive Redirection: mount container shared folder as RDP SharedFolder
            if enable_drive:
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

            if ignore_cert:
                args.append("/cert:ignore")

            if self.supports_args_from:
                if any("\n" in a or "\r" in a for a in args):
                    return {"success": False, "message": "Connection fields must not contain line breaks"}
                cmd = [self.binary, "/args-from:stdin"]
            else:
                logger.warning("FreeRDP lacks /args-from; password will be visible in the process list")
                cmd = [self.binary] + args

            logger.info(f"Starting FreeRDP session to {target_address}...")
            self.log_history.clear()
            self.last_error = None
            self.current_target = target_address
            self.start_time = None
            self.status = "connecting"

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE if self.supports_args_from else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=self._env()
                )
                if self.supports_args_from:
                    proc.stdin.write("\n".join(args) + "\n")
                    proc.stdin.close()
            except Exception as e:
                self.status = "error"
                self.last_error = str(e)
                self.current_target = None
                logger.error(f"Failed to launch FreeRDP: {e}")
                return {"success": False, "message": str(e)}

            self.process = proc
            threading.Thread(target=self._monitor_process, args=(proc,), daemon=True).start()
            threading.Thread(target=self._watch_connected, args=(proc,), daemon=True).start()
            return {"success": True, "message": f"Connecting to {target_address}..."}

    def disconnect(self) -> Dict[str, Any]:
        with self.lock:
            proc = self.process
            if not proc or proc.poll() is not None:
                self.status = "disconnected"
                self.current_target = None
                self.start_time = None
                return {"success": True, "message": "No active session"}
            self._user_disconnected.add(proc)
            self.status = "disconnected"
            self.current_target = None
            self.start_time = None

        # Terminate outside the lock so status polling isn't blocked
        logger.info("Disconnecting active FreeRDP session...")
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
                "target": self.current_target,
                "uptime_seconds": uptime,
                "last_error": self.last_error,
                "recent_logs": self.log_history[-20:]
            }

    def send_keys(self, key_combination: str) -> Dict[str, Any]:
        """Inject special key events (Ctrl+Alt+Del, Windows Key, etc.) into the virtual X11 display."""
        keys_to_send = KEY_MAP.get(key_combination.lower())
        if not keys_to_send:
            return {"success": False, "message": f"Unsupported key action: {key_combination}"}
        try:
            for k in keys_to_send:
                subprocess.run(["xdotool", "key", k], env=self._env(), check=True, timeout=5)
            return {"success": True, "message": f"Sent keys: {key_combination}"}
        except FileNotFoundError:
            return {"success": False, "message": "xdotool not installed in container"}
        except Exception as e:
            return {"success": False, "message": f"Failed to send keys: {e}"}

    def set_clipboard(self, text: str) -> Dict[str, Any]:
        """Put text on the X11 CLIPBOARD selection; FreeRDP's +clipboard syncs it to Windows."""
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
rdp_manager = RDPManager()
