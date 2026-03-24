"""
optimizer.py — Binary search optimizer for iRacing graphics settings.

Tunes each setting independently, in impact_weight order (highest first),
to find the maximum quality level that still meets the target FPS.
"""

import time
from dataclasses import dataclass, field
from queue import Queue
from typing import Optional

from .benchmark_runner import BenchmarkResult, BenchmarkRunner
from .config_manager import ConfigManager
from .settings import SETTINGS, SETTINGS_BY_KEY


@dataclass
class OptimizationResult:
    final_settings: dict                      # {key: value} — recommended settings
    original_settings: dict                   # {key: value} — what was there before
    benchmark_results: list[BenchmarkResult]  # one per iteration
    target_fps: int
    achieved_fps_median: float
    achieved_fps_p5: float
    total_duration_seconds: float
    iterations_run: int
    success: bool                             # True if p5 >= target - 5


class BinarySearchOptimizer:
    """
    Tunes iRacing graphics settings to maximise quality at a target FPS.

    Algorithm per setting (in impact_weight order, highest first):
    1. Try maximum quality value.
    2. If passes target → keep max, move to next setting.
    3. If fails → binary search between min and max to find highest passing value.
    4. Commit that value and move to next setting.

    A setting "passes" when fps_sample.passes_target(target_fps, tolerance=5) is True.
    The p5 (5th percentile) FPS is used as the metric, not median.

    Settings with depends_on are skipped if their prerequisite is not met in
    current_settings.
    """

    def __init__(self, target_fps: int, tolerance: int = 5):
        self.target_fps = target_fps
        self.tolerance = tolerance

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def estimate_iterations(self) -> int:
        """Estimate total benchmark runs needed (for progress display). ~2-3 per setting."""
        return (
            len([s for s in SETTINGS if not s.get("depends_on")]) * 2
            + len([s for s in SETTINGS if s.get("depends_on")]) * 2
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_ordered_settings(self, current_settings: dict) -> list[dict]:
        """
        Return settings in impact_weight order (highest first).
        Skip settings whose depends_on prerequisite is not met.
        """
        ordered = sorted(SETTINGS, key=lambda s: s["impact_weight"], reverse=True)
        result = []
        for s in ordered:
            dep = s.get("depends_on")
            if dep:
                dep_key, dep_val = list(dep.items())[0]
                if current_settings.get(dep_key) != dep_val:
                    continue  # prerequisite not met — skip
            result.append(s)
        return result

    def _get_values_list(self, setting: dict) -> list:
        """
        Return the list of valid values for a setting, ordered min→max.
        For int type:  range(min, max+1)
        For enum/bool: setting['values'] (already ordered low→high)
        """
        if setting["type"] == "int":
            return list(range(setting["min"], setting["max"] + 1))
        # enum or bool — values list is already ordered
        return list(setting["values"])

    def _run_and_record(self,
                        runner: BenchmarkRunner,
                        settings_dict: dict,
                        iteration_counter: list,
                        total_estimated: int,
                        all_results: list) -> BenchmarkResult:
        """
        Run a single benchmark, increment the counter, record the result,
        emit a progress event, and return the result.
        """
        iteration_counter[0] += 1
        result = runner.run_single(
            settings_dict=settings_dict,
            iteration=iteration_counter[0],
            total_iterations=total_estimated,
        )
        all_results.append(result)

        pct = int(iteration_counter[0] / total_estimated * 100)
        passed = result.fps_sample.passes_target(self.target_fps, self.tolerance)

        # Determine which key/value we just tested (first item in the dict)
        tested_key = next(iter(settings_dict), "")
        tested_val = settings_dict.get(tested_key, "")

        runner.emit(
            "progress",
            pct=pct,
            fps=result.fps_sample.p5,
            setting=tested_key,
            value=tested_val,
            passed=passed,
        )
        return result

    def _binary_search_setting(self,
                                setting: dict,
                                current_settings: dict,
                                runner: BenchmarkRunner,
                                iteration_counter: list,
                                total_estimated: int,
                                all_results: list) -> tuple[int, list[BenchmarkResult]]:
        """
        Binary search for the highest value in values_list that passes target FPS.
        Returns (best_value, list_of_BenchmarkResults_from_this_setting).

        Steps:
        1. Try max value  → if passes, return max immediately (1 run).
        2. Try min value  → if fails,  return min (1 run, log warning).
        3. Binary search between min_idx and max_idx:
           - Try mid value.
           - If passes: record as best, search upper half (try higher).
           - If fails:  search lower half.
           - Stop when range collapses to a single value.
        """
        key = setting["key"]
        values = self._get_values_list(setting)
        local_results: list[BenchmarkResult] = []

        def run(value):
            test_settings = {**current_settings, key: value}
            r = self._run_and_record(
                runner, test_settings, iteration_counter,
                total_estimated, all_results
            )
            local_results.append(r)
            return r

        # --- Step 1: try max ---
        max_val = values[-1]
        runner.log(f"  [{key}] Testing MAX value: {max_val}")
        result_max = run(max_val)
        if result_max.fps_sample.passes_target(self.target_fps, self.tolerance):
            runner.log(f"  [{key}] MAX value {max_val} passes — keeping max quality.")
            return max_val, local_results

        # --- Step 2: try min ---
        min_val = values[0]
        runner.log(f"  [{key}] MAX failed (p5={result_max.fps_sample.p5:.1f}). Testing MIN: {min_val}")
        result_min = run(min_val)
        if not result_min.fps_sample.passes_target(self.target_fps, self.tolerance):
            runner.log(
                f"  [{key}] WARNING: Even MIN value {min_val} fails "
                f"(p5={result_min.fps_sample.p5:.1f}). Keeping min."
            )
            return min_val, local_results

        # --- Step 3: binary search between min and max ---
        lo = 0
        hi = len(values) - 1
        best_idx = lo  # start conservatively at min (we know it passes)

        while lo < hi:
            if runner._stop_event.is_set():
                break

            mid = (lo + hi + 1) // 2  # bias toward upper half
            mid_val = values[mid]
            runner.log(f"  [{key}] Binary search: lo={values[lo]} hi={values[hi]} trying mid={mid_val}")
            result_mid = run(mid_val)

            if result_mid.fps_sample.passes_target(self.target_fps, self.tolerance):
                best_idx = mid
                lo = mid  # this value passes; can we go higher?
            else:
                hi = mid - 1  # this value fails; go lower

        best_value = values[best_idx]
        runner.log(f"  [{key}] Binary search complete — best value: {best_value}")
        return best_value, local_results

    # ------------------------------------------------------------------
    # Main optimization entry point
    # ------------------------------------------------------------------

    def optimize(self,
                 runner: BenchmarkRunner,
                 config_manager: ConfigManager) -> OptimizationResult:
        """
        Run the full optimization.

        1. Backup ini files.
        2. Record original settings.
        3. For each setting (in impact_weight order):
           a. Check depends_on.
           b. Binary search for best value.
           c. Apply best value to ini (so subsequent settings build on it).
           d. Emit SSE progress events.
        4. Return OptimizationResult.

        Emits these event types via runner.emit():
        - {"type": "setting_start", "key": "...", "display_name": "...",
           "iteration": N, "total": N}
        - {"type": "progress", "pct": 0-100, "fps": float, "setting": "...",
           "value": int, "passed": bool}
        - {"type": "setting_done", "key": "...", "best_value": int, "fps_p5": float}
        - {"type": "done", "result": {...}}
        """
        opt_start = time.monotonic()
        all_results: list[BenchmarkResult] = []
        iteration_counter = [0]

        # --- Backup ini files ---
        runner.log("Creating INI file backups…")
        try:
            renderer_bak, app_bak = config_manager.backup()
            runner.log(f"Backups created: {renderer_bak.name}, {app_bak.name}")
        except Exception as exc:
            runner.log(f"WARNING: Could not create backups: {exc}")

        # --- Record original settings ---
        original_settings = config_manager.get_all_tunable()
        runner.log(f"Recorded {len(original_settings)} original setting(s).")

        # Working copy of settings — updated as we commit each best value
        current_settings: dict = dict(original_settings)
        final_settings: dict = dict(original_settings)

        total_estimated = self.estimate_iterations()
        runner.log(f"Estimated iterations: {total_estimated}")

        # --- Iterate over settings in impact_weight order ---
        ordered = self._get_ordered_settings(current_settings)
        runner.log(f"Optimizing {len(ordered)} setting(s).")

        for setting in ordered:
            # Check stop signal
            if runner._stop_event.is_set():
                runner.log("Stop signal received — aborting optimization.")
                break

            key = setting["key"]
            display_name = setting["display_name"]

            runner.emit(
                "setting_start",
                key=key,
                display_name=display_name,
                iteration=iteration_counter[0],
                total=total_estimated,
            )
            runner.log(f"Optimizing: {display_name} ({key})")

            try:
                best_value, _ = self._binary_search_setting(
                    setting=setting,
                    current_settings=current_settings,
                    runner=runner,
                    iteration_counter=iteration_counter,
                    total_estimated=total_estimated,
                    all_results=all_results,
                )
            except RuntimeError as exc:
                # Stop was signalled mid-run
                runner.log(f"Run aborted during {key}: {exc}")
                break

            # Commit the best value so subsequent settings are benchmarked
            # at the correct baseline
            current_settings[key] = best_value
            final_settings[key] = best_value
            config_manager.set_value(key, best_value, file=setting["file"])

            # Report p5 from the last result that used this best value
            last_p5 = 0.0
            if all_results:
                last_p5 = all_results[-1].fps_sample.p5

            runner.emit(
                "setting_done",
                key=key,
                best_value=best_value,
                fps_p5=last_p5,
            )
            runner.log(
                f"  → {display_name} set to {best_value} "
                f"(p5 FPS in last sample: {last_p5:.1f})"
            )

        # --- Build final OptimizationResult ---
        total_duration = time.monotonic() - opt_start

        # Use the last benchmark result for the achieved FPS summary
        achieved_median = 0.0
        achieved_p5 = 0.0
        if all_results:
            last = all_results[-1].fps_sample
            achieved_median = last.median
            achieved_p5 = last.p5

        success = achieved_p5 >= (self.target_fps - self.tolerance)

        result = OptimizationResult(
            final_settings=final_settings,
            original_settings=original_settings,
            benchmark_results=all_results,
            target_fps=self.target_fps,
            achieved_fps_median=achieved_median,
            achieved_fps_p5=achieved_p5,
            total_duration_seconds=total_duration,
            iterations_run=iteration_counter[0],
            success=success,
        )

        result_dict = {
            "final_settings": result.final_settings,
            "original_settings": result.original_settings,
            "target_fps": result.target_fps,
            "achieved_fps_median": result.achieved_fps_median,
            "achieved_fps_p5": result.achieved_fps_p5,
            "total_duration_seconds": result.total_duration_seconds,
            "iterations_run": result.iterations_run,
            "success": result.success,
        }
        runner.emit("done", result=result_dict)

        runner.log(
            f"Optimization complete in {total_duration:.1f}s — "
            f"{iteration_counter[0]} iteration(s). "
            f"Success: {success} (p5={achieved_p5:.1f} fps, "
            f"target={self.target_fps} fps)"
        )

        return result
