"""
Live Session Optimizer
Coaches the user through session-by-session setting changes.
No replay file needed — works with real iRacing practice/race sessions.
"""

import random
import threading
import time
from dataclasses import dataclass, field
from queue import Queue
from typing import Optional

from .config_manager import ConfigManager
from .settings import SETTINGS, SETTINGS_BY_KEY


VALID_LIVE_TYPES = {
    'practice', 'race', 'qualify', 'open practice', 'open qualify',
    'lone qualify', 'offline testing', 'time trial'
}


def _get_session_type(ir) -> str:
    """Safely get session type — ir['SessionType'] returns None; use SessionInfo instead."""
    try:
        val = ir["SessionType"]
        if val:
            return str(val).lower().strip()
    except Exception:
        pass
    try:
        session_num = ir["SessionNum"] or 0
        return str(ir["SessionInfo"]["Sessions"][session_num]["SessionType"]).lower().strip()
    except Exception:
        return ""


def _is_live_session(ir) -> bool:
    try:
        return ir.is_connected and not ir["IsReplayPlaying"] and _get_session_type(ir) in VALID_LIVE_TYPES
    except Exception:
        return False
SESSION_CHECK_INTERVAL = 2.0
POLL_INTERVAL = 0.1
PROGRESS_INTERVAL = 10.0
SESSION_WAIT_TIMEOUT = 900.0   # 15 min
MIN_SAMPLES = 300              # 30s at 10Hz


@dataclass
class SessionResult:
    session_number: int
    fps_p5: float
    fps_median: float
    fps_p95: float
    sample_count: int
    duration_seconds: float
    settings_applied: dict
    session_type: str
    track: str
    car: str


@dataclass
class SettingRecommendation:
    key: str
    display_name: str
    current_value: int
    recommended_value: int
    expected_fps_gain: float
    description: str


@dataclass
class LiveOptimizationState:
    sessions: list = field(default_factory=list)
    pending_recommendation: Optional[SettingRecommendation] = None
    applied_settings: dict = field(default_factory=dict)
    target_fps: int = 60
    status: str = "idle"
    target_met: bool = False


class LiveSessionOptimizer:
    """
    Coaches user through session-by-session setting optimization.
    Each iteration: wait for live session → collect FPS → recommend one change → wait for decision.
    """

    def __init__(self,
                 config_manager: ConfigManager,
                 target_fps: int,
                 event_queue: Queue,
                 mock_mode: bool = False,
                 mock_fps: float = 55.0):
        self.cm = config_manager
        self.target_fps = target_fps
        self.event_queue = event_queue
        self.mock_mode = mock_mode
        self.mock_fps = mock_fps
        self.state = LiveOptimizationState(target_fps=target_fps)
        self._stop_event = threading.Event()
        self._continue_event = threading.Event()
        self._user_decision: Optional[str] = None  # 'accept' | 'skip'

    def emit(self, event_type: str, **kwargs):
        self.event_queue.put({"type": event_type, "ts": time.time(), **kwargs})

    def run(self) -> LiveOptimizationState:
        """Main loop. Runs in a background thread."""
        self.state.status = "waiting"
        self.state.applied_settings = self.cm.get_all_tunable()
        session_num = 0

        while not self._stop_event.is_set():
            session_num += 1

            # Wait for user to enter a live session
            self.emit("live_waiting",
                      msg=f"Session {session_num}: Waiting for you to enter a live iRacing session...",
                      session_num=session_num)

            session_info = self._wait_for_live_session()
            if session_info is None:
                break

            # Collect FPS during the session
            self.state.status = "collecting"
            self.emit("live_session_start",
                      session_num=session_num,
                      session_type=session_info.get("session_type", ""),
                      track=session_info.get("track", ""),
                      car=session_info.get("car", ""))

            fps_data, meta = self._collect_session_fps()

            if not fps_data or len(fps_data) < MIN_SAMPLES:
                self.emit("live_session_skipped",
                          msg=f"Session {session_num}: Too brief — need at least {MIN_SAMPLES // 10}s of data. Ready for next session.",
                          sample_count=len(fps_data) if fps_data else 0)
                continue

            # Compute stats
            sv = sorted(fps_data)
            n = len(sv)
            fps_p5 = sv[max(0, int(n * 0.05))]
            fps_median = sv[n // 2]
            fps_p95 = sv[min(n - 1, int(n * 0.95))]

            result = SessionResult(
                session_number=session_num,
                fps_p5=round(fps_p5, 1),
                fps_median=round(fps_median, 1),
                fps_p95=round(fps_p95, 1),
                sample_count=n,
                duration_seconds=round(meta.get("duration", 0), 1),
                settings_applied=dict(self.state.applied_settings),
                session_type=session_info.get("session_type", ""),
                track=session_info.get("track", ""),
                car=session_info.get("car", ""),
            )
            self.state.sessions.append(result)

            self.emit("live_session_result",
                      session_num=session_num,
                      fps_p5=fps_p5,
                      fps_median=fps_median,
                      fps_p95=fps_p95,
                      sample_count=n,
                      target_fps=self.target_fps,
                      target_met=fps_p5 >= self.target_fps - 5)

            # Check if target met
            if fps_p5 >= self.target_fps - 5:
                self.state.target_met = True
                self.state.status = "done"
                self.emit("live_done",
                          msg=f"Target reached! Your p5 FPS ({fps_p5:.1f}) meets your {self.target_fps}fps target.",
                          sessions_run=session_num,
                          final_settings=self.state.applied_settings)
                return self.state

            # Generate recommendation
            rec = self._recommend_next_change(fps_p5)
            if rec is None:
                self.state.status = "done"
                self.emit("live_done",
                          msg="All settings optimized. Further gains require hardware upgrades.",
                          sessions_run=session_num,
                          final_settings=self.state.applied_settings)
                return self.state

            self.state.pending_recommendation = rec
            self.state.status = "recommending"
            self.emit("live_recommendation",
                      key=rec.key,
                      display_name=rec.display_name,
                      current_value=rec.current_value,
                      recommended_value=rec.recommended_value,
                      expected_fps_gain=rec.expected_fps_gain,
                      description=rec.description,
                      fps_gap=round(self.target_fps - fps_p5, 1))

            # Wait for user decision
            self._continue_event.clear()
            self._user_decision = None
            self.emit("live_awaiting_decision",
                      msg="Apply the recommended change before your next session?")

            while not self._stop_event.is_set():
                if self._continue_event.wait(timeout=1.0):
                    break

            if self._stop_event.is_set():
                break

            decision = self._user_decision
            if decision == "accept":
                s = SETTINGS_BY_KEY[rec.key]
                self.cm.set_value(rec.key, rec.recommended_value, file=s["file"])
                self.state.applied_settings[rec.key] = rec.recommended_value
                self.emit("live_setting_applied",
                          key=rec.key,
                          display_name=rec.display_name,
                          value=rec.recommended_value,
                          msg=f"Applied: {rec.display_name} → {rec.recommended_value}. Start your next session.")
            elif decision == "skip":
                self.emit("live_setting_skipped",
                          key=rec.key,
                          msg=f"Skipped {rec.display_name}. Will try next best option next session.")

            self.state.status = "waiting"

        self.state.status = "aborted"
        self.emit("live_aborted",
                  msg="Live optimization stopped.",
                  sessions_run=len(self.state.sessions),
                  final_settings=self.state.applied_settings)
        return self.state

    def accept_recommendation(self):
        self._user_decision = "accept"
        self._continue_event.set()

    def skip_recommendation(self):
        self._user_decision = "skip"
        self._continue_event.set()

    def stop(self):
        self._stop_event.set()
        self._continue_event.set()

    def reset(self):
        self._stop_event.clear()
        self._continue_event.clear()
        self._user_decision = None

    # ── Private helpers ──────────────────────────────────────────────────────

    def _wait_for_live_session(self) -> Optional[dict]:
        """Wait for a non-replay iRacing session. Returns session info dict or None."""
        if self.mock_mode:
            time.sleep(1.0)
            return {"session_type": "practice", "track": "Spa-Francorchamps", "car": "Ferrari 296 GT3"}

        try:
            import irsdk
        except ImportError:
            self.emit("live_error", msg="pyirsdk not installed. Enable mock mode for testing.")
            return None

        ir = irsdk.IRSDK()
        deadline = time.time() + SESSION_WAIT_TIMEOUT
        try:
            while time.time() < deadline and not self._stop_event.is_set():
                ir.startup()
                if _is_live_session(ir):
                    return {
                        "session_type": _get_session_type(ir),
                        "track": ir.get("TrackDisplayName") or ir.get("TrackName") or "Unknown",
                        "car": ir.get("PlayerCarTeamName") or "Unknown",
                    }
                time.sleep(SESSION_CHECK_INTERVAL)
        finally:
            try:
                ir.shutdown()
            except Exception:
                pass
        return None

    def _collect_session_fps(self) -> tuple:
        """Collect FPS until session ends. Returns (samples, meta)."""
        if self.mock_mode:
            samples = [max(20.0, random.gauss(self.mock_fps, 8)) for _ in range(600)]
            time.sleep(2.0)
            return samples, {"duration": 60.0}

        try:
            import irsdk
        except ImportError:
            return [], {}

        ir = irsdk.IRSDK()
        samples = []
        start_time = time.time()
        last_progress = start_time

        try:
            ir.startup()
            while not self._stop_event.is_set():
                if not ir.is_connected:
                    break
                if not _is_live_session(ir):
                    break

                fps = ir["FrameRate"]
                if fps and fps > 20:
                    samples.append(float(fps))

                now = time.time()
                if now - last_progress >= PROGRESS_INTERVAL:
                    self.emit("live_collecting",
                              elapsed=round(now - start_time),
                              sample_count=len(samples),
                              current_fps=round(fps or 0, 1))
                    last_progress = now

                time.sleep(POLL_INTERVAL)
        finally:
            try:
                ir.shutdown()
            except Exception:
                pass

        return samples, {"duration": time.time() - start_time}

    def _recommend_next_change(self, current_fps_p5: float) -> Optional[SettingRecommendation]:
        """Find highest-impact setting that can still be reduced."""
        fps_gap = self.target_fps - current_fps_p5
        current_settings = self.cm.get_all_tunable()
        ordered = sorted(SETTINGS, key=lambda s: s["impact_weight"], reverse=True)
        total_impact = sum(s["impact_weight"] for s in SETTINGS) or 1

        label_maps = {
            "AntiAliasMethod": {0: "None", 1: "MSAA", 2: "FXAA", 3: "SMAA"},
            "DynamicShadowRes": {0: "Off", 1: "512px", 2: "1024px", 3: "2048px", 4: "4096px"},
            "ShaderQuality": {0: "Low", 1: "Medium", 2: "High", 3: "Ultra"},
        }

        for s in ordered:
            key = s["key"]
            current_val = current_settings.get(key)
            if current_val is None:
                continue

            if s["type"] == "int":
                min_val, values = s["min"], list(range(s["min"], s["max"] + 1))
            else:
                values = s.get("values", [])
                min_val = values[0] if values else None

            if min_val is None or current_val <= min_val:
                continue

            if s["type"] == "int":
                recommended = max(min_val, current_val - 1)
            else:
                idx = values.index(current_val) if current_val in values else len(values) - 1
                recommended = values[max(0, idx - 1)]

            if recommended == current_val:
                continue

            labels = label_maps.get(key, {})
            cur_label = labels.get(current_val, str(current_val))
            rec_label = labels.get(recommended, str(recommended))
            estimated_gain = round((fps_gap / total_impact) * s["impact_weight"] * 2, 1)

            return SettingRecommendation(
                key=key,
                display_name=s["display_name"],
                current_value=current_val,
                recommended_value=recommended,
                expected_fps_gain=estimated_gain,
                description=(
                    f"Reduce {s['display_name']} from {cur_label} to {rec_label}. "
                    f"Estimated +{estimated_gain} fps toward your {self.target_fps}fps target."
                ),
            )

        return None
