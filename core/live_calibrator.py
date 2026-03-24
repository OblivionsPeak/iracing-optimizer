"""
live_calibrator.py — Two-phase live baseline calibrator.

Phase 1: run a replay benchmark at current settings via BenchmarkRunner.
Phase 2: wait for the user to be in a live iRacing session, then collect
         FPS until they stop driving or stop() is called.

Computes correction_factor = live_fps_p5 / replay_fps_p5 and writes
calibration.json via CalibrationStore.
"""

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue

from .benchmark_runner import BenchmarkRunner
from .calibration_store import CalibrationStore
from .config_manager import ConfigManager
from .fps_sampler import FPSSampler
from .process_controller import ProcessController

VALID_LIVE_TYPES: frozenset[str] = frozenset(
    {"practice", "race", "qualify", "open practice", "open qualify"}
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CalibrationAborted(Exception):
    """Raised when stop() is called before calibration completes."""


class CalibrationError(Exception):
    """Raised on unrecoverable failure (e.g. iRacing SDK import missing)."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CalibrationResult:
    replay_fps_p5: float
    replay_fps_median: float
    replay_fps_p95: float
    replay_sample_count: int
    live_fps_p5: float
    live_fps_median: float
    live_fps_p95: float
    live_sample_count: int
    session_type: str
    track: str
    car: str
    correction_factor: float
    collection_duration_seconds: float


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class LiveCalibrator:
    MIN_LIVE_SAMPLES = 600          # 60 s at 10 Hz minimum
    LIVE_POLL_INTERVAL = 0.1        # seconds between FPS polls in Phase 2
    PROGRESS_INTERVAL = 10.0        # emit cal_progress every N seconds
    SESSION_WAIT_TIMEOUT = 600.0    # max seconds to wait for a live session
    SESSION_CHECK_INTERVAL = 2.0    # how often to check for live session

    def __init__(
        self,
        config_manager: ConfigManager,
        process_controller: ProcessController,
        replay_path: Path,
        event_queue: Queue,
        mock_mode: bool = False,
        mock_live_fps: float = 80.0,
    ) -> None:
        self._cm = config_manager
        self._pc = process_controller
        self._replay_path = replay_path
        self._event_queue = event_queue
        self._mock_mode = mock_mode
        self._mock_live_fps = mock_live_fps

        self._stop_requested = False
        self._runner: BenchmarkRunner | None = None  # set during Phase 1

    # ------------------------------------------------------------------
    # SSE helpers
    # ------------------------------------------------------------------

    def emit(self, event_type: str, **kwargs) -> None:
        """Push an SSE-compatible event dict to the queue."""
        self._event_queue.put({"type": event_type, "ts": time.time(), **kwargs})

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop_requested = True
        if self._runner is not None:
            self._runner.stop()

    def signal_ready(self) -> None:
        """Forward the user's Ready signal to the Phase 1 benchmark runner."""
        if self._runner is not None:
            self._runner.signal_ready()

    def reset(self) -> None:
        self._stop_requested = False
        self._runner = None

    # ------------------------------------------------------------------
    # Phase helpers
    # ------------------------------------------------------------------

    def _check_stop(self) -> None:
        """Raise CalibrationAborted if stop has been requested."""
        if self._stop_requested:
            raise CalibrationAborted("Calibration aborted by user.")

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(self) -> CalibrationResult:
        """
        Execute the two-phase calibration.

        Phase 1 — replay benchmark at current settings.
        Phase 2 — wait for live session, collect FPS until done.

        Returns CalibrationResult and writes calibration.json.
        Raises CalibrationAborted if stop() is called.
        Raises CalibrationError on unrecoverable failure.
        """
        # ----------------------------------------------------------------
        # Phase 1: replay benchmark
        # ----------------------------------------------------------------
        self.emit("cal_phase1_start", msg="Starting Phase 1: replay benchmark…")

        current_settings = self._cm.get_all_tunable()
        sampler = FPSSampler(mock_mode=self._mock_mode, mock_target_fps=self._mock_live_fps)

        # Pass log events from BenchmarkRunner through to our queue.
        runner = BenchmarkRunner(
            config_manager=self._cm,
            process_controller=self._pc,
            fps_sampler=sampler,
            replay_path=self._replay_path,
            event_queue=self._event_queue,
        )
        self._runner = runner  # expose so signal_ready() can reach it

        try:
            bench = runner.run_single(
                settings_dict=current_settings,
                iteration=1,
                total_iterations=1,
            )
        except RuntimeError as exc:
            if self._stop_requested:
                self.emit("cal_aborted", msg="Calibration aborted during Phase 1.")
                raise CalibrationAborted(str(exc)) from exc
            self.emit("cal_error", msg=str(exc))
            raise CalibrationError(str(exc)) from exc

        self._check_stop()

        replay_sample = bench.fps_sample
        replay_fps_p5 = replay_sample.p5
        replay_fps_median = replay_sample.median
        replay_fps_p95 = replay_sample.p95
        replay_sample_count = replay_sample.sample_count

        self.emit(
            "cal_phase1_done",
            fps_p5=replay_fps_p5,
            fps_median=replay_fps_median,
            fps_p95=replay_fps_p95,
            sample_count=replay_sample_count,
        )

        # ----------------------------------------------------------------
        # Phase 2: wait for live session, collect FPS
        # ----------------------------------------------------------------
        live_fps_values: list[float] = []
        session_type = ""
        track = ""
        car = ""
        collection_start = 0.0

        if self._mock_mode:
            # Mock: skip irsdk, generate samples for 60 seconds
            self.emit("cal_phase2_start", msg="[Mock] Phase 2: collecting live FPS…",
                      session_type="Practice", track="Mock Track", car="Mock Car")
            session_type = "Practice"
            track = "Mock Track"
            car = "Mock Car"
            collection_start = time.monotonic()
            mock_duration = 60.0
            last_progress = collection_start

            while True:
                self._check_stop()
                elapsed = time.monotonic() - collection_start
                if elapsed >= mock_duration:
                    break

                fps = max(15.0, min(300.0, random.gauss(self._mock_live_fps, 8)))
                live_fps_values.append(fps)

                if time.monotonic() - last_progress >= self.PROGRESS_INTERVAL:
                    self.emit(
                        "cal_progress",
                        samples=len(live_fps_values),
                        elapsed=round(time.monotonic() - collection_start, 1),
                        msg=f"Collected {len(live_fps_values)} live samples…",
                    )
                    last_progress = time.monotonic()

                time.sleep(self.LIVE_POLL_INTERVAL)

        else:
            # Real mode: use irsdk directly
            try:
                import irsdk  # type: ignore[import-untyped]
            except ImportError as exc:
                msg = "pyirsdk not installed — cannot run live Phase 2."
                self.emit("cal_error", msg=msg)
                raise CalibrationError(msg) from exc

            ir = irsdk.IRSDK()

            # --- Wait for live session ---
            deadline = time.monotonic() + self.SESSION_WAIT_TIMEOUT
            detected = False

            self.emit("cal_waiting", msg="Waiting for a live iRacing session…")

            while time.monotonic() < deadline:
                self._check_stop()

                try:
                    if not ir.is_connected:
                        try:
                            ir.startup()
                        except Exception:
                            pass

                    if ir.is_connected:
                        is_replay = ir["IsReplayPlaying"]
                        session_t = (ir["SessionType"] or "").lower().strip()
                        if (not is_replay) and session_t in VALID_LIVE_TYPES:
                            detected = True
                            session_type = ir["SessionType"] or ""
                            break
                except Exception:
                    pass

                self.emit(
                    "cal_waiting",
                    msg="Waiting for live session…",
                    remaining=round(max(0.0, deadline - time.monotonic()), 0),
                )
                time.sleep(self.SESSION_CHECK_INTERVAL)

            if not detected:
                try:
                    ir.shutdown()
                except Exception:
                    pass
                if self._stop_requested:
                    self.emit("cal_aborted", msg="Calibration aborted while waiting for session.")
                    raise CalibrationAborted("Aborted while waiting for live session.")
                msg = "Timed out waiting for a live iRacing session."
                self.emit("cal_error", msg=msg)
                raise CalibrationError(msg)

            # --- Resolve track and car ---
            try:
                track = ir["TrackDisplayName"] or ""
            except Exception:
                track = ""

            try:
                player_idx = ir["PlayerCarIdx"]
                drivers = ir["DriverInfo"]["Drivers"]
                car = drivers[player_idx]["CarScreenNameShort"] or ""
            except Exception:
                car = ""

            self.emit(
                "cal_phase2_start",
                msg=f"Live session detected — collecting FPS. Track: {track}, Car: {car}",
                session_type=session_type,
                track=track,
                car=car,
            )

            collection_start = time.monotonic()
            last_progress = collection_start

            # --- Collect FPS ---
            while True:
                if self._stop_requested:
                    break

                connected = False
                try:
                    connected = ir.is_connected
                except Exception:
                    connected = False

                if not connected:
                    break  # iRacing disconnected — end collection

                try:
                    fps_raw = ir["FrameRate"]
                    if fps_raw is not None:
                        fps = float(fps_raw)
                        if fps >= 20.0:  # discard loading/stutter artifacts
                            live_fps_values.append(fps)
                except Exception:
                    pass

                now = time.monotonic()
                if now - last_progress >= self.PROGRESS_INTERVAL:
                    self.emit(
                        "cal_progress",
                        samples=len(live_fps_values),
                        elapsed=round(now - collection_start, 1),
                        msg=f"Collected {len(live_fps_values)} live samples…",
                    )
                    last_progress = now

                time.sleep(self.LIVE_POLL_INTERVAL)

            try:
                ir.shutdown()
            except Exception:
                pass

        # ----------------------------------------------------------------
        # Compute live FPS stats
        # ----------------------------------------------------------------
        if self._stop_requested and len(live_fps_values) < self.MIN_LIVE_SAMPLES:
            self.emit("cal_aborted", msg="Calibration aborted — insufficient live samples collected.")
            raise CalibrationAborted(
                f"Aborted with only {len(live_fps_values)} live samples "
                f"(minimum {self.MIN_LIVE_SAMPLES})."
            )

        if len(live_fps_values) < self.MIN_LIVE_SAMPLES:
            msg = (
                f"Insufficient live FPS samples: {len(live_fps_values)} "
                f"(minimum {self.MIN_LIVE_SAMPLES}). "
                "Drive for at least 60 seconds before stopping."
            )
            self.emit("cal_error", msg=msg)
            raise CalibrationError(msg)

        collection_duration = time.monotonic() - collection_start

        sorted_vals = sorted(live_fps_values)
        n = len(sorted_vals)
        live_fps_p5 = sorted_vals[max(0, int(n * 0.05))]
        live_fps_median = sorted_vals[n // 2]
        live_fps_p95 = sorted_vals[min(n - 1, int(n * 0.95))]

        # ----------------------------------------------------------------
        # Compute correction factor
        # ----------------------------------------------------------------
        if replay_fps_p5 <= 0:
            correction_factor = 1.0
        else:
            correction_factor = round(live_fps_p5 / replay_fps_p5, 6)

        # ----------------------------------------------------------------
        # Write calibration.json
        # ----------------------------------------------------------------
        store = CalibrationStore()
        now_iso = datetime.now(timezone.utc).isoformat()

        cal_data = {
            "schema_version": 1,
            "created": now_iso,
            "replay_baseline": {
                "fps_p5": round(replay_fps_p5, 3),
                "fps_median": round(replay_fps_median, 3),
                "fps_p95": round(replay_fps_p95, 3),
                "sample_count": replay_sample_count,
                "replay_file": self._replay_path.name,
                "duration_seconds": round(bench.duration_seconds, 2),
                "settings_snapshot": current_settings,
            },
            "live_baseline": {
                "fps_p5": round(live_fps_p5, 3),
                "fps_median": round(live_fps_median, 3),
                "fps_p95": round(live_fps_p95, 3),
                "sample_count": n,
                "session_type": session_type,
                "track": track,
                "car": car,
                "collection_duration_seconds": round(collection_duration, 2),
            },
            "correction_factor": correction_factor,
            "valid": True,
            "notes": "",
        }

        store.save(cal_data)

        result = CalibrationResult(
            replay_fps_p5=replay_fps_p5,
            replay_fps_median=replay_fps_median,
            replay_fps_p95=replay_fps_p95,
            replay_sample_count=replay_sample_count,
            live_fps_p5=live_fps_p5,
            live_fps_median=live_fps_median,
            live_fps_p95=live_fps_p95,
            live_sample_count=n,
            session_type=session_type,
            track=track,
            car=car,
            correction_factor=correction_factor,
            collection_duration_seconds=collection_duration,
        )

        self.emit(
            "cal_done",
            correction_factor=correction_factor,
            live_fps_p5=live_fps_p5,
            replay_fps_p5=replay_fps_p5,
            live_sample_count=n,
            track=track,
            car=car,
            msg=(
                f"Calibration complete. correction_factor={correction_factor:.4f} "
                f"(live p5={live_fps_p5:.1f} / replay p5={replay_fps_p5:.1f})"
            ),
        )

        return result
