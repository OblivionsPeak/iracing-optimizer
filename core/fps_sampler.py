import time
import statistics
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class FPSSample:
    fps_values: list[float] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def median(self) -> float:
        if not self.fps_values:
            return 0.0
        return statistics.median(self.fps_values)

    @property
    def p5(self) -> float:
        """5th percentile — the stutter floor. This is the pass/fail metric."""
        if not self.fps_values:
            return 0.0
        sorted_vals = sorted(self.fps_values)
        p5_idx = max(0, int(len(sorted_vals) * 0.05))
        return sorted_vals[p5_idx]

    @property
    def p95(self) -> float:
        if not self.fps_values:
            return 0.0
        sorted_vals = sorted(self.fps_values)
        p95_idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * 0.95))
        return sorted_vals[p95_idx]

    @property
    def mean(self) -> float:
        if not self.fps_values:
            return 0.0
        return statistics.mean(self.fps_values)

    @property
    def sample_count(self) -> int:
        return len(self.fps_values)

    def passes_target(self, target_fps: int, tolerance: int = 5) -> bool:
        """Returns True if p5 >= (target_fps - tolerance)."""
        return self.p5 >= (target_fps - tolerance)


class FPSSampler:
    """
    Samples FPS from iRacing via pyirsdk.
    Falls back to mock mode (Gaussian distribution) when iRacing is not running.
    """

    POLL_INTERVAL = 0.1      # seconds between samples (10Hz)
    WARMUP_SECONDS = 10      # wait this long after connection before sampling
    DISCARD_BELOW_FPS = 20   # discard samples below this (loading/streaming artifacts)

    def __init__(self, mock_mode: bool = False, mock_target_fps: float = 60.0):
        self._mock_mode = mock_mode
        self._mock_target_fps = mock_target_fps
        self._stop_event = threading.Event()

    def wait_for_iracing(
        self,
        timeout: float = 120.0,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """
        Poll until iRacing SDK connects (ir.is_connected) and replay is playing.
        Returns True if connected within timeout, False otherwise.
        Calls progress_cb with status messages while waiting.
        """
        if self._mock_mode:
            if progress_cb:
                progress_cb("Mock mode: skipping iRacing connection wait.")
            return True

        try:
            import irsdk  # type: ignore[import-untyped]
        except ImportError:
            self._mock_mode = True
            if progress_cb:
                progress_cb("pyirsdk not installed — switching to mock mode.")
            return True

        ir = irsdk.IRSDK()
        deadline = time.monotonic() + timeout
        sdk_connected = False

        if progress_cb:
            progress_cb("Waiting for iRacing to start…")

        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                if progress_cb:
                    progress_cb("Stop requested — aborting wait.")
                try:
                    ir.shutdown()
                except Exception:
                    pass
                return False

            if not sdk_connected:
                try:
                    connected = ir.startup()
                except Exception:
                    connected = False

                if connected and ir.is_connected:
                    sdk_connected = True
                    if progress_cb:
                        progress_cb(
                            "iRacing SDK connected. "
                            "Please load your replay in iRacing: "
                            "Garage > Replay > select your file."
                        )
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if progress_cb:
                        progress_cb(
                            f"Waiting for iRacing to open… ({remaining:.0f}s remaining)"
                        )
                    time.sleep(0.5)
                    continue

            # SDK is connected — wait for replay to be playing
            try:
                replay_playing = ir["IsReplayPlaying"]
            except Exception:
                replay_playing = False

            if replay_playing:
                if progress_cb:
                    progress_cb("Replay detected — starting benchmark.")
                try:
                    ir.shutdown()
                except Exception:
                    pass
                return True

            remaining = max(0.0, deadline - time.monotonic())
            if progress_cb:
                progress_cb(
                    f"Waiting for replay to start… ({remaining:.0f}s remaining)"
                )
            time.sleep(0.5)

        if progress_cb:
            progress_cb("Timed out waiting for iRacing.")
        try:
            ir.shutdown()
        except Exception:
            pass
        return False

    def wait_for_stable(
        self,
        stable_seconds: float = 10.0,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Wait for FPS to stabilize after iRacing loads (LOD streaming settles).
        Strategy: wait stable_seconds, discarding any samples below DISCARD_BELOW_FPS.
        """
        if self._mock_mode:
            if progress_cb:
                progress_cb("Mock mode: brief stability pause.")
            time.sleep(0.5)
            return

        if progress_cb:
            progress_cb(
                f"Waiting {stable_seconds:.0f}s for FPS to stabilise (LOD streaming)…"
            )

        ir: Optional[object] = None
        try:
            import irsdk  # type: ignore[import-untyped]
            ir = irsdk.IRSDK()
            ir.startup()  # type: ignore[union-attr]
        except (ImportError, Exception):
            ir = None

        start = time.monotonic()
        while time.monotonic() - start < stable_seconds:
            if self._stop_event.is_set():
                break

            # Opportunistically read FPS but discard — just filling the warmup window
            if ir is not None:
                try:
                    fps = ir["FrameRate"]  # type: ignore[index]
                    if progress_cb and fps is not None:
                        elapsed = time.monotonic() - start
                        remaining = max(0.0, stable_seconds - elapsed)
                        progress_cb(
                            f"Stabilising… {remaining:.0f}s remaining, "
                            f"current FPS: {float(fps):.1f}"
                        )
                except Exception:
                    pass

            time.sleep(self.POLL_INTERVAL)

        if ir is not None:
            try:
                ir.shutdown()  # type: ignore[union-attr]
            except Exception:
                pass

        if progress_cb:
            progress_cb("Stability wait complete.")

    def sample(
        self,
        duration_seconds: float = 30.0,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> FPSSample:
        """
        Sample FrameRate for duration_seconds.
        Calls progress_cb every 5 seconds with: "Sampling... Xs remaining, current FPS: X.X"
        Discards samples below DISCARD_BELOW_FPS.
        Returns FPSSample with all collected values.
        """
        fps_values: list[float] = []
        start_time = time.monotonic()
        last_progress_report = start_time
        PROGRESS_INTERVAL = 5.0

        ir: Optional[object] = None
        if not self._mock_mode:
            try:
                import irsdk  # type: ignore[import-untyped]
                ir = irsdk.IRSDK()
                ir.startup()  # type: ignore[union-attr]
            except (ImportError, Exception):
                self._mock_mode = True
                ir = None

        if progress_cb:
            progress_cb(
                f"Starting {duration_seconds:.0f}s FPS sample"
                f"{'  (mock mode)' if self._mock_mode else ''}…"
            )

        while True:
            now = time.monotonic()
            elapsed = now - start_time

            if elapsed >= duration_seconds or self._stop_event.is_set():
                break

            fps = self._get_current_fps(ir)

            if fps is not None and fps >= self.DISCARD_BELOW_FPS:
                fps_values.append(fps)

            # Progress report every 5 seconds
            if now - last_progress_report >= PROGRESS_INTERVAL:
                remaining = max(0.0, duration_seconds - elapsed)
                current_fps = fps if fps is not None else 0.0
                if progress_cb:
                    progress_cb(
                        f"Sampling... {remaining:.0f}s remaining, "
                        f"current FPS: {current_fps:.1f}"
                    )
                last_progress_report = now

            time.sleep(self.POLL_INTERVAL)

        actual_duration = time.monotonic() - start_time

        if ir is not None:
            try:
                ir.shutdown()  # type: ignore[union-attr]
            except Exception:
                pass

        if len(fps_values) < 5:
            raise ValueError(
                "Insufficient FPS samples — iRacing may have crashed"
            )

        if progress_cb:
            progress_cb(
                f"Sampling complete. Collected {len(fps_values)} samples "
                f"over {actual_duration:.1f}s."
            )

        return FPSSample(fps_values=fps_values, duration_seconds=actual_duration)

    def stop(self) -> None:
        """Signal any active wait/sample to stop early."""
        self._stop_event.set()

    def reset(self) -> None:
        """Reset stop signal for reuse."""
        self._stop_event.clear()

    def _mock_fps(self) -> float:
        """Generate mock FPS value: Gaussian(target, sigma=8) clamped to [15, 300]."""
        import random
        val = random.gauss(self._mock_target_fps, 8)
        return max(15.0, min(300.0, val))

    def _get_current_fps(self, ir: object) -> Optional[float]:
        """Get current FPS from irsdk or mock. Returns None if not available."""
        if self._mock_mode:
            return self._mock_fps()

        if ir is None:
            return None

        try:
            fps = ir["FrameRate"]  # type: ignore[index]
            if fps is None:
                return None
            return float(fps)
        except Exception:
            return None
