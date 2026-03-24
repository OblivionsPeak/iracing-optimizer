import os
import subprocess
import time
import winreg
from pathlib import Path
from typing import Optional, Callable

try:
    import psutil
except ImportError:
    raise ImportError("psutil is required: pip install psutil")

IRACING_EXE_NAME = "iRacingSim64DX11.exe"
IRACING_PROCESS_NAME = "iRacingSim64DX11"


class ProcessController:

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None

    def find_iracing_exe(self) -> Path:
        """
        Find iRacingSim64DX11.exe.
        Strategy:
        1. Check registry: HKLM\\SOFTWARE\\WOW6432Node\\iRacing.com\\iRacing -> InstallPath
        2. Fall back to: C:\\Program Files\\iRacing\\iRacingSim64DX11.exe
        3. Fall back to: C:\\Program Files (x86)\\iRacing\\iRacingSim64DX11.exe
        Raises FileNotFoundError if not found anywhere.
        """
        # 1. Registry lookup
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\iRacing.com\iRacing",
            )
            install_path, _ = winreg.QueryValueEx(key, "InstallPath")
            winreg.CloseKey(key)
            exe = Path(install_path) / IRACING_EXE_NAME
            if exe.exists():
                return exe
        except (FileNotFoundError, OSError, Exception):
            pass

        # 2. Default Program Files locations
        fallback_dirs = [
            Path(r"C:\Program Files\iRacing"),
            Path(r"C:\Program Files (x86)\iRacing"),
        ]
        for directory in fallback_dirs:
            exe = directory / IRACING_EXE_NAME
            if exe.exists():
                return exe

        raise FileNotFoundError(
            f"Could not find {IRACING_EXE_NAME}. "
            "Verify iRacing is installed or set the registry key "
            r"HKLM\SOFTWARE\WOW6432Node\iRacing.com\iRacing -> InstallPath."
        )

    def find_replay_files(self) -> list[Path]:
        """
        Return list of .rpy files in Documents\\iRacing\\replay\\, sorted by
        modification time (newest first). Returns empty list if directory
        doesn't exist.
        """
        replay_dir = Path.home() / "Documents" / "iRacing" / "replay"
        if not replay_dir.exists():
            return []

        rpy_files = [f for f in replay_dir.iterdir() if f.suffix == ".rpy"]
        rpy_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return rpy_files

    def is_iracing_running(self) -> bool:
        """Check if iRacingSim64DX11 process is currently running using psutil."""
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] == IRACING_EXE_NAME:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False

    def kill_iracing(self, timeout: float = 8.0) -> bool:
        """
        Gracefully terminate iRacing.
        1. Send SIGTERM / terminate() to all iRacingSim64DX11 processes
        2. Wait up to timeout seconds for them to exit
        3. Force kill any remaining
        Returns True if at least one process was found and killed,
        False if it wasn't running.
        """
        procs: list[psutil.Process] = [
            p
            for p in psutil.process_iter(["name", "pid"])
            if p.info["name"] == IRACING_EXE_NAME
        ]

        if not procs:
            return False

        for p in procs:
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass

        _gone, alive = psutil.wait_procs(procs, timeout=timeout)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass

        return True

    def launch_replay(
        self,
        replay_path: Path,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> subprocess.Popen:
        """
        Launch iRacing with the specified replay file.
        1. Find iRacing exe via find_iracing_exe()
        2. Run: iRacingSim64DX11.exe /loadReplay "replay_path"
        3. Store process handle in self._process
        4. Call progress_cb with launch status messages
        Returns the Popen handle.
        """
        if progress_cb:
            progress_cb("Locating iRacing executable...")

        exe_path = self.find_iracing_exe()

        if progress_cb:
            progress_cb(f"Found iRacing at: {exe_path}")

        if not replay_path.exists():
            raise FileNotFoundError(f"Replay file not found: {replay_path}")

        if progress_cb:
            progress_cb(f"Launching iRacing with replay: {replay_path.name}")

        self._process = subprocess.Popen(
            [str(exe_path), "/loadReplay", str(replay_path)],
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        if progress_cb:
            progress_cb(
                f"iRacing launched (PID: {self._process.pid}). "
                "Waiting for process to initialise..."
            )

        return self._process

    def wait_for_process_stable(
        self,
        timeout: float = 60.0,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """
        Wait until iRacing process CPU usage stabilises (indicates loading complete).
        Strategy: poll psutil process CPU % every 2 seconds.
        Consider "stable" when CPU drops below 30% for 3 consecutive readings.
        Returns True if stable within timeout, False if timeout exceeded.
        Calls progress_cb every 5 seconds with:
        "Waiting for iRacing to load... Xs elapsed"
        """
        start_time = time.monotonic()
        consecutive_low = 0
        required_low = 3
        cpu_threshold = 30.0
        poll_interval = 2.0
        report_interval = 5.0
        last_report_time = start_time
        first_reading_skipped = False

        while True:
            elapsed = time.monotonic() - start_time

            if elapsed >= timeout:
                if progress_cb:
                    progress_cb(
                        f"Timeout reached after {timeout:.0f}s waiting for iRacing to stabilise."
                    )
                return False

            # Find the iRacing process
            target_proc: Optional[psutil.Process] = None
            for proc in psutil.process_iter(["name", "pid"]):
                try:
                    if proc.info["name"] == IRACING_EXE_NAME:
                        target_proc = proc
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            if target_proc is None:
                # Process not found yet — keep waiting
                time.sleep(poll_interval)
                continue

            try:
                # First call on a fresh process returns 0.0; skip it
                cpu = target_proc.cpu_percent(interval=poll_interval)
                if not first_reading_skipped:
                    first_reading_skipped = True
                    continue

                if cpu < cpu_threshold:
                    consecutive_low += 1
                else:
                    consecutive_low = 0

                if consecutive_low >= required_low:
                    if progress_cb:
                        progress_cb(
                            f"iRacing stabilised after {elapsed:.0f}s "
                            f"(CPU: {cpu:.1f}%)."
                        )
                    return True

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                time.sleep(poll_interval)
                continue

            # Periodic progress report
            now = time.monotonic()
            if progress_cb and (now - last_report_time) >= report_interval:
                last_report_time = now
                progress_cb(
                    f"Waiting for iRacing to load... {int(elapsed)}s elapsed"
                )

    def get_process_info(self) -> Optional[dict]:
        """
        Returns dict with process info if iRacing is running:
        {"pid": int, "cpu_pct": float, "memory_mb": float, "status": str}
        Returns None if not running.
        """
        for proc in psutil.process_iter(["name", "pid", "status"]):
            try:
                if proc.info["name"] != IRACING_EXE_NAME:
                    continue

                cpu_pct: float = proc.cpu_percent(interval=0.1)
                mem_info = proc.memory_info()
                memory_mb: float = mem_info.rss / (1024 * 1024)
                status: str = proc.info["status"]

                return {
                    "pid": proc.info["pid"],
                    "cpu_pct": cpu_pct,
                    "memory_mb": memory_mb,
                    "status": status,
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return None
