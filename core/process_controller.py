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

IRACING_SIM_EXE   = "iRacingSim64DX11.exe"
IRACING_UI_EXE    = "iRacingUI.exe"
# Both process names to detect "iRacing is running"
IRACING_PROCESS_NAMES = {IRACING_SIM_EXE, IRACING_UI_EXE}

# Registry keys to search (newest installations use HKCU)
_REGISTRY_SEARCHES = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\iRacing.com\iRacing"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\iRacing.com\iRacing"),
    (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\iRacing.com\iRacing"),
]

_FALLBACK_DIRS = [
    Path(r"C:\Program Files\iRacing"),
    Path(r"C:\Program Files (x86)\iRacing"),
]


def _find_install_dir() -> Optional[Path]:
    """Return the iRacing install directory from registry or common paths."""
    for hive, subkey in _REGISTRY_SEARCHES:
        try:
            key = winreg.OpenKey(hive, subkey)
            install_path, _ = winreg.QueryValueEx(key, "InstallPath")
            winreg.CloseKey(key)
            p = Path(install_path)
            if p.exists():
                return p
        except (FileNotFoundError, OSError):
            pass

    for d in _FALLBACK_DIRS:
        if d.exists():
            return d

    return None


class ProcessController:

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None

    def find_iracing_exe(self) -> Path:
        """
        Find iRacingSim64DX11.exe for replay launching.
        Falls back to iRacingUI.exe if the sim exe is not present
        (though replay loading requires the sim exe).
        Raises FileNotFoundError if neither is found.
        """
        install_dir = _find_install_dir()

        if install_dir:
            sim_exe = install_dir / IRACING_SIM_EXE
            if sim_exe.exists():
                return sim_exe
            # iRacingUI.exe present but not the sim exe — likely installer-only state
            ui_exe = install_dir / IRACING_UI_EXE
            if ui_exe.exists():
                raise FileNotFoundError(
                    f"Found iRacingUI.exe at {install_dir} but not {IRACING_SIM_EXE}. "
                    "Launch iRacing at least once and let it finish updating before using this tool."
                )

        raise FileNotFoundError(
            f"Could not find {IRACING_SIM_EXE}. "
            "Verify iRacing is installed. Searched registry and common install paths."
        )

    def find_install_dir(self) -> Optional[Path]:
        """Public accessor for the install directory (used by UI to display path)."""
        return _find_install_dir()

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
        """Check if any iRacing process is running (iRacingUI.exe or iRacingSim64DX11.exe)."""
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] in IRACING_PROCESS_NAMES:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False

    def kill_iracing(self, timeout: float = 8.0) -> bool:
        """
        Gracefully terminate all iRacing processes (both iRacingUI.exe and iRacingSim64DX11.exe).
        Returns True if at least one process was found and killed, False if none were running.
        """
        procs: list[psutil.Process] = [
            p
            for p in psutil.process_iter(["name", "pid"])
            if p.info["name"] in IRACING_PROCESS_NAMES
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

        # iRacing must be fully closed before launching with /loadReplay.
        # If iRacingUI.exe or the sim is still running, /loadReplay is ignored.
        if self.is_iracing_running():
            if progress_cb:
                progress_cb("iRacing still running — killing all processes before relaunch...")
            self.kill_iracing(timeout=10.0)
            time.sleep(3.0)  # let OS release file handles

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
                    if proc.info["name"] in IRACING_PROCESS_NAMES:
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
                if proc.info["name"] not in IRACING_PROCESS_NAMES:
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
