"""
config_manager.py — Parse and write iRacing INI files without mangling
tab-aligned comments. Standard configparser is NOT used because it destroys
the tab-comment formatting that iRacing relies on.
"""

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from .settings import SETTINGS, SETTINGS_BY_KEY

# Default iRacing document paths
_IRACING_DOCS = Path.home() / "Documents" / "iRacing"
_DEFAULT_RENDERER = _IRACING_DOCS / "rendererDX11.ini"
_DEFAULT_APP = _IRACING_DOCS / "app.ini"


class ConfigManager:
    """
    Reads and writes iRacing INI files while preserving all whitespace,
    tab alignment, and inline comments exactly as iRacing wrote them.
    """

    def __init__(
        self,
        renderer_ini: Optional[Path] = None,
        app_ini: Optional[Path] = None,
    ) -> None:
        self.renderer_ini: Path = renderer_ini or _DEFAULT_RENDERER
        self.app_ini: Path = app_ini or _DEFAULT_APP
        # Lazy-loaded caches: {file_name: parsed_dict}
        self._cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def detect_paths(self) -> tuple[Path, Path]:
        """
        Return (renderer_ini_path, app_ini_path).
        Raises FileNotFoundError if either file is missing.
        """
        for p in (self.renderer_ini, self.app_ini):
            if not p.exists():
                raise FileNotFoundError(
                    f"iRacing INI file not found: {p}\n"
                    "Make sure iRacing has been run at least once."
                )
        return self.renderer_ini, self.app_ini

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------

    def backup(self) -> tuple[Path, Path]:
        """
        Copy both INI files to timestamped .bak files.
        Returns (renderer_bak_path, app_bak_path).
        """
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        renderer_bak = self.renderer_ini.with_suffix(f".bak_{stamp}")
        app_bak = self.app_ini.with_suffix(f".bak_{stamp}")
        shutil.copy2(self.renderer_ini, renderer_bak)
        shutil.copy2(self.app_ini, app_bak)
        return renderer_bak, app_bak

    def restore_backup(self) -> None:
        """
        Restore the most recent backup of both INI files (determined by
        the highest-sorting timestamp suffix).
        """
        for ini_path in (self.renderer_ini, self.app_ini):
            stem = ini_path.stem  # e.g. "rendererDX11"
            parent = ini_path.parent
            pattern = f"{stem}.bak_*"
            candidates = sorted(parent.glob(pattern))
            if not candidates:
                raise FileNotFoundError(
                    f"No backup found for {ini_path.name} in {parent}"
                )
            latest_bak = candidates[-1]  # lexicographic sort == chronological
            shutil.copy2(latest_bak, ini_path)
        # Invalidate cache after restore
        self._cache.clear()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse(self, path: Path) -> dict:
        """
        Parse an iRacing INI file into a nested dict:

            {
              section_name: {
                key: {
                  "value": str,       # raw string value (before comments)
                  "raw_line": str,    # complete original line (no newline)
                  "line_num": int,    # 1-based line number
                },
                "__meta__": [         # non-key lines (comments, blanks, headers)
                  {"raw_line": str, "line_num": int},
                  ...
                ],
              },
              ...
            }

        Section headers themselves are recorded in the *previous* section's
        __meta__ list so that write-back can reconstruct the file verbatim.
        The initial section for lines before any header is "__preamble__".
        """
        parsed: dict[str, dict] = {}
        current_section = "__preamble__"
        parsed[current_section] = {"__meta__": []}

        _kv_re = re.compile(r"^(\s*)([^=\s][^=]*?)\s*=\s*([^\t;]*?)\s*(\t.*|;.*)?$")

        with path.open(encoding="utf-8", errors="replace") as fh:
            for line_num, raw in enumerate(fh, start=1):
                line = raw.rstrip("\n\r")

                # Section header
                section_match = re.match(r"^\[(.+)\]\s*$", line)
                if section_match:
                    current_section = section_match.group(1)
                    if current_section not in parsed:
                        parsed[current_section] = {"__meta__": []}
                    parsed[current_section]["__meta__"].append(
                        {"raw_line": line, "line_num": line_num}
                    )
                    continue

                # Key=Value line
                kv_match = _kv_re.match(line)
                if kv_match and "=" in line:
                    key = kv_match.group(2).strip()
                    value = kv_match.group(3).strip()
                    if current_section not in parsed:
                        parsed[current_section] = {"__meta__": []}
                    parsed[current_section][key] = {
                        "value": value,
                        "raw_line": line,
                        "line_num": line_num,
                    }
                    continue

                # Everything else (blank lines, pure comments)
                parsed[current_section]["__meta__"].append(
                    {"raw_line": line, "line_num": line_num}
                )

        return parsed

    def _get_parsed(self, file: str) -> dict:
        """Return (and cache) the parsed dict for a given filename."""
        if file not in self._cache:
            path = self.renderer_ini if "renderer" in file.lower() else self.app_ini
            self._cache[file] = self.parse(path)
        return self._cache[file]

    # ------------------------------------------------------------------
    # Value accessors
    # ------------------------------------------------------------------

    def get_value(self, key: str, file: str = "rendererDX11.ini") -> Optional[str]:
        """Return the current string value for *key*, or None if not found."""
        parsed = self._get_parsed(file)
        for section, entries in parsed.items():
            if section == "__preamble__":
                continue
            if key in entries:
                return entries[key]["value"]
        return None

    def get_all_tunable(self) -> dict[str, int]:
        """
        Return {key: int_value} for every setting defined in SETTINGS.
        Missing keys are omitted from the result.
        """
        result: dict[str, int] = {}
        for setting in SETTINGS:
            raw = self.get_value(setting["key"], file=setting["file"])
            if raw is not None:
                try:
                    result[setting["key"]] = int(raw)
                except ValueError:
                    pass  # skip unparseable values
        return result

    # ------------------------------------------------------------------
    # Value mutators
    # ------------------------------------------------------------------

    def set_value(self, key: str, value, file: str = "rendererDX11.ini") -> bool:
        """
        Update *key* in the INI file on disk, preserving all whitespace,
        tab alignment, and inline comments exactly.

        Strategy: read every line, find the line that matches ``key=…``,
        and replace only the value portion using a regex that captures:
          group 1 — leading whitespace + key + equals sign (+ any space)
          group 2 — the old value (stripped)
          group 3 — everything after the value: tabs + comment

        Returns True if the key was found and the file was updated.
        """
        path = self.renderer_ini if "renderer" in file.lower() else self.app_ini

        # Pattern: optional leading space, literal key, optional spaces, =,
        # optional spaces, then the value, then the rest.
        pattern = re.compile(
            r"^(\s*" + re.escape(key) + r"\s*=\s*)([^\t;]*?)((\t[^\n]*)|(;[^\n]*))?$"
        )

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        updated = False

        for i, line in enumerate(lines):
            m = pattern.match(line.rstrip("\n\r"))
            if m:
                prefix = m.group(1)      # "Key=", with original spacing
                suffix = m.group(3) or ""  # "\t\t; comment" or ""
                eol = "\n" if line.endswith("\n") else ""

                # Reconstruct: keep the original value width by padding
                # to match iRacing's column alignment (column 48 by default).
                new_value_str = str(value)
                # Rebuild the line preserving the tab-comment block unchanged.
                lines[i] = f"{prefix}{new_value_str}{suffix}{eol}"
                updated = True
                break

        if updated:
            path.write_text("".join(lines), encoding="utf-8")
            # Invalidate cache for this file
            self._cache.pop(file, None)

        return updated

    def apply_settings(self, settings_dict: dict) -> None:
        """
        Apply multiple settings at once.

        Args:
            settings_dict: {key: value} mapping of settings to apply.
        """
        for key, value in settings_dict.items():
            meta = SETTINGS_BY_KEY.get(key)
            file = meta["file"] if meta else "rendererDX11.ini"
            self.set_value(key, value, file=file)
