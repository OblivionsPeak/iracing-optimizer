"""
profile_store.py — Save, load, list, and delete named optimizer profiles.
Each profile is stored as a JSON file under the profiles/ directory.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

PROFILES_DIR = Path(__file__).parent.parent / "profiles"


class ProfileStore:
    """Persist and retrieve named iRacing optimizer profiles as JSON files."""

    def __init__(self, profiles_dir: Path = PROFILES_DIR) -> None:
        self.dir = profiles_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        """Return the JSON file path for a given profile name."""
        # Sanitise the name so it is safe as a filename
        safe = "".join(c if (c.isalnum() or c in " _-") else "_" for c in name)
        safe = safe.strip().replace(" ", "_")
        return self.dir / f"{safe}.json"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save(
        self,
        name: str,
        target_fps: int,
        scenario: str,
        settings: dict,
        benchmark_results: dict,
    ) -> Path:
        """
        Persist a named profile to disk as JSON.

        Args:
            name:              Human-readable profile name, e.g. "Race Day 60fps".
            target_fps:        The FPS target that was used during the benchmark run.
            scenario:          Scenario label, e.g. "race", "practice", "qualify".
            settings:          {key: value} dict of all applied setting values.
            benchmark_results: Result metrics dict (fps_median, fps_p5, fps_p95, …).

        Returns:
            Path to the saved JSON file.
        """
        profile: dict = {
            "name": name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "target_fps": target_fps,
            "scenario": scenario,
            "settings": settings,
            "benchmark_results": benchmark_results,
        }
        path = self._path(name)
        path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        return path

    def load(self, name: str) -> dict:
        """
        Load a profile by name.

        Raises:
            FileNotFoundError: if no profile with that name exists.
        """
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(
                f"Profile '{name}' not found. Expected file: {path}"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def list_all(self) -> list[dict]:
        """
        Return summary dicts for every saved profile, sorted newest first.

        Each summary contains:
            name, created, target_fps, scenario, fps_median
        """
        summaries: list[dict] = []
        for json_file in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # skip corrupted files silently

            results = data.get("benchmark_results", {})
            summaries.append(
                {
                    "name": data.get("name", json_file.stem),
                    "created": data.get("created", ""),
                    "target_fps": data.get("target_fps"),
                    "scenario": data.get("scenario", ""),
                    "fps_median": results.get("fps_median"),
                }
            )

        # Sort newest-first by the ISO timestamp string (lexicographic sort works)
        summaries.sort(key=lambda s: s["created"], reverse=True)
        return summaries

    def delete(self, name: str) -> bool:
        """
        Delete a profile by name.

        Returns:
            True if the file existed and was deleted, False if it was not found.
        """
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False
