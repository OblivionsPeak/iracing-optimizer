"""
calibration_store.py — Persist and retrieve replay/live baseline calibration data.

calibration.json is written atomically to the project root.
Schema version: 1
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .settings import SETTINGS

CALIBRATION_PATH = Path(__file__).parent.parent / "calibration.json"

# Keys with impact_weight >= 5 (used for stale-warning comparison)
_HIGH_IMPACT_KEYS: frozenset[str] = frozenset(
    s["key"] for s in SETTINGS if s["impact_weight"] >= 5
)


class CalibrationStore:
    """Read/write calibration.json for live baseline calibration."""

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> dict | None:
        """
        Return parsed calibration.json, or None if the file is missing or
        malformed. Never raises.
        """
        try:
            if not CALIBRATION_PATH.exists():
                return None
            text = CALIBRATION_PATH.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, data: dict) -> Path:
        """
        Atomically write *data* to calibration.json.
        Sets data["updated"] to the current UTC ISO timestamp.
        Returns the path written to.
        """
        data["updated"] = datetime.now(timezone.utc).isoformat()

        serialised = json.dumps(data, indent=2, ensure_ascii=False)

        # Atomic write: write to a temp file in the same directory, then rename.
        parent = CALIBRATION_PATH.parent
        fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp", prefix=".calibration_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialised)
            os.replace(tmp_path, CALIBRATION_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return CALIBRATION_PATH

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_correction_factor(self) -> float:
        """
        Return correction_factor from a valid calibration file.
        Returns 1.0 if the file is absent, malformed, or valid=False.
        """
        data = self.load()
        if data is None:
            return 1.0
        if not data.get("valid", False):
            return 1.0
        try:
            return float(data["correction_factor"])
        except (KeyError, TypeError, ValueError):
            return 1.0

    def get_stale_warning(self, current_settings: dict) -> bool:
        """
        Return True if calibration exists, is valid, but its
        settings_snapshot differs from *current_settings* on any key
        whose impact_weight >= 5.

        Returns False if there is no calibration, the calibration is not
        valid, or the snapshot is missing.
        """
        data = self.load()
        if data is None:
            return False
        if not data.get("valid", False):
            return False

        replay_baseline = data.get("replay_baseline")
        if not isinstance(replay_baseline, dict):
            return False

        snapshot = replay_baseline.get("settings_snapshot")
        if not isinstance(snapshot, dict):
            return False

        for key in _HIGH_IMPACT_KEYS:
            snap_val = snapshot.get(key)
            curr_val = current_settings.get(key)
            if snap_val is not None and snap_val != curr_val:
                return True

        return False

    def clear(self) -> bool:
        """
        Delete calibration.json.
        Returns True if the file was deleted, False if it did not exist.
        """
        try:
            CALIBRATION_PATH.unlink()
            return True
        except FileNotFoundError:
            return False
        except Exception:
            raise
