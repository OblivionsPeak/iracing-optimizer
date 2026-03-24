#!/usr/bin/env python3
"""
iRacing Adaptive Settings Optimizer
Automatically benchmarks your system and finds the best graphics settings
for your target FPS. Runs entirely on your local machine.
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

# Import core modules
from core.config_manager import ConfigManager
from core.process_controller import ProcessController
from core.fps_sampler import FPSSampler
from core.settings import SETTINGS, SETTINGS_BY_KEY
from core.profile_store import ProfileStore

BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder='templates', static_folder='static')

# ── Global state ──────────────────────────────────────────────────────────────
_state = {
    "status": "idle",       # idle | running | done | done_partial | error | aborted
    "result": None,
    "error": None,
    "start_time": None,
}
_event_queue = queue.Queue()
_benchmark_thread = None
_runner = None  # BenchmarkRunner instance (for stop signal)
_state_lock = threading.Lock()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Returns current state: idle/running/done/error/aborted"""
    with _state_lock:
        status = _state["status"]
        error = _state["error"]
    return jsonify({"status": status, "error": error})


@app.route('/api/settings')
def api_get_settings():
    """Returns current rendererDX11.ini tunable settings as JSON"""
    try:
        cm = ConfigManager()
        current = cm.get_all_tunable()
        # Enrich with metadata
        result = []
        for s in SETTINGS:
            key = s["key"]
            result.append({
                "key": key,
                "display_name": s["display_name"],
                "description": s["description"],
                "type": s["type"],
                "values": s["values"],
                "min": s["min"],
                "max": s["max"],
                "impact_weight": s["impact_weight"],
                "current_value": current.get(key),
            })
        renderer_path = str(cm.renderer_ini)
        return jsonify({"settings": result, "renderer_ini": renderer_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/settings', methods=['POST'])
def api_set_settings():
    """Apply specific settings immediately. Body: {key: value, ...}"""
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        return jsonify({"error": "No settings provided"}), 400
    try:
        cm = ConfigManager()
        cm.apply_settings(data)
        return jsonify({"ok": True, "applied": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/replays')
def api_replays():
    """List .rpy replay files available, newest first"""
    try:
        pc = ProcessController()
        files = pc.find_replay_files()
        replay_list = [
            {
                "name": f.name,
                "path": str(f),
                "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                "mtime": f.stat().st_mtime,
            }
            for f in files
        ]
        return jsonify({"replays": replay_list})
    except Exception as e:
        return jsonify({"replays": [], "error": str(e)})


@app.route('/api/profiles')
def api_profiles():
    """List saved profiles"""
    try:
        store = ProfileStore()
        profiles = store.list_all()
        return jsonify({"profiles": profiles})
    except Exception as e:
        return jsonify({"profiles": [], "error": str(e)})


@app.route('/api/profiles', methods=['POST'])
def api_save_profile():
    """Save current result as named profile. Body: {name, scenario}"""
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    scenario = (data.get("scenario") or "practice").strip()

    if not name:
        return jsonify({"error": "Profile name is required"}), 400

    with _state_lock:
        result = _state.get("result")
        status = _state.get("status")

    if status not in ("done", "done_partial") or result is None:
        return jsonify({"error": "No completed benchmark result to save"}), 400

    try:
        store = ProfileStore()
        # result is an OptimizeResult dataclass — pull fields safely
        target_fps = getattr(result, "target_fps", 0)
        best_settings = getattr(result, "best_settings", {})
        fps_sample = getattr(result, "fps_sample", None)

        benchmark_results = {}
        if fps_sample is not None:
            benchmark_results = {
                "fps_median": round(getattr(fps_sample, "median", 0), 2),
                "fps_p5": round(getattr(fps_sample, "p5", 0), 2),
                "fps_p95": round(getattr(fps_sample, "p95", 0), 2),
                "sample_count": getattr(fps_sample, "sample_count", 0),
            }

        path = store.save(
            name=name,
            target_fps=target_fps,
            scenario=scenario,
            settings=best_settings,
            benchmark_results=benchmark_results,
        )
        return jsonify({"ok": True, "saved_to": str(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles/<name>/apply', methods=['POST'])
def api_apply_profile(name):
    """Apply a saved profile's settings to rendererDX11.ini"""
    try:
        store = ProfileStore()
        profile = store.load(name)
        settings = profile.get("settings", {})
        if not settings:
            return jsonify({"error": "Profile has no settings to apply"}), 400
        cm = ConfigManager()
        cm.apply_settings(settings)
        return jsonify({"ok": True, "applied": settings, "profile": name})
    except FileNotFoundError:
        return jsonify({"error": f"Profile '{name}' not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/benchmark/start', methods=['POST'])
def api_benchmark_start():
    """
    Start optimization run.
    Body: {
        "target_fps": 60,
        "replay": "path/to/file.rpy",  # or replay filename
        "mock": false  # true for testing without iRacing
    }
    Starts benchmark in background thread.
    Returns {"status": "started"} or {"error": "already running"}
    """
    global _benchmark_thread

    with _state_lock:
        if _state["status"] == "running":
            return jsonify({"error": "Benchmark already running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    target_fps = int(data.get("target_fps", 60))
    replay_str = (data.get("replay") or "").strip()
    mock = bool(data.get("mock", False))

    # Resolve replay path
    if not replay_str:
        return jsonify({"error": "replay is required"}), 400

    replay_path = Path(replay_str)
    if not replay_path.is_absolute():
        # Try relative to Documents/iRacing/replay/
        replay_dir = Path.home() / "Documents" / "iRacing" / "replay"
        replay_path = replay_dir / replay_str

    if not mock and not replay_path.exists():
        return jsonify({"error": f"Replay file not found: {replay_path}"}), 400

    # Reset state
    with _state_lock:
        _state["status"] = "running"
        _state["result"] = None
        _state["error"] = None
        _state["start_time"] = time.time()

    # Drain stale events from previous run
    while not _event_queue.empty():
        try:
            _event_queue.get_nowait()
        except queue.Empty:
            break

    _benchmark_thread = threading.Thread(
        target=_run_benchmark,
        args=(target_fps, replay_path, mock),
        daemon=True,
        name="benchmark-runner",
    )
    _benchmark_thread.start()

    return jsonify({"status": "started", "target_fps": target_fps, "replay": str(replay_path), "mock": mock})


@app.route('/api/benchmark/stop', methods=['POST'])
def api_benchmark_stop():
    """Abort current benchmark run"""
    global _runner
    with _state_lock:
        status = _state["status"]

    if status != "running":
        return jsonify({"error": "No benchmark running"}), 400

    if _runner is not None:
        _runner.stop()

    with _state_lock:
        _state["status"] = "aborted"

    _event_queue.put({"type": "aborted", "msg": "Benchmark aborted by user", "ts": time.time()})
    return jsonify({"status": "aborted"})


@app.route('/api/benchmark/stream')
def api_benchmark_stream():
    """
    SSE endpoint. Streams events from _event_queue.
    Event format: data: {"type": "log|progress|setting_start|setting_done|done|error", ...}
    Keeps connection alive with ': keepalive' comments every 15s.
    """
    def generate():
        last_keepalive = time.time()
        while True:
            try:
                event = _event_queue.get(timeout=1.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get('type') in ('done', 'error', 'aborted'):
                    break
            except queue.Empty:
                now = time.time()
                if now - last_keepalive > 15:
                    yield ": keepalive\n\n"
                    last_keepalive = now

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/benchmark/result')
def api_benchmark_result():
    """Returns final result after completion"""
    with _state_lock:
        status = _state["status"]
        result = _state.get("result")
        error = _state.get("error")
        start_time = _state.get("start_time")

    if status not in ("done", "done_partial"):
        return jsonify({"status": status, "error": error})

    if result is None:
        return jsonify({"status": status, "error": "No result data available"})

    # Serialise OptimizeResult
    fps_sample = getattr(result, "fps_sample", None)
    original_settings = getattr(result, "original_settings", {})
    best_settings = getattr(result, "best_settings", {})
    iterations = getattr(result, "iterations", 0)
    success = getattr(result, "success", False)
    target_fps = getattr(result, "target_fps", 0)

    duration = round(time.time() - start_time, 1) if start_time else None

    fps_stats = {}
    if fps_sample is not None:
        fps_stats = {
            "median": round(getattr(fps_sample, "median", 0), 1),
            "p5": round(getattr(fps_sample, "p5", 0), 1),
            "p95": round(getattr(fps_sample, "p95", 0), 1),
            "sample_count": getattr(fps_sample, "sample_count", 0),
        }

    # Build settings comparison table
    comparison = []
    all_keys = set(list(original_settings.keys()) + list(best_settings.keys()))
    for key in all_keys:
        meta = SETTINGS_BY_KEY.get(key, {})
        orig = original_settings.get(key)
        opt = best_settings.get(key)
        comparison.append({
            "key": key,
            "display_name": meta.get("display_name", key),
            "original": orig,
            "optimized": opt,
            "changed": orig != opt,
        })
    comparison.sort(key=lambda x: SETTINGS_BY_KEY.get(x["key"], {}).get("impact_weight", 0), reverse=True)

    return jsonify({
        "status": status,
        "success": success,
        "target_fps": target_fps,
        "fps_stats": fps_stats,
        "iterations": iterations,
        "duration_seconds": duration,
        "comparison": comparison,
        "best_settings": best_settings,
    })


# ── Background benchmark thread ───────────────────────────────────────────────

def _run_benchmark(target_fps: int, replay_path: Path, mock: bool):
    """Background thread: runs the full optimization."""
    global _runner

    from core.benchmark_runner import BenchmarkRunner
    from core.profile_store import ProfileStore

    cm = ConfigManager()
    pc = ProcessController()
    sampler = FPSSampler(mock_mode=mock, mock_target_fps=float(target_fps))

    runner = BenchmarkRunner(cm, pc, sampler, replay_path, event_queue=_event_queue)
    _runner = runner

    try:
        cm.backup()
        _event_queue.put({"type": "log", "msg": "Backed up ini files", "ts": time.time()})

        # Try to import the optimizer; if not yet implemented, run a single pass
        try:
            from core.optimizer import BinarySearchOptimizer
            optimizer = BinarySearchOptimizer(target_fps=target_fps)
            result = optimizer.optimize(runner, cm)
        except ImportError:
            _event_queue.put({
                "type": "log",
                "msg": "optimizer module not found — running single baseline pass",
                "ts": time.time(),
            })
            current_settings = cm.get_all_tunable()
            bench_result = runner.run_single(
                settings_dict=current_settings,
                iteration=1,
                total_iterations=1,
            )
            # Wrap in a minimal result object
            class _SimpleResult:
                pass
            result = _SimpleResult()
            result.success = bench_result.fps_sample.passes_target(target_fps)
            result.target_fps = target_fps
            result.fps_sample = bench_result.fps_sample
            result.best_settings = current_settings
            result.original_settings = current_settings
            result.iterations = 1

        with _state_lock:
            _state["status"] = "done" if getattr(result, "success", False) else "done_partial"
            _state["result"] = result

        fps_sample = getattr(result, "fps_sample", None)
        fps_stats = {}
        if fps_sample is not None:
            fps_stats = {
                "median": round(getattr(fps_sample, "median", 0), 1),
                "p5": round(getattr(fps_sample, "p5", 0), 1),
                "p95": round(getattr(fps_sample, "p95", 0), 1),
            }

        _event_queue.put({
            "type": "done",
            "success": getattr(result, "success", False),
            "fps_stats": fps_stats,
            "iterations": getattr(result, "iterations", 1),
            "ts": time.time(),
        })

    except Exception as e:
        with _state_lock:
            if _state["status"] != "aborted":
                _state["status"] = "error"
                _state["error"] = str(e)
        if _state["status"] != "aborted":
            _event_queue.put({"type": "error", "msg": str(e), "ts": time.time()})
    finally:
        _runner = None


# ── Port helper ───────────────────────────────────────────────────────────────

def _find_free_port(default=5002):
    """Find an available port, starting at default."""
    for port in range(default, default + 20):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('127.0.0.1', port))
            s.close()
            return port
        except OSError:
            continue
    return default


if __name__ == '__main__':
    port = _find_free_port(5002)
    url = f"http://127.0.0.1:{port}"
    print(f"\n{'='*52}")
    print(f"  iRacing Adaptive Settings Optimizer")
    print(f"  Open: {url}")
    print(f"{'='*52}\n")
    print("Press Ctrl+C to stop.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
