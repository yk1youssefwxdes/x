import datetime
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.request import urlopen

# Early customer data directories bootstrap & legacy data migration
from core.paths import (
    get_base_dir as _get_paths_base_dir,
    get_data_dir,
    get_config_dir,
    get_logs_dir,
    get_whatsapp_session_dir,
    ensure_data_directories,
    migrate_legacy_data,
)
from core.version import VERSION, APP_NAME

ensure_data_directories()
migrate_legacy_data()


# ---------------------------------------------------------------------------
# Runtime discovery helpers (Commercial Bundled vs. Local Development)
# ---------------------------------------------------------------------------

def _get_base_dir() -> str:
    """Directory containing this script or the frozen executable."""
    if getattr(sys, "frozen", False) or "nuitka" in sys.modules:
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return str(_get_paths_base_dir())


def get_runtime_dir() -> str:
    """Path to the bundled runtime directory."""
    return os.path.join(_get_base_dir(), "runtime")


def get_python_executable() -> str:
    """
    Locate Python executable.
    1. Bundled private runtime: runtime/python/python.exe (Win) or runtime/python/bin/python (Linux)
    2. Local venv/ or .venv/
    3. sys.executable
    """
    runtime_dir = get_runtime_dir()
    if sys.platform == "win32":
        bundled = os.path.join(runtime_dir, "python", "python.exe")
    else:
        bundled = os.path.join(runtime_dir, "python", "bin", "python")

    if os.path.isfile(bundled):
        return bundled

    base_dir = _get_base_dir()
    for candidate in ("venv", ".venv"):
        vpath = os.path.join(base_dir, candidate)
        if os.path.isdir(vpath):
            if sys.platform == "win32":
                vpy = os.path.join(vpath, "Scripts", "python.exe")
            else:
                vpy = os.path.join(vpath, "bin", "python")
            if os.path.isfile(vpy):
                return vpy

    return sys.executable


def get_node_executable() -> str:
    """
    Locate Node.js executable.
    1. Bundled private runtime: runtime/node/node.exe (Win) or runtime/node/bin/node (Linux)
    2. System node.exe (Win) or node (Linux)
    """
    runtime_dir = get_runtime_dir()
    if sys.platform == "win32":
        bundled = os.path.join(runtime_dir, "node", "node.exe")
    else:
        bundled = os.path.join(runtime_dir, "node", "bin", "node")

    if os.path.isfile(bundled):
        return bundled

    return "node.exe" if sys.platform == "win32" else "node"


def get_npm_executable() -> str:
    """
    Locate npm executable.
    1. Bundled private runtime: runtime/node/npm.cmd (Win) or runtime/node/bin/npm (Linux)
    2. System npm.cmd (Win) or npm (Linux)
    """
    runtime_dir = get_runtime_dir()
    if sys.platform == "win32":
        bundled = os.path.join(runtime_dir, "node", "npm.cmd")
    else:
        bundled = os.path.join(runtime_dir, "node", "bin", "npm")

    if os.path.isfile(bundled):
        return bundled

    return "npm.cmd" if sys.platform == "win32" else "npm"


def get_chromium_executable() -> str | None:
    """
    Locate Chromium browser binary for Puppeteer / WhatsApp.
    1. Bundled private runtime: runtime/chromium/chrome.exe (Win) or runtime/chromium/chrome (Linux)
    2. CHROME_PATH environment variable if set and existing
    3. None (Puppeteer internal fallback)
    """
    runtime_dir = get_runtime_dir()
    if sys.platform == "win32":
        bundled = os.path.join(runtime_dir, "chromium", "chrome.exe")
    else:
        bundled = os.path.join(runtime_dir, "chromium", "chrome")

    if os.path.isfile(bundled):
        return bundled

    env_chrome = os.environ.get("CHROME_PATH")
    if env_chrome and os.path.isfile(env_chrome):
        return env_chrome

    return None


# ---------------------------------------------------------------------------
# Network / port helpers
# ---------------------------------------------------------------------------

def is_port_available(port, host="0.0.0.0"):
    """Return True if *port* is available for binding on *host*."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
            return True
    except OSError:
        return False


def find_free_port(start_port, host="0.0.0.0", max_port=65535):
    """
    Return the first available TCP port >= start_port on *host*.
    """
    port = start_port
    while port <= max_port:
        if is_port_available(port, host):
            return port
        port += 1
    raise RuntimeError(f"No available TCP port found starting from {start_port}.")


def get_local_ip():
    """Return the local LAN IP address without requiring internet access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Process management (pure — no Tkinter)
# ---------------------------------------------------------------------------

def _stop_process_tree(process, name, log_fn=None):
    """
    Terminate *process* and its entire process group / tree.
    """
    _log = log_fn if callable(log_fn) else (lambda _msg: None)

    if process is None:
        return

    if process.poll() is not None:
        _log(f"{name} was already stopped.")
        return

    try:
        _log(f"Stopping {name}...")

        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _log(f"{name} did not exit after taskkill.")

        else:
            import signal

            pgid = None
            try:
                pgid = os.getpgid(process.pid)
            except OSError:
                _log(f"{name}: process group not found; terminating process directly.")

            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _log(f"{name} did not stop gracefully. Force-killing process group...")
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                        process.wait(timeout=5)
                    except Exception:
                        pass
                except ProcessLookupError:
                    pass
            else:
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _log(f"{name} did not stop gracefully. Force-killing process...")
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception:
                        pass

        _log(f"{name} stopped.")

    except ProcessLookupError:
        _log(f"{name} was already stopped.")
    except Exception as exc:
        _log(f"Error stopping {name}: {exc}")


def _run_subprocess(args, **kwargs):
    """
    subprocess.run() wrapper injecting CREATE_NO_WINDOW on Windows only.
    """
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(args, **kwargs)


# ---------------------------------------------------------------------------
# Path / environment / config helpers
# ---------------------------------------------------------------------------

def _get_config_path():
    return os.path.join(str(get_config_dir()), "run_server_config.json")


def _load_local_config():
    try:
        with open(_get_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_local_config(config):
    try:
        with open(_get_config_path(), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


def _relaunch_in_venv_if_needed():
    """
    If running in development as a plain script, re-exec using discovered Python runtime.
    """
    if getattr(sys, "frozen", False) or "nuitka" in sys.modules:
        return

    py_exe = get_python_executable()
    try:
        if os.path.samefile(sys.executable, py_exe):
            return
    except OSError:
        if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(
            os.path.abspath(py_exe)
        ):
            return

    sys.exit(subprocess.run([py_exe] + sys.argv).returncode)


_relaunch_in_venv_if_needed()


# ---------------------------------------------------------------------------
# License check — runs before GUI starts
# ---------------------------------------------------------------------------

try:
    from core.license import validate_or_exit
    validate_or_exit()
except SystemExit as _e:
    _root = tk.Tk()
    _root.withdraw()
    _msg = str(_e) if str(_e) else "This copy of the application is not licensed for this device."
    messagebox.showerror("License Verification", _msg)
    sys.exit(1)
except Exception as _e:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "License Verification Error",
        f"Failed to perform license check:\n{_e}",
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

BG        = "#0f1115"
PANEL     = "#171a21"
PANEL_ALT = "#1d2129"
ACCENT    = "#4f8cff"
GREEN     = "#2ecc71"
RED       = "#ff5c5c"
AMBER     = "#f5a623"
TEXT_MAIN = "#e8eaed"
TEXT_DIM  = "#8a8f98"
FONT      = "Segoe UI"


# ---------------------------------------------------------------------------
# Disk Logger (Structured logging to logs/launcher.log)
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()

def _disk_log(message: str, level: str = "INFO"):
    """Thread-safe structured logging to customer logs directory."""
    try:
        log_file = get_logs_dir() / "launcher.log"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [{level}] [Launcher]: {message}\n"
        with _log_lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Server Session (Encapsulates all session-specific state)
# ---------------------------------------------------------------------------

class ServerSession:
    """
    Container for session-specific state.
    A new instance is created on every Start operation.
    """
    def __init__(self, session_id):
        self.id = session_id

        self.django_port = None
        self.whatsapp_port = None

        self.node_process = None
        self.server_instance = None

        self.server_ready = threading.Event()
        self.cancelled = threading.Event()


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class ServerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{VERSION} — Server Controller")
        self.root.geometry("480x420")
        self.root.minsize(480, 420)
        self.root.configure(bg=BG)

        # ── Global application state ─────────────────────────────────────────
        self._session = None          # Active ServerSession instance or None
        self.is_running = False
        self.local_ip = get_local_ip()
        self._stopping = False

        self._startup_lock = threading.Lock()
        self._startup_counter = 0

        # Security
        self.whatsapp_api_key = self._load_or_create_api_key()

        # GUI
        self._build_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        _disk_log(f"Launcher initialized ({APP_NAME} v{VERSION}). Data dir: {get_data_dir()}")

    # =========================================================================
    # API key
    # =========================================================================

    def _load_or_create_api_key(self):
        config = _load_local_config()
        api_key = config.get("WA_API_KEY")
        if not isinstance(api_key, str) or len(api_key) < 10:
            api_key = secrets.token_urlsafe(32)
            config["WA_API_KEY"] = api_key
            _save_local_config(config)
        return api_key

    # =========================================================================
    # Session management & Validation
    # =========================================================================

    def _new_startup_id(self):
        with self._startup_lock:
            self._startup_counter += 1
            return self._startup_counter

    def _is_valid_session(self, session):
        """
        Returns True iff *session* is the active session and has not been cancelled.
        Thread-safe check.
        """
        return (
            session is not None
            and session is self._session
            and not self._stopping
            and not session.cancelled.is_set()
        )

    # =========================================================================
    # Tkinter-safe logging & helpers
    # =========================================================================

    def _log(self, message):
        """Append entry to activity log. Main thread only."""
        clean_msg = message
        if self.whatsapp_api_key:
            clean_msg = clean_msg.replace(self.whatsapp_api_key, "[REDACTED]")

        _disk_log(clean_msg)

        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {clean_msg}\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _thread_log(self, message):
        """Thread-safe log helper using root.after."""
        self.root.after(0, lambda m=message: self._log(m))

    def _make_bg_log(self):
        """Return a logging function safe for background thread calls."""
        return lambda msg: self.root.after(0, lambda m=msg: self._log(m))

    # =========================================================================
    # UI construction
    # =========================================================================

    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Card.TFrame", background=PANEL)
        style.configure("Root.TFrame", background=BG)

        style.configure(
            "Start.TButton",
            background=GREEN, foreground="#0b1f12",
            font=(FONT, 11, "bold"), padding=10, borderwidth=0,
        )
        style.map(
            "Start.TButton",
            background=[("disabled", "#2a3a30"), ("active", "#27ae60")],
            foreground=[("disabled", "#5a6b60")],
        )
        style.configure(
            "Stop.TButton",
            background=RED, foreground="#2a0a0a",
            font=(FONT, 11, "bold"), padding=10, borderwidth=0,
        )
        style.map(
            "Stop.TButton",
            background=[("disabled", "#3a2626"), ("active", "#e64545")],
            foreground=[("disabled", "#6b5a5a")],
        )

    def _build_ui(self):
        root_frame = tk.Frame(self.root, bg=BG)
        root_frame.pack(fill="both", expand=True, padx=18, pady=18)

        # Header
        header = tk.Frame(root_frame, bg=BG)
        header.pack(fill="x", pady=(0, 14))
        tk.Label(header, text=APP_NAME, font=(FONT, 16, "bold"),
                 bg=BG, fg=TEXT_MAIN).pack(anchor="w")
        tk.Label(header, text=f"Local Server Controller • v{VERSION}", font=(FONT, 10),
                 bg=BG, fg=TEXT_DIM).pack(anchor="w")

        # Status card
        status_card = tk.Frame(root_frame, bg=PANEL, padx=16, pady=14)
        status_card.pack(fill="x", pady=(0, 14))

        status_row = tk.Frame(status_card, bg=PANEL)
        status_row.pack(fill="x")

        self.status_dot = tk.Canvas(status_row, width=14, height=14,
                                    bg=PANEL, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 10))
        self._draw_dot(RED)

        status_text_frame = tk.Frame(status_row, bg=PANEL)
        status_text_frame.pack(side="left", fill="x", expand=True)

        self.status_label = tk.Label(status_text_frame, text="Stopped",
                                     font=(FONT, 13, "bold"), bg=PANEL, fg=TEXT_MAIN)
        self.status_label.pack(anchor="w")

        self.status_sub = tk.Label(status_text_frame, text="Server is not running",
                                   font=(FONT, 9), bg=PANEL, fg=TEXT_DIM)
        self.status_sub.pack(anchor="w")

        self.url_label = tk.Label(status_card, text="",
                                  font=(FONT, 9, "underline"), bg=PANEL,
                                  fg=ACCENT, cursor="hand2")
        self.url_label.pack(anchor="w", pady=(8, 0))
        self.url_label.bind("<Button-1>", self._open_url)

        # Buttons
        btn_row = tk.Frame(root_frame, bg=BG)
        btn_row.pack(fill="x", pady=(0, 14))

        self.start_btn = ttk.Button(btn_row, text="▶  Start Server",
                                    style="Start.TButton",
                                    command=self.start_services)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self.stop_btn = ttk.Button(btn_row, text="■  Stop Server",
                                   style="Stop.TButton",
                                   command=self.stop_services,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=(6, 0))

        # Activity log
        tk.Label(root_frame, text="ACTIVITY LOG", font=(FONT, 8, "bold"),
                 bg=BG, fg=TEXT_DIM).pack(anchor="w")

        log_frame = tk.Frame(root_frame, bg=PANEL_ALT)
        log_frame.pack(fill="both", expand=True, pady=(4, 0))

        self.log_text = tk.Text(
            log_frame, bg=PANEL_ALT, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN, font=("Consolas", 9),
            relief="flat", padx=10, pady=8,
            state="disabled", wrap="word",
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        self._log('Ready. Click "Start Server" to begin.')

    def _open_url(self, event=None):
        if self._session and self._session.django_port:
            webbrowser.open(f"http://127.0.0.1:{self._session.django_port}")

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 12, 12, fill=color, outline="")

    def _set_status(self, state, session=None):
        """Update the status card. Main thread only."""
        if state == "starting":
            self._draw_dot(AMBER)
            self.status_label.config(text="Starting…")
            self.status_sub.config(text="Launching background services")
            self.url_label.config(text="")
        elif state == "running":
            self._draw_dot(GREEN)
            self.status_label.config(text="Running")
            self.status_sub.config(text="Server is live")
            port = session.django_port if session else (self._session.django_port if self._session else None)
            if port:
                self.url_label.config(
                    text=(
                        f"LOCAL: http://127.0.0.1:{port}\n"
                        f"LAN:   http://{self.local_ip}:{port}"
                    )
                )
            else:
                self.url_label.config(text="")
        elif state == "stopping":
            self._draw_dot(AMBER)
            self.status_label.config(text="Stopping…")
            self.status_sub.config(text="Shutting down services")
            self.url_label.config(text="")
        elif state == "stopped":
            self._draw_dot(RED)
            self.status_label.config(text="Stopped")
            self.status_sub.config(text="Server is not running")
            self.url_label.config(text="")
        elif state == "error":
            self._draw_dot(RED)
            self.status_label.config(text="Error")
            self.status_sub.config(text="Server failed to start — see log")
            self.url_label.config(text="")

    # =========================================================================
    # WhatsApp health check
    # =========================================================================

    def _whatsapp_health_check(self, port, timeout=25):
        url = f"http://127.0.0.1:{port}/status"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        try:
                            data = json.loads(resp.read(4096))
                            return True, data.get("status")
                        except Exception:
                            return True, None
            except Exception:
                pass
            time.sleep(0.5)
        return False, None

    # =========================================================================
    # WhatsApp background thread
    # =========================================================================

    def prepare_and_launch_whatsapp(self, service_dir, session):
        """
        Background thread for Node.js WhatsApp service.
        Session-owned process management and stream reader threads.
        """
        if not self._is_valid_session(session):
            return

        node_cmd = get_node_executable()

        # Check Node.js runtime availability
        node_available = False
        try:
            r = _run_subprocess([node_cmd, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                node_available = True
                self._thread_log(f"Using Node.js ({node_cmd}): {r.stdout.strip()}")
            else:
                self._thread_log("ERROR: Node.js version check returned non-zero.")
        except FileNotFoundError:
            self._thread_log(f"ERROR: Node.js runtime '{node_cmd}' was not found.")
        except subprocess.TimeoutExpired:
            self._thread_log("ERROR: Node.js version check timed out.")

        server_js_path    = os.path.join(service_dir, "server.js")
        package_json_path = os.path.join(service_dir, "package.json")

        if not node_available:
            self._thread_log("WhatsApp service cannot start without Node.js.")
            return

        if not os.path.isdir(service_dir):
            self._thread_log(f"ERROR: WhatsApp service directory not found: {service_dir}")
            return

        if not os.path.isfile(server_js_path):
            self._thread_log("ERROR: whatsapp_service/server.js not found.")
            return

        if not os.path.isfile(package_json_path):
            self._thread_log("ERROR: whatsapp_service/package.json not found.")
            return

        if not self._is_valid_session(session):
            return

        wa_port = session.whatsapp_port
        if wa_port is None:
            self._thread_log("ERROR: WhatsApp port was not assigned. Cannot start service.")
            return

        self._thread_log(f"Starting WhatsApp automation service on port {wa_port}...")

        popen_kwargs = {
            "cwd": service_dir,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True

        env = os.environ.copy()
        env["WA_PORT"]             = str(wa_port)
        env["WA_API_KEY"]          = self.whatsapp_api_key
        env["WA_SESSION_DIR"]      = str(get_whatsapp_session_dir())
        env["WA_LOG_DIR"]          = str(get_logs_dir())
        env["SCHOOL_ERP_DATA_DIR"] = str(get_data_dir())

        chromium_bin = get_chromium_executable()
        if chromium_bin:
            env["CHROME_PATH"] = chromium_bin
            self._thread_log(f"Using Chromium runtime at: {chromium_bin}")

        launched_process = None
        for _attempt in range(1, 4):
            if not self._is_valid_session(session):
                return

            try:
                process = subprocess.Popen(
                    [node_cmd, "server.js"],
                    env=env,
                    **popen_kwargs,
                )
            except FileNotFoundError:
                self._thread_log(f"ERROR: node executable '{node_cmd}' not found during launch.")
                return
            except Exception as exc:
                self._thread_log(f"ERROR: Failed to launch WhatsApp service: {exc}")
                return

            # Atomic session ownership check right after Popen
            if not self._is_valid_session(session):
                _stop_process_tree(process, "WhatsApp service (stale session)", log_fn=None)
                return

            session.node_process = process

            # Dedicated daemon reader threads for stdout/stderr to prevent pipe deadlocks
            def _stream_reader(pipe, stream_name, sess):
                try:
                    for line in iter(pipe.readline, b""):
                        if not line or not self._is_valid_session(sess):
                            break
                        text = line.decode("utf-8", errors="replace").rstrip()
                        if text:
                            if self.whatsapp_api_key:
                                text = text.replace(self.whatsapp_api_key, "[REDACTED]")
                            if any(k in text for k in ("listening", "Error", "INITIALIZING", "READY", "QR", "STARTING")):
                                self._thread_log(f"[{stream_name}] {text}")
                except Exception:
                    pass
                finally:
                    try:
                        pipe.close()
                    except Exception:
                        pass

            threading.Thread(
                target=_stream_reader,
                args=(process.stdout, "WhatsApp", session),
                daemon=True,
                name=f"wa-stdout-{session.id}",
            ).start()

            threading.Thread(
                target=_stream_reader,
                args=(process.stderr, "WhatsApp-Err", session),
                daemon=True,
                name=f"wa-stderr-{session.id}",
            ).start()

            time.sleep(1.5)

            exit_code = process.poll()
            if exit_code is None:
                if not self._is_valid_session(session):
                    _stop_process_tree(process, "WhatsApp service (stale session)", log_fn=None)
                    session.node_process = None
                    return
                launched_process = process
                break

            # Exit detected
            if not self._is_valid_session(session):
                session.node_process = None
                return

            try:
                wa_port = find_free_port(wa_port + 1, host="127.0.0.1")
                session.whatsapp_port = wa_port
                env["WA_PORT"] = str(wa_port)
                if self._is_valid_session(session):
                    os.environ["WA_PORT"] = str(wa_port)
                self._thread_log(f"WhatsApp port conflict. Retrying on port {wa_port}...")
                continue
            except RuntimeError as exc2:
                self._thread_log(f"ERROR: No available port for WhatsApp service: {exc2}")
                session.node_process = None
                return
        else:
            self._thread_log("ERROR: WhatsApp service failed to start after multiple port attempts.")
            return

        # Health check
        self._thread_log("Waiting for WhatsApp service to respond...")
        reachable, wa_status = self._whatsapp_health_check(wa_port, timeout=25)

        if not self._is_valid_session(session):
            return

        if reachable:
            self._thread_log(f"WhatsApp automation service is listening on port {wa_port}.")
            if wa_status == "READY":
                self._thread_log("WhatsApp client is connected and ready.")
            elif wa_status in ("QR_RECEIVED", "INITIALIZING", "AUTHENTICATED", "STARTING"):
                self._thread_log(
                    f"WhatsApp client status: {wa_status}. "
                    "Open the dashboard to scan the QR code if prompted."
                )
            elif wa_status in ("DISCONNECTED", "ERROR", "AUTHENTICATION_FAILED", "RESTART_WAIT"):
                self._thread_log(
                    f"WhatsApp client status: {wa_status}. "
                    "The dashboard will show more details."
                )
            elif wa_status:
                self._thread_log(f"WhatsApp client status: {wa_status}.")
        else:
            self._thread_log(
                "WhatsApp service started but health check timed out "
                "(Chromium/Puppeteer may still be initializing)."
            )

    # =========================================================================
    # Django / Waitress background thread
    # =========================================================================

    def run_waitress(self, session):
        """
        Background thread for Waitress/Django server.
        Session-owned server instance and port binding.
        """
        self._thread_log("Loading Django & Waitress handlers...")

        try:
            import django
            django.setup()
            from django.core.management import call_command
            from core.paths import get_database_path

            db_file = get_database_path()
            if not db_file.exists() or db_file.stat().st_size == 0:
                self._thread_log("Initializing database for first-time launch...")
                call_command("migrate", interactive=False, verbosity=0)
                self._thread_log("Database initialized successfully.")

            from django.contrib.staticfiles.handlers import StaticFilesHandler
            from school_erp.wsgi import application
            from waitress.server import create_server
        except Exception as exc:
            self._thread_log(f"ERROR: Failed to load Django/Waitress: {exc}")
            if self._is_valid_session(session):
                self.root.after(0, lambda: self._handle_session_failure(session, f"Django error: {exc}"))
            return

        if not self._is_valid_session(session):
            return

        server = None
        for _attempt in range(1, 6):
            if not self._is_valid_session(session):
                return
            try:
                server = create_server(
                    StaticFilesHandler(application),
                    host="0.0.0.0",
                    port=session.django_port,
                )
                break
            except OSError as exc:
                msg = str(exc).lower()
                if "address already in use" in msg or "port is already allocated" in msg:
                    self._thread_log(f"Django port {session.django_port} was taken. Retrying...")
                    try:
                        new_port = find_free_port(session.django_port + 1)
                        session.django_port = new_port
                        self._thread_log(f"Waitress port re-selected: {session.django_port}")
                        continue
                    except RuntimeError as port_exc:
                        self._thread_log(f"ERROR: Could not find available Django port. {port_exc}")
                        if self._is_valid_session(session):
                            self.root.after(0, lambda: self._handle_session_failure(session, str(port_exc)))
                        return
                self._thread_log(f"ERROR: Waitress failed to bind: {exc}")
                if self._is_valid_session(session):
                    self.root.after(0, lambda e=exc: self._handle_session_failure(session, f"Waitress bind failed: {e}"))
                return
        else:
            self._thread_log("ERROR: Django failed to start after multiple port attempts.")
            if self._is_valid_session(session):
                self.root.after(0, lambda: self._handle_session_failure(session, "Port allocation exhausted"))
            return

        if not self._is_valid_session(session):
            try:
                server.close()
            except Exception:
                pass
            return

        session.server_instance = server

        if not self._is_valid_session(session):
            try:
                server.close()
            except Exception:
                pass
            session.server_instance = None
            return

        # Mark session live on main thread
        def _mark_live():
            if self._is_valid_session(session):
                self.is_running = True
                self._set_status("running", session)
                self._log(
                    f"Django server is listening on {self.local_ip}:{session.django_port} (LAN accessible)."
                )

        self.root.after(0, _mark_live)
        session.server_ready.set()

        try:
            server.run()
        except Exception as exc:
            self._thread_log(f"Waitress server error: {exc}")
        finally:
            def _on_django_exit():
                if self._is_valid_session(session):
                    self.is_running = False
                    self._begin_shutdown(destroy_after=False)

            self.root.after(0, _on_django_exit)

    # =========================================================================
    # Session failure handler (main thread)
    # =========================================================================

    def _handle_session_failure(self, session, message):
        """Called on main thread when a session fails to start."""
        if self._is_valid_session(session):
            self._log(f"ERROR: {message}")
            self._set_status("error")
            self._begin_shutdown(destroy_after=False)

    # =========================================================================
    # Browser opener (background thread)
    # =========================================================================

    def open_browser_delayed(self, session):
        """Waits on session.server_ready and TCP port check, then opens browser."""
        if not session.server_ready.wait(timeout=30):
            if self._is_valid_session(session):
                self._thread_log("Timed out waiting for Django to become ready.")
            return

        if not self._is_valid_session(session):
            return

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self._is_valid_session(session):
                return
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    if sock.connect_ex(("127.0.0.1", session.django_port)) == 0:
                        break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            if self._is_valid_session(session):
                self._thread_log("Timed out waiting for Django port to open.")
            return

        if not self._is_valid_session(session):
            return

        url = f"http://127.0.0.1:{session.django_port}"
        try:
            webbrowser.open(url)
            self._thread_log(f"Opened browser at {url}")
        except Exception as exc:
            self._thread_log(f"Could not open browser: {exc}")

    # =========================================================================
    # Start services
    # =========================================================================

    def start_services(self):
        """Main thread entry point to start services under a new ServerSession."""
        if self.is_running or self._stopping or self._session is not None:
            return

        session_id = self._new_startup_id()
        session = ServerSession(session_id)
        self._session = session
        self._stopping = False

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self._set_status("starting")
        self._log("Starting server…")

        try:
            session.django_port = find_free_port(8000)
            self._log(f"Waitress port selected: {session.django_port}")
            session.whatsapp_port = find_free_port(3000, host="127.0.0.1")
            self._log(f"WhatsApp service port selected: {session.whatsapp_port}")

            os.environ["WA_PORT"] = str(session.whatsapp_port)
            os.environ["WA_API_KEY"] = self.whatsapp_api_key
        except Exception as exc:
            self._log(f"ERROR: Could not find available ports: {exc}")
            self._set_status("error")
            self._session = None
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            return

        base_dir = _get_base_dir()
        service_dir = os.path.join(base_dir, "whatsapp_service")

        threading.Thread(
            target=self.prepare_and_launch_whatsapp,
            args=(service_dir, session),
            daemon=True,
            name=f"wa-{session_id}",
        ).start()

        threading.Thread(
            target=self.run_waitress,
            args=(session,),
            daemon=True,
            name=f"django-{session_id}",
        ).start()

        threading.Thread(
            target=self.open_browser_delayed,
            args=(session,),
            daemon=True,
            name=f"browser-{session_id}",
        ).start()

    # =========================================================================
    # Unified Shutdown
    # =========================================================================

    def _begin_shutdown(self, destroy_after=False):
        """
        Unified shutdown entry point.
        Invalidates active session, captures process/server references, and tears down in worker thread.
        """
        if self._stopping and not destroy_after:
            return

        self._stopping = True
        self.is_running = False

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)

        if not destroy_after:
            self._set_status("stopping")
            self._log("Stopping server…")

        # Capture and clear active session
        session = self._session
        self._session = None

        if session:
            session.cancelled.set()
            session.server_ready.set()  # Unblock browser thread

        def _worker():
            bg_log = self._make_bg_log()
            if session:
                if session.node_process:
                    _stop_process_tree(session.node_process, "WhatsApp service", log_fn=bg_log)
                    session.node_process = None
                if session.server_instance:
                    try:
                        session.server_instance.close()
                        bg_log("Waitress server closed.")
                    except Exception as exc:
                        bg_log(f"Error closing Waitress: {exc}")
                    session.server_instance = None

            def _finish():
                self._stopping = False
                if destroy_after:
                    self.root.destroy()
                else:
                    self._set_status("stopped")
                    self.start_btn.config(state=tk.NORMAL)
                    self.stop_btn.config(state=tk.DISABLED)

            self.root.after(0, _finish)

        threading.Thread(
            target=_worker,
            daemon=True,
            name="shutdown-worker",
        ).start()

    def stop_services(self):
        """Stop button callback."""
        self._begin_shutdown(destroy_after=False)

    def on_closing(self):
        """Window close (X button) handler."""
        server_running = self.is_running
        node_running   = (
            self._session is not None
            and self._session.node_process is not None
            and self._session.node_process.poll() is None
        )

        if server_running or node_running:
            if not messagebox.askokcancel(
                "Quit",
                f"{APP_NAME} services are still running. Stop them and quit?",
            ):
                return

        self._begin_shutdown(destroy_after=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    app = ServerApp(root)
    root.mainloop()
