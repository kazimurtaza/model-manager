"""
Model Manager - Flask backend for Hugging Face model downloads.

See SPEC.md for the target behavior. Downloads run in subprocesses (downloader.py)
so they can be truly cancelled; progress is reported by a parent-side thread that
scans the destination directory.
"""
import os
import sys
import json
import uuid
import shutil
import queue
import time
import logging
import tempfile
import threading
import subprocess
import atexit
from datetime import datetime
from pathlib import Path
from typing import Dict
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, Response, stream_with_context
from huggingface_hub import HfApi

from models import ModelScanner

app = Flask(__name__)

# Configuration
MODELS_PATH = os.environ.get("MODELS_PATH", "/models")
DATA_PATH = os.environ.get("DATA_PATH", "/app/data")
QUEUE_FILE = Path(DATA_PATH) / "download_queue.json"
MAX_DOWNLOAD_WORKERS = int(os.environ.get("MAX_DOWNLOAD_WORKERS", "3"))
QUEUE_SAVE_INTERVAL = int(os.environ.get("QUEUE_SAVE_INTERVAL", "30"))
COMPLETED_AUTO_CLEAR_SECONDS = int(os.environ.get("COMPLETED_AUTO_CLEAR_SECONDS", "10"))
DOWNLOADER = str(Path(__file__).parent / "downloader.py")

# Download status constants (single source of truth)
STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_DOWNLOADING)

# Initialize scanner
scanner = ModelScanner(MODELS_PATH)

# In-memory download tracking
downloads: Dict[str, Dict] = {}
download_events: Dict[str, queue.Queue] = {}
download_lock = threading.Lock()

# Thread pool for download workers (concurrency cap; the queue is unlimited)
download_executor = ThreadPoolExecutor(
    max_workers=MAX_DOWNLOAD_WORKERS,
    thread_name_prefix="download_worker",
)

# Periodic queue save (queue_save_lock also serializes save_queue writes)
queue_save_timer = None
queue_save_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def repo_to_dirname(model_id: str) -> str:
    """Map a HF repo id (org/name) to an on-disk folder name (org__name).

    Namespacing by org prevents two repos that share a name from colliding.
    """
    return model_id.replace("/", "__")


def target_dir_for(model_id: str, model_type: str) -> Path:
    return Path(MODELS_PATH) / model_type / repo_to_dirname(model_id)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def save_queue():
    """Persist the queue with an atomic, race-free write.

    Completed entries are never persisted (they auto-clear). The snapshot is built
    under download_lock; the file write is serialized under queue_save_lock using a
    unique temp file, so concurrent callers cannot corrupt each other's writes.
    """
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

        with download_lock:
            queue_snapshot = {}
            for dl_id, dl_info in downloads.items():
                if dl_info.get("status") == STATUS_COMPLETED:
                    continue  # auto-cleared; never persist
                dl_copy = {}
                for k, v in dl_info.items():
                    if k in ("proc", "future", "stop_progress"):
                        continue
                    try:
                        json.dumps(v, default=str)  # serializable?
                        dl_copy[k] = v
                    except (TypeError, ValueError):
                        dl_copy[k] = str(v) if v is not None else None
                queue_snapshot[dl_id] = dl_copy

        with queue_save_lock:
            fd, tmp_path = tempfile.mkstemp(dir=str(QUEUE_FILE.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(queue_snapshot, f, indent=2, default=str)
                os.replace(tmp_path, QUEUE_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        logging.info(f"Saved queue with {len(queue_snapshot)} downloads")
    except Exception as e:
        logging.error(f"Failed to save queue: {e}")


def periodic_queue_save():
    """Periodically save the queue state, then re-arm the timer."""
    global queue_save_timer
    try:
        save_queue()
    except Exception as e:
        logging.error(f"Failed to save queue: {e}")
    with queue_save_lock:
        queue_save_timer = threading.Timer(QUEUE_SAVE_INTERVAL, periodic_queue_save)
        queue_save_timer.daemon = True
        queue_save_timer.start()


def start_periodic_save():
    global queue_save_timer
    with queue_save_lock:
        if queue_save_timer is None or not queue_save_timer.is_alive():
            queue_save_timer = threading.Timer(QUEUE_SAVE_INTERVAL, periodic_queue_save)
            queue_save_timer.daemon = True
            queue_save_timer.start()


def load_queue():
    """Restore queue state on startup.

    Completed/legacy entries are dropped; failed entries are kept (for manual retry);
    any entry that was queued/downloading (orphaned by the restart) is marked failed.
    """
    global downloads
    if not QUEUE_FILE.exists():
        return
    try:
        with open(QUEUE_FILE, "r") as f:
            downloads = json.load(f)
        for dl_id, dl_info in list(downloads.items()):
            status = dl_info.get("status")
            if status == STATUS_COMPLETED:
                downloads.pop(dl_id, None)
                download_events.pop(dl_id, None)
            elif status in ACTIVE_STATUSES:
                downloads[dl_id]["status"] = STATUS_FAILED
                downloads[dl_id]["error"] = "Download interrupted by restart"
                downloads[dl_id]["end_time"] = datetime.now().isoformat()
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Failed to load queue: {e}")
        downloads = {}


# --------------------------------------------------------------------------- #
# Download lifecycle helpers
# --------------------------------------------------------------------------- #
def delete_partial(dl_info: Dict):
    """Remove partial files for a non-completed download.

    Single-file download: remove just that file. Whole-repo snapshot: remove the
    entire destination folder (including its .cache). Safe to call when nothing exists.
    """
    target_dir = dl_info.get("target_dir")
    if not target_dir:
        return
    target = Path(target_dir)
    filename = dl_info.get("filename")
    try:
        if filename:
            file_path = target / filename
            if file_path.exists():
                file_path.unlink()
        elif target.exists() and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    except Exception as e:
        logging.error(f"Failed to delete partial files: {e}")


def terminate_download(dl_info: Dict):
    """Terminate the subprocess for a download if it is still running."""
    proc = dl_info.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception as e:
            logging.error(f"Failed to terminate download: {e}")


def schedule_auto_clear(download_id: str):
    """Remove a completed download record after a short delay."""
    def clear():
        with download_lock:
            if downloads.get(download_id, {}).get("status") == STATUS_COMPLETED:
                downloads.pop(download_id, None)
                download_events.pop(download_id, None)
        save_queue()

    timer = threading.Timer(COMPLETED_AUTO_CLEAR_SECONDS, clear)
    timer.daemon = True
    timer.start()


def _emit(download_id: str, event: dict):
    """Publish an SSE event without ever blocking.

    Queues are bounded (maxsize=100); a blocking put() with no SSE consumer would fill the
    queue and hang the worker/request thread (and leak an executor slot). Drop on Full.
    """
    q = download_events.get(download_id)
    if q is None:
        return
    try:
        q.put_nowait(event)
    except queue.Full:
        pass  # no consumer draining; drop rather than block


def download_worker(download_id: str, model_id: str, model_type: str,
                    filename: str = None, expected_size: int = 0):
    """Run a download in a subprocess and report progress via a dir-scan thread."""
    target_dir = target_dir_for(model_id, model_type)
    progress_thread = None
    try:
        _emit(download_id, {
            "type": "started", "model_id": model_id, "model_type": model_type,
            "filename": filename, "target_dir": str(target_dir),
            "expected_size": expected_size,
        })

        with download_lock:
            if download_id not in downloads:
                return  # removed before we started
            if downloads[download_id].get("status") == STATUS_CANCELLED:
                return  # cancelled while queued; don't start the download
            downloads[download_id]["stop_progress"] = threading.Event()
            downloads[download_id]["status"] = STATUS_DOWNLOADING
            downloads[download_id]["expected_size"] = expected_size

        def send_progress_updates():
            while True:
                with download_lock:
                    if download_id not in downloads:
                        break
                    status = downloads[download_id].get("status")
                    stop_event = downloads[download_id].get("stop_progress")
                if status != STATUS_DOWNLOADING or (stop_event and stop_event.is_set()):
                    break
                try:
                    current_size = 0
                    if target_dir.exists():
                        for item in target_dir.rglob("*"):
                            if item.is_file():
                                current_size += item.stat().st_size
                    percent = 0
                    if expected_size > 0:
                        percent = min(int(current_size / expected_size * 100), 99)
                    _emit(download_id, {
                        "type": "progress", "percent": percent,
                        "downloaded": current_size, "total": expected_size,
                    })
                except Exception:
                    pass  # never let progress tracking break the download
                time.sleep(3)

        progress_thread = threading.Thread(target=send_progress_updates, daemon=True)
        progress_thread.start()

        # Spawn the downloader subprocess (HF_TOKEN inherited via env)
        target_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, DOWNLOADER,
               "--repo", model_id, "--type", model_type, "--dest", str(target_dir)]
        if filename:
            cmd += ["--file", filename]
        proc = subprocess.Popen(
            cmd, env=dict(os.environ),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        with download_lock:
            if download_id in downloads:
                downloads[download_id]["proc"] = proc

        # Block until the subprocess finishes (or is terminated by cancel/clear/remove)
        _, stderr = proc.communicate()
        exit_code = proc.returncode

        # Signal the progress thread to stop FIRST, then join (fixes the ~1h hang)
        with download_lock:
            removed = download_id not in downloads
            if not removed:
                stop_event = downloads[download_id].get("stop_progress")
                if stop_event:
                    stop_event.set()
        if removed:
            # Removed mid-flight (delete/clear): clean up partials OUTSIDE the lock
            delete_partial({"target_dir": str(target_dir), "filename": filename})
            return
        if progress_thread:
            progress_thread.join(timeout=5)

        # Determine outcome
        with download_lock:
            if download_id not in downloads:
                return
            current_status = downloads[download_id].get("status")
            if current_status == STATUS_CANCELLED:
                pass  # cancelled while running; partials already cleaned up
            elif exit_code == 0:
                downloads[download_id]["status"] = STATUS_COMPLETED
                downloads[download_id]["end_time"] = datetime.now().isoformat()
                _emit(download_id, {"type": "completed"})
                schedule_auto_clear(download_id)
            else:
                err_lines = (stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
                err_msg = err_lines[-1] if err_lines else f"Downloader exited with code {exit_code}"
                downloads[download_id]["status"] = STATUS_FAILED
                downloads[download_id]["error"] = err_msg
                downloads[download_id]["end_time"] = datetime.now().isoformat()
                _emit(download_id, {"type": "error", "message": err_msg})

    except Exception as e:
        with download_lock:
            if download_id in downloads:
                downloads[download_id]["status"] = STATUS_FAILED
                downloads[download_id]["error"] = str(e)
                downloads[download_id]["end_time"] = datetime.now().isoformat()
                stop_event = downloads[download_id].get("stop_progress")
                if stop_event:
                    stop_event.set()
        _emit(download_id, {"type": "error", "message": str(e)})
    finally:
        with download_lock:
            if download_id in downloads:
                downloads[download_id].pop("proc", None)
        if progress_thread and progress_thread.is_alive():
            # Ensure the progress thread is stopped even on the error path
            with download_lock:
                sp = downloads.get(download_id, {}).get("stop_progress")
                if sp:
                    sp.set()
            progress_thread.join(timeout=5)
        try:
            save_queue()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


@app.route("/download", methods=["POST"])
def start_download():
    """Queue a model download. The queue is unlimited; up to MAX_DOWNLOAD_WORKERS
    run concurrently, the rest wait."""
    data = request.get_json()
    if not data or "model_id" not in data:
        return jsonify({"error": "model_id is required"}), 400

    model_id = data["model_id"]
    model_type = data.get("model_type", "gguf")
    filename = data.get("filename")
    try:
        expected_size = int(data.get("expected_size", 0) or 0)
    except (TypeError, ValueError):
        expected_size = 0

    if model_type not in ("gguf", "safetensors"):
        return jsonify({"error": "model_type must be 'gguf' or 'safetensors'"}), 400
    if ".." in model_id or model_id.startswith("/"):
        return jsonify({"error": "Invalid model_id"}), 400
    if filename and (".." in filename or "/" in filename):
        return jsonify({"error": "Invalid filename"}), 400

    target_dir = target_dir_for(model_id, model_type)
    download_id = str(uuid.uuid4())

    with download_lock:
        downloads[download_id] = {
            "id": download_id,
            "model_id": model_id,
            "model_type": model_type,
            "status": STATUS_QUEUED,
            "created_at": datetime.now().isoformat(),
            "expected_size": expected_size,
            "target_dir": str(target_dir),
        }
        if filename:
            downloads[download_id]["filename"] = filename
        download_events[download_id] = queue.Queue(maxsize=100)

    save_queue()

    future = download_executor.submit(
        download_worker, download_id, model_id, model_type, filename, expected_size
    )
    with download_lock:
        if download_id in downloads:
            downloads[download_id]["future"] = future

    return jsonify({
        "download_id": download_id, "status": STATUS_QUEUED,
        "model_id": model_id, "model_type": model_type, "filename": filename,
    }), 202


@app.route("/models", methods=["GET"])
def list_models():
    return jsonify({"models": scanner.scan_all()})


@app.route("/downloads", methods=["GET"])
def list_downloads():
    with download_lock:
        downloads_list = []
        for dl_info in downloads.values():
            dl_copy = dl_info.copy()
            for key in ("proc", "future", "stop_progress"):
                dl_copy.pop(key, None)
            downloads_list.append(dl_copy)
    return jsonify({"downloads": downloads_list})


@app.route("/models/<model_id>", methods=["DELETE"])
def delete_model(model_id: str):
    """Delete an installed model and any download rows referencing it."""
    if not scanner.validate_model_id(model_id):
        return jsonify({"error": "Invalid model_id"}), 400

    success = scanner.delete_model(model_id)
    if not success:
        return jsonify({"error": "Model not found"}), 404

    # Remove related download rows (match by type + namespaced folder name)
    if model_id.startswith("gguf_"):
        mtype, folder = "gguf", model_id[5:]
    else:
        mtype, folder = "safetensors", model_id[12:]
    with download_lock:
        related = [
            dl_id for dl_id, d in downloads.items()
            if d.get("model_type") == mtype and repo_to_dirname(d.get("model_id", "")) == folder
        ]
        snapshots = {dl_id: dict(downloads[dl_id]) for dl_id in related}
        for dl_id in related:
            downloads.pop(dl_id, None)
            download_events.pop(dl_id, None)
    for info in snapshots.values():
        terminate_download(info)
    save_queue()

    return jsonify({"status": "deleted", "model_id": model_id})


@app.route("/downloads/<download_id>/cancel", methods=["POST"])
def cancel_download(download_id: str):
    """Abort a running download and delete its partial files."""
    with download_lock:
        if download_id not in downloads:
            return jsonify({"error": "Download not found"}), 404
        dl_info = dict(downloads[download_id])
        downloads[download_id]["status"] = STATUS_CANCELLED
        downloads[download_id]["end_time"] = datetime.now().isoformat()

    terminate_download(dl_info)  # outside the lock: may wait on the subprocess
    delete_partial(dl_info)

    if download_id in download_events:
        _emit(download_id, {"type": "cancelled"})
    save_queue()
    return jsonify({"status": STATUS_CANCELLED})


@app.route("/downloads/<download_id>/retry", methods=["POST"])
def retry_download(download_id: str):
    """Retry a failed/cancelled download, replacing the entry in place."""
    with download_lock:
        if download_id not in downloads:
            return jsonify({"error": "Download not found"}), 404
        dl_info = downloads[download_id]
        if dl_info["status"] not in (STATUS_FAILED, STATUS_CANCELLED):
            return jsonify({"error": "Can only retry failed or cancelled downloads"}), 400

        model_id = dl_info["model_id"]
        model_type = dl_info["model_type"]
        filename = dl_info.get("filename")
        expected_size = dl_info.get("expected_size", 0)
        target_dir = dl_info.get("target_dir") or str(target_dir_for(model_id, model_type))

        # Replace in place: reset the same record
        dl_info.update({
            "status": STATUS_QUEUED,
            "created_at": datetime.now().isoformat(),
            "error": None,
            "end_time": None,
            "target_dir": target_dir,
            "expected_size": expected_size,
        })
        dl_info.pop("proc", None)
        download_events[download_id] = queue.Queue(maxsize=100)

    save_queue()

    future = download_executor.submit(
        download_worker, download_id, model_id, model_type, filename, expected_size
    )
    with download_lock:
        if download_id in downloads:
            downloads[download_id]["future"] = future

    return jsonify({
        "download_id": download_id, "status": STATUS_QUEUED,
        "model_id": model_id, "model_type": model_type, "filename": filename,
    }), 202


@app.route("/downloads/<download_id>", methods=["DELETE"])
def delete_download(download_id: str):
    """Remove a download entry. Incomplete downloads also have partial files deleted;
    completed files are kept (they are a real installed model)."""
    with download_lock:
        if download_id not in downloads:
            return jsonify({"error": "Download not found"}), 404
        dl_info = dict(downloads[download_id])
        incomplete = dl_info.get("status") != STATUS_COMPLETED
        downloads.pop(download_id, None)
        download_events.pop(download_id, None)

    if incomplete:
        terminate_download(dl_info)
        delete_partial(dl_info)
    save_queue()

    return jsonify({
        "status": "deleted",
        "model_id": dl_info.get("model_id"),
        "filename": dl_info.get("filename"),
    })


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    """Clear ALL Hugging Face transfer caches under the models directory."""
    cleared_dirs = []
    for cache_dir in Path(MODELS_PATH).rglob("*/.cache/huggingface"):
        try:
            shutil.rmtree(cache_dir)
            cleared_dirs.append(str(cache_dir))
        except Exception:
            pass
    return jsonify({
        "status": "cleared", "cleared_dirs": cleared_dirs, "count": len(cleared_dirs),
    })


@app.route("/downloads/clear", methods=["POST"])
def clear_downloads():
    """Stop all in-flight downloads, clear the list, delete partial files (completed
    models are kept), and remove the queue file."""
    with download_lock:
        snapshots = {dl_id: dict(info) for dl_id, info in downloads.items()}
        downloads.clear()
        download_events.clear()

    for info in snapshots.values():
        if info.get("status") != STATUS_COMPLETED:
            terminate_download(info)
            delete_partial(info)

    if QUEUE_FILE.exists():
        try:
            QUEUE_FILE.unlink()
        except Exception:
            pass

    return jsonify({"status": "cleared", "cleared_count": len(snapshots)})


@app.route("/scan", methods=["POST"])
def scan_repo():
    """List ALL files in a HF repo with their real sizes."""
    data = request.get_json()
    if not data or "model_id" not in data:
        return jsonify({"error": "model_id is required"}), 400

    model_id = data["model_id"]
    if ".." in model_id or model_id.startswith("/"):
        return jsonify({"error": "Invalid model_id"}), 400

    try:
        api = HfApi()
        info = api.repo_info(
            repo_id=model_id, repo_type="model", files_metadata=True,
            token=os.environ.get("HF_TOKEN"),
        )
        files = []
        for sibling in info.siblings:
            size_bytes = getattr(sibling, "size", None) or 0
            files.append({
                "filename": sibling.rfilename,
                "size_bytes": size_bytes,
                "size_gb": round(size_bytes / (1024 ** 3), 2),
                "model_id": model_id,
            })
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": f"Failed to scan repo: {str(e)}"}), 500


@app.route("/events", methods=["GET"])
def events_stream():
    """Server-Sent Events stream for download progress."""
    download_id = request.args.get("download_id")
    if not download_id or download_id not in downloads:
        return "Invalid download_id", 400

    def event_stream():
        try:
            dl_info = downloads.get(download_id)
            if dl_info is None:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Download not found'})}\n\n"
                return
            yield f"data: {json.dumps({'type': 'status', 'status': dl_info['status']})}\n\n"

            if download_id not in download_events:
                # No event queue: orphaned by restart
                yield f"data: {json.dumps({'type': 'error', 'message': 'Download interrupted - please retry'})}\n\n"
                return

            while True:
                try:
                    if download_id not in download_events:
                        break  # entry deleted (remove/clear) between iterations
                    event = download_events[download_id].get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event["type"] in ("completed", "error", "cancelled"):
                        break
                except queue.Empty:
                    yield ": keepalive\n\n"
                    if download_id not in downloads:
                        break
                except KeyError:
                    break  # download_events[download_id] vanished mid-iteration
        except GeneratorExit:
            logging.info(f"SSE client disconnected for download {download_id}")
            raise

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.route("/", methods=["GET"])
def index():
    with open(Path(__file__).parent / "static" / "index.html") as f:
        return f.read()


def cleanup_executor():
    download_executor.shutdown(wait=False)


atexit.register(cleanup_executor)


# Load queue on startup
load_queue()
start_periodic_save()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
