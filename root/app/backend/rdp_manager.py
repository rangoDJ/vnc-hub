import os
import subprocess
import threading
import time
import logging
import shlex
from typing import Optional, Dict, Any, List

logger = logging.getLogger("rdp_manager")

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
        self._find_xfreerdp_binary()

    def _find_xfreerdp_binary(self):
        for candidate in ["/usr/bin/xfreerdp3", "/usr/bin/xfreerdp", "xfreerdp3", "xfreerdp"]:
            try:
                subprocess.run([candidate, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.binary = candidate
                logger.info(f"Using FreeRDP binary: {self.binary}")
                return
            except FileNotFoundError:
                continue
        self.binary = "xfreerdp"

    def _append_log(self, line: str):
        line = line.strip()
        if not line:
            return
        with self.lock:
            self.log_history.append(line)
            if len(self.log_history) > self.max_logs:
                self.log_history.pop(0)

    def _monitor_process(self):
        time.sleep(1.5)
        if self.process and self.process.poll() is None:
            with self.lock:
                self.status = "connected"

        # Read stdout and stderr line by line
        for line in iter(self.process.stdout.readline, ''):
            if not line:
                break
            self._append_log(line)
            if "Authentication only, exit status" in line or "ERRCONNECT" in line:
                with self.lock:
                    self.status = "error"
                    self.last_error = line.strip()

        return_code = self.process.wait()
        with self.lock:
            if return_code != 0 and self.status != "disconnected":
                self.status = "error"
                self.last_error = f"FreeRDP process exited with code {return_code}"
            else:
                self.status = "disconnected"
            self.process = None
            self.start_time = None
        logger.info(f"FreeRDP session terminated with code {return_code}")

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
            cmd = [self.binary, f"/v:{target_address}"]

            if username:
                cmd.append(f"/u:{username}")
            if password:
                cmd.append(f"/p:{password}")
            if domain:
                cmd.append(f"/d:{domain}")

            # Audio Redirection to container's PulseAudio server
            if enable_audio:
                cmd.append("/sound:sys:pulse")

            # Bidirectional Clipboard Synchronization
            if enable_clipboard:
                cmd.append("+clipboard")

            # Drive Redirection: mount container /shared folder as RDP SharedFolder
            if enable_drive:
                os.makedirs("/shared", exist_ok=True)
                cmd.append("/drive:SharedFolder,/shared")

            # Resolution & Fullscreen mode
            if resolution == "dynamic" or not resolution:
                cmd.append("/dynamic-resolution")
            elif "x" in resolution:
                cmd.append(f"/size:{resolution}")
            cmd.append("/f") # Fullscreen mode inside virtual display

            # Advanced graphics & network optimizations
            cmd.extend([
                "/gfx:avc444",
                "/network:auto",
                "+auto-reconnect",
                "+auto-reconnect-max-retries:10"
            ])

            if ignore_cert:
                cmd.append("/cert:ignore")

            # Execution environment
            env = os.environ.copy()
            env["DISPLAY"] = os.environ.get("DISPLAY", ":1")
            env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", "127.0.0.1:4713")

            logger.info(f"Starting FreeRDP session to {target_address}...")
            self.log_history.clear()
            self.last_error = None
            self.current_target = target_address
            self.start_time = time.time()
            self.status = "connecting"

            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env
                )
                threading.Thread(target=self._monitor_process, daemon=True).start()
                return {"success": True, "message": f"Connecting to {target_address}..."}
            except Exception as e:
                self.status = "error"
                self.last_error = str(e)
                logger.error(f"Failed to launch FreeRDP: {e}")
                return {"success": False, "message": str(e)}

    def disconnect(self) -> Dict[str, Any]:
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.status = "disconnected"
                return {"success": True, "message": "No active session"}

            logger.info("Disconnecting active FreeRDP session...")
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

            self.status = "disconnected"
            self.current_target = None
            self.start_time = None
            self.process = None
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
        env = os.environ.copy()
        env["DISPLAY"] = os.environ.get("DISPLAY", ":1")
        
        # Mapping common action names to xdotool keys
        # Note: In FreeRDP, Ctrl+Alt+End is translated to Ctrl+Alt+Del for remote Windows
        key_map = {
            "ctrl_alt_del": ["Control_L+Alt_L+End", "Control_L+Alt_L+Delete"],
            "super": ["Super_L"],
            "alt_tab": ["Alt_L+Tab"]
        }

        keys_to_send = key_map.get(key_combination.lower(), [key_combination])
        try:
            for k in keys_to_send:
                subprocess.run(["xdotool", "key", k], env=env, check=True)
            return {"success": True, "message": f"Sent keys: {key_combination}"}
        except FileNotFoundError:
            return {"success": False, "message": "xdotool not installed in container"}
        except Exception as e:
            return {"success": False, "message": f"Failed to send keys: {e}"}

# Global singleton manager
rdp_manager = RDPManager()
