"""
benchmark_runner.py — Orchestrates a single benchmark iteration.

Write settings → restart iRacing → sample FPS → return BenchmarkResult.
Emits SSE-compatible events to an optional Queue[dict] for live UI updates.
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Optional

from .config_manager import ConfigManager
from .fps_sampler import FPSSample, FPSSampler
from .process_controller import ProcessController


@dataclass
class BenchmarkResult:
    settings: dict          # {key: value} applied for this run
    fps_sample: FPSSample
    iteration: int
    total_iterations: int
    duration_seconds: float


class BenchmarkRunner:
    """
    Runs a single benchmark iteration.
    Emits SSE-compatible events to an optional Queue[dict].
    """

    KILL_WAIT = 3.0        # seconds after kill before launching
    WARMUP_SECONDS = 10.0  # FPS stabilization wait after iRacing connects
    SAMPLE_SECONDS = 30.0  # FPS sampling duration

    def __init__(self,
                 config_manager: ConfigManager,
                 process_controller: ProcessController,
                 fps_sampler: FPSSampler,
                 replay_path: Path,
                 event_queue: Optional[Queue] = None):
        self.cm = config_manager
        self.pc = process_controller
        self.sampler = fps_sampler
        self.replay_path = replay_path
        self.event_queue = event_queue
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # SSE event helpers
    # ------------------------------------------------------------------

    def emit(self, msg_type: str, **kwargs) -> None:
        """Push an event to the queue for SSE streaming."""
        if self.event_queue:
            import time as _time
            self.event_queue.put({"type": msg_type, "ts": _time.time(), **kwargs})

    def log(self, msg: str) -> None:
        """Emit a log message."""
        self.emit("log", msg=msg)

    # ------------------------------------------------------------------
    # Core run logic
    # ------------------------------------------------------------------

    def run_single(self, settings_dict: dict,
                   iteration: int = 0,
                   total_iterations: int = 0) -> BenchmarkResult:
        """
        1. Apply settings to ini file
        2. Kill iRacing if running
        3. Wait KILL_WAIT seconds
        4. Launch iRacing with replay
        5. Wait for FPS sampler to connect and stabilize
        6. Sample FPS for SAMPLE_SECONDS
        7. Kill iRacing
        8. Return BenchmarkResult
        Raises RuntimeError if iRacing fails to connect within 120s.
        """
        run_start = time.monotonic()

        # Step 1 — apply settings
        self.log(f"[{iteration}/{total_iterations}] Applying {len(settings_dict)} setting(s)…")
        self.cm.apply_settings(settings_dict)

        # Step 2 — kill iRacing if running
        if self.pc.is_iracing_running():
            self.log("iRacing is running — terminating…")
            self.pc.kill_iracing()
        else:
            self.log("iRacing not running, skipping kill step.")

        # Step 3 — wait after kill
        self.log(f"Waiting {self.KILL_WAIT:.0f}s before launch…")
        for _ in range(int(self.KILL_WAIT * 10)):
            if self._stop_event.is_set():
                raise RuntimeError("Benchmark aborted via stop signal.")
            time.sleep(0.1)

        # Step 4 — launch iRacing with replay
        self.log(f"Launching iRacing with replay: {self.replay_path.name}")
        self.pc.launch_replay(
            self.replay_path,
            progress_cb=self.log,
        )

        # Step 5a — wait for SDK connection (120s timeout)
        self.log("Waiting for iRacing SDK to connect…")
        connected = self.sampler.wait_for_iracing(
            timeout=120.0,
            progress_cb=self.log,
        )
        if not connected:
            self.pc.kill_iracing()
            raise RuntimeError(
                "iRacing failed to connect within 120s. "
                "Replay may not have started."
            )

        if self._stop_event.is_set():
            self.pc.kill_iracing()
            raise RuntimeError("Benchmark aborted via stop signal.")

        # Step 5b — warmup / stabilization wait
        self.log(f"Stabilizing for {self.WARMUP_SECONDS:.0f}s…")
        self.sampler.wait_for_stable(
            stable_seconds=self.WARMUP_SECONDS,
            progress_cb=self.log,
        )

        if self._stop_event.is_set():
            self.pc.kill_iracing()
            raise RuntimeError("Benchmark aborted via stop signal.")

        # Step 6 — sample FPS
        self.log(f"Sampling FPS for {self.SAMPLE_SECONDS:.0f}s…")
        fps_sample = self.sampler.sample(
            duration_seconds=self.SAMPLE_SECONDS,
            progress_cb=self.log,
        )
        self.log(
            f"Sample complete — median: {fps_sample.median:.1f} fps, "
            f"p5: {fps_sample.p5:.1f} fps, "
            f"n={fps_sample.sample_count}"
        )

        # Step 7 — kill iRacing
        self.log("Killing iRacing after sample…")
        self.pc.kill_iracing()

        duration = time.monotonic() - run_start
        return BenchmarkResult(
            settings=settings_dict,
            fps_sample=fps_sample,
            iteration=iteration,
            total_iterations=total_iterations,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal this runner and its sampler to abort."""
        self._stop_event.set()
        self.sampler.stop()

    def reset(self) -> None:
        self._stop_event.clear()
        self.sampler.reset()
