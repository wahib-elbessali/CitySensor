"""
run.py — City Sensor Dashboard unified launcher
================================================
Starts Redis, Server B, Server A, and the Vite dev server as child processes.
All output is merged into one terminal with color-coded prefixes.
ALL child processes are killed when this script exits (Ctrl+C or otherwise).

Usage:
    python run.py
    python run.py --sensors 200 --step-delay 0.05
    python run.py --redis-host 192.168.1.10 --redis-port 6380
    python run.py --no-browser
    python run.py --no-redis   # if Redis is already running externally
"""

import argparse
import atexit
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ── Ensure UTF-8 output encoding to prevent UnicodeEncodeError in some Windows environments ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# ── ANSI color support (Windows needs this enabled) ───────────────────────────
if sys.platform == "win32":
    os.system("")   # enables ANSI escape codes in Windows terminals

COLORS = {
    "Redis":    "\033[95m",   # Magenta
    "Server-B": "\033[94m",   # Blue
    "Server-A": "\033[92m",   # Green
    "Vite":     "\033[96m",   # Cyan
    "Launcher": "\033[93m",   # Yellow
    "ERROR":    "\033[91m",   # Red
    "RESET":    "\033[0m",
}

def cprint(label: str, msg: str, color: str = ""):
    c = color or COLORS.get(label, COLORS["Launcher"])
    reset = COLORS["RESET"]
    prefix = f"{c}[{label:<8s}]{reset}"
    print(f"{prefix} {msg}", flush=True)


# ── Process registry — all child PIDs tracked here ───────────────────────────
_procs: list[subprocess.Popen] = []
_cleanup_done = False


def kill_all():
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    cprint("Launcher", "Shutting down all child processes...")
    for proc in _procs:
        if proc.poll() is None:
            try:
                if sys.platform == "win32":
                    # taskkill /T kills the process tree (e.g. npm → node)
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True
                    )
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                pass
    cprint("Launcher", "All processes stopped. Goodbye.")


atexit.register(kill_all)


def _signal_handler(sig, frame):
    print()
    sys.exit(0)   # triggers atexit → kill_all


signal.signal(signal.SIGINT, _signal_handler)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _signal_handler)


# ── Log tailer — streams stdout/stderr from a child process ──────────────────
def _tail(proc: subprocess.Popen, label: str, stream_name: str):
    stream = getattr(proc, stream_name, None)
    if stream is None:
        return
    for raw in iter(stream.readline, b""):
        text = raw.decode("utf-8", errors="replace").rstrip()
        if text:
            cprint(label, text)


def start_tailing(proc: subprocess.Popen, label: str):
    for attr in ("stdout", "stderr"):
        t = threading.Thread(target=_tail, args=(proc, label, attr), daemon=True)
        t.start()


# ── Launch helper ─────────────────────────────────────────────────────────────
def launch(label: str, cmd: list, cwd: Path) -> subprocess.Popen:
    cmd_str = " ".join(str(c) for c in cmd)
    cprint("Launcher", f"Starting {label}: {cmd_str}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _procs.append(proc)
    start_tailing(proc, label)
    return proc


# ── Dependency helpers ────────────────────────────────────────────────────────
def redis_is_running(host: str, port: int) -> bool:
    """Check Redis connectivity via TCP socket — works even when redis-cli
    is not on the Windows PATH (e.g. Redis is running inside WSL)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _find_wsl_distro() -> str | None:
    """Query WSL for available distributions and return the name of the preferred
    distro (e.g. Ubuntu or Debian) to launch Redis in. This prevents using minimal
    default distros like 'docker-desktop' that lack redis-server."""
    import subprocess
    import shutil
    if not shutil.which("wsl"):
        return None
    try:
        # Run wsl -l -q to get clean distribution names
        res = subprocess.run(["wsl", "-l", "-q"], capture_output=True, timeout=3)
        # Try decoding as utf-16-le (WSL's default output encoding) or utf-8
        out = ""
        try:
            out = res.stdout.decode("utf-16-le")
        except Exception:
            out = res.stdout.decode("utf-8", errors="ignore")
        
        distros = [line.strip() for line in out.splitlines() if line.strip()]
        
        # If wsl -l -q is empty/fails, parse wsl -l
        if not distros:
            res = subprocess.run(["wsl", "-l"], capture_output=True, timeout=3)
            try:
                out = res.stdout.decode("utf-16-le")
            except Exception:
                out = res.stdout.decode("utf-8", errors="ignore")
            distros = []
            for line in out.splitlines():
                cleaned = line.replace("*", "").strip()
                if cleaned and "Windows Subsystem" not in cleaned and "Distribution" not in cleaned:
                    distros.append(cleaned.split()[0])
        
        # Prefer Ubuntu or Debian if present
        for d in distros:
            if "ubuntu" in d.lower() or "debian" in d.lower():
                return d
        # Fallback to first non-docker distro
        for d in distros:
            if "docker" not in d.lower():
                return d
        if distros:
            return distros[0]
    except Exception:
        pass
    return None


def _redis_launch_cmd() -> list[str]:
    """Return the command to launch redis-server.
    On Windows, calls the binary via WSL targeting the Ubuntu distro specifically
    if Windows native redis-server is missing, avoiding 'docker-desktop' traps."""
    import shutil
    import subprocess
    if shutil.which("redis-server"):
        return ["redis-server"]
    if sys.platform == "win32" and shutil.which("wsl"):
        distro = _find_wsl_distro()
        if distro:
            cprint("Launcher", f"redis-server not on Windows PATH — launching via WSL distro '{distro}'.")
            try:
                # Safely start the redis-server daemon in WSL.
                # If it is already running, this exits cleanly without error.
                subprocess.run(["wsl", "-d", distro, "redis-server", "--daemonize", "yes"], timeout=5, capture_output=True)
            except Exception:
                pass
            # Return a persistent dummy command to keep the WSL port forwarding bridge active.
            return ["wsl", "-d", distro, "tail", "-f", "/dev/null"]
        else:
            cprint("Launcher", "redis-server not on Windows PATH — launching via default WSL distro.")
            try:
                subprocess.run(["wsl", "redis-server", "--daemonize", "yes"], timeout=5, capture_output=True)
            except Exception:
                pass
            return ["wsl", "tail", "-f", "/dev/null"]
    return ["redis-server"]  # will surface a clear OS error if truly missing


def missing_python_packages() -> list[str]:
    checks = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "redis":   "redis",
        "noise":   "noise",
        "PIL":     "Pillow",
        "scipy":   "scipy",
        "numpy":   "numpy",
    }
    missing = []
    for import_name, install_name in checks.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(install_name)
    return missing


# ── Config writer ─────────────────────────────────────────────────────────────
def write_config(root: Path, sensors: int, step_delay: float,
                 redis_host: str, redis_port: int):
    zone_b_start = sensors + 1
    content = f'''\
"""
config.py — Shared configuration for the city sensor simulation cluster.
Auto-generated by run.py. Pass flags to run.py to change these values.
"""

# ─── Redis ────────────────────────────────────────────────────────────────────
REDIS_HOST = "{redis_host}"
REDIS_PORT = {redis_port}
REDIS_DB   = 0

# ─── Simulation ───────────────────────────────────────────────────────────────
STEP_DELAY             = {step_delay}    # seconds between time-step publishes
NUM_SENSORS_PER_SERVER = {sensors}
ZONE_A_START           = 1
ZONE_B_START           = {zone_b_start}

# ─── CORS ─────────────────────────────────────────────────────────────────────
# Change to ["http://localhost:5173"] for a production deployment.
CORS_ORIGINS = ["*"]
'''
    (root / "config.py").write_text(content, encoding="utf-8")
    cprint("Launcher", (
        f"config.py written — sensors={sensors}, step_delay={step_delay}, "
        f"redis={redis_host}:{redis_port}, zone_b_start={zone_b_start}"
    ))


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    root = Path(__file__).parent

    parser = argparse.ArgumentParser(
        description="City Sensor Dashboard — unified launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--sensors",    type=int,   default=150,         help="Sensors per server")
    parser.add_argument("--step-delay", type=float, default=0.1,         help="Seconds between simulation steps")
    parser.add_argument("--redis-host", type=str,   default="127.0.0.1", help="Redis host")
    parser.add_argument("--redis-port", type=int,   default=6379,        help="Redis port")
    parser.add_argument("--no-browser", action="store_true",             help="Skip auto-opening the browser")
    parser.add_argument("--no-redis",   action="store_true",             help="Skip launching redis-server (use if Redis is already running)")
    args = parser.parse_args()

    cprint("Launcher", "=" * 52)
    cprint("Launcher", "  City Sensor Dashboard — Unified Launcher")
    cprint("Launcher", "=" * 52)

    # ── 1. Check + install Python packages ────────────────────────────────────
    cprint("Launcher", "Checking Python dependencies...")
    missing = missing_python_packages()
    if missing:
        cprint("Launcher", f"Installing: {missing}", COLORS["ERROR"])
        subprocess.run([sys.executable, "-m", "pip", "install"] + missing, check=True)
        cprint("Launcher", "Packages installed.")
    else:
        cprint("Launcher", "All Python packages present.")

    # ── 2. Check node_modules ─────────────────────────────────────────────────
    dashboard_dir = root / "city-dashboard"
    if not (dashboard_dir / "node_modules").exists():
        cprint("Launcher", "node_modules missing — running npm install...")
        subprocess.run(["npm", "install"], cwd=str(dashboard_dir), check=True)

    # ── 3. Write config.py ────────────────────────────────────────────────────
    write_config(root, args.sensors, args.step_delay, args.redis_host, args.redis_port)

    # ── 4. Redis ──────────────────────────────────────────────────────────────
    redis_proc = None
    if redis_is_running(args.redis_host, args.redis_port):
        cprint("Launcher", "Redis already running — skipping launch.")
    elif args.no_redis:
        cprint("Launcher", "Redis not running and --no-redis set. Aborting.", COLORS["ERROR"])
        sys.exit(1)
    else:
        redis_proc = launch("Redis", _redis_launch_cmd(), cwd=root)
        cprint("Launcher", "Waiting for Redis to be ready...")
        for _ in range(20):
            time.sleep(0.5)
            if redis_is_running(args.redis_host, args.redis_port):
                cprint("Redis", "Ready.")
                break
        else:
            cprint("Launcher", "Redis did not start in time. Aborting.", COLORS["ERROR"])
            sys.exit(1)

        # Settle time: WSL VM boot and network port forwarding can be unstable for a couple of seconds.
        # Wait 3 seconds to ensure the forwarding channel is fully established and stable.
        cprint("Launcher", "Waiting 3 seconds for WSL network forwarding to settle...")
        time.sleep(3)

    # ── 5. Server B ───────────────────────────────────────────────────────────
    launch("Server-B", [sys.executable, "-u", "server_b.py"], cwd=root)
    time.sleep(1)

    # ── 6. Server A ───────────────────────────────────────────────────────────
    launch("Server-A", [sys.executable, "-u", "server_a.py"], cwd=root)
    time.sleep(2)

    # ── 7. Vite ───────────────────────────────────────────────────────────────
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    launch("Vite", [npm, "run", "dev"], cwd=dashboard_dir)

    # ── 8. Open browser ───────────────────────────────────────────────────────
    if not args.no_browser:
        time.sleep(2)
        cprint("Launcher", "Opening http://localhost:5173 ...")
        webbrowser.open("http://localhost:5173")

    cprint("Launcher", "All services running. Press Ctrl+C to stop everything.")
    cprint("Launcher", "─" * 50)
    cprint("Launcher", "  Dashboard → http://localhost:5173")
    cprint("Launcher", "  API       → http://localhost:8001")
    cprint("Launcher", "─" * 50)

    # ── Keep alive — exit if any critical process dies unexpectedly ───────────
    critical = _procs[:]   # snapshot at launch time
    while True:
        time.sleep(1)
        for proc in critical:
            if proc.poll() is not None:
                # If the Redis process exited but Redis is still responsive, ignore it.
                # This happens if a background WSL service automatically took over.
                if proc == redis_proc and redis_is_running(args.redis_host, args.redis_port):
                    cprint("Launcher", "Foreground Redis process exited, but Redis server is still reachable. Continuing.")
                    critical.remove(proc)
                    continue

                cprint("Launcher",
                       f"A process (PID {proc.pid}) exited unexpectedly "
                       f"with code {proc.returncode}. Shutting down.",
                       COLORS["ERROR"])
                sys.exit(1)


if __name__ == "__main__":
    main()
