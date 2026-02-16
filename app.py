"""
Model Manager - Flask backend for Hugging Face model downloads.
"""
import os
import json
import uuid
import threading
import queue
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from concurrent.futures import ThreadPoolExecutor
import atexit

from flask import Flask, request, jsonify, Response, stream_with_context
from huggingface_hub import HfApi, hf_hub_download, snapshot_download

from models import ModelScanner

app = Flask(__name__)

# Configuration
MODELS_PATH = os.environ.get("MODELS_PATH", "/models")
DATA_PATH = os.environ.get("DATA_PATH", "/app/data")
QUEUE_FILE = Path(DATA_PATH) / "download_queue.json"
MAX_DOWNLOAD_WORKERS = int(os.environ.get("MAX_DOWNLOAD_WORKERS", "3"))

# Initialize scanner
scanner = ModelScanner(MODELS_PATH)

# In-memory download tracking
downloads: Dict[str, Dict] = {}
download_events: Dict[str, queue.Queue] = {}
download_lock = threading.Lock()

# Thread pool for download workers
download_executor = ThreadPoolExecutor(
    max_workers=MAX_DOWNLOAD_WORKERS,
    thread_name_prefix="download_worker"
)

# Simple in-memory rate limiter
download_requests = {}
RATE_LIMIT = 10  # requests per minute

# Periodic queue save
queue_save_timer = None
queue_save_lock = threading.Lock()
QUEUE_SAVE_INTERVAL = int(os.environ.get("QUEUE_SAVE_INTERVAL", "30"))


def save_queue():
    """Save download queue to file with atomic write."""
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

        with download_lock:
            # Filter out non-serializable objects
            queue_snapshot = {}
            for dl_id, dl_info in downloads.items():
                dl_copy = {}
                for k, v in dl_info.items():
                    if k not in ["future", "subscribers", "stop_progress"]:
                        try:
                            json.dumps(v, default=str)  # Test if serializable
                            dl_copy[k] = v
                        except (TypeError, ValueError):
                            dl_copy[k] = str(v) if v is not None else None
                queue_snapshot[dl_id] = dl_copy

        temp_file = QUEUE_FILE.with_suffix('.tmp')
        with open(temp_file, "w") as f:
            json.dump(queue_snapshot, f, indent=2, default=str)

        temp_file.replace(QUEUE_FILE)
        logging.info(f"Saved queue with {len(queue_snapshot)} downloads")
    except Exception as e:
        logging.error(f"Failed to save queue: {e}")


def periodic_queue_save():
    """Periodically save the queue state."""
    global queue_save_timer
    logging.info("Periodic queue save triggered")
    try:
        save_queue()
    except Exception as e:
        logging.error(f"Failed to save queue: {e}")

    with queue_save_lock:
        queue_save_timer = threading.Timer(QUEUE_SAVE_INTERVAL, periodic_queue_save)
        queue_save_timer.daemon = True
        queue_save_timer.start()


def start_periodic_save():
    """Start the periodic save timer."""
    global queue_save_timer
    with queue_save_lock:
        if queue_save_timer is None or not queue_save_timer.is_alive():
            queue_save_timer = threading.Timer(QUEUE_SAVE_INTERVAL, periodic_queue_save)
            queue_save_timer.daemon = True
            queue_save_timer.start()


def load_queue():
    """Load download queue from file."""
    global downloads
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r") as f:
                downloads = json.load(f)
                # Clean up old downloads from previous runs
                for dl_id, dl_info in list(downloads.items()):
                    status = dl_info.get("status")
                    # Remove completed/failed/deleted downloads
                    if status in ["completed", "failed", "deleted"]:
                        del downloads[dl_id]
                        if dl_id in download_events:
                            del download_events[dl_id]
                    # Mark queued/downloading as failed (orphaned by restart)
                    elif status in ["queued", "downloading"]:
                        downloads[dl_id]["status"] = "failed"
                        downloads[dl_id]["error"] = "Download interrupted by restart"
                        downloads[dl_id]["end_time"] = datetime.now().isoformat()
        except (json.JSONDecodeError, IOError) as e:
            downloads = {}


def download_worker(download_id: str, model_id: str, model_type: str, filename: Optional[str] = None, expected_size: int = 0):
    """Worker function to download model in background thread."""
    try:
        # Determine target directory
        if model_type == "gguf":
            target_dir = Path(MODELS_PATH) / "gguf" / model_id.split("/")[-1]
        elif model_type == "safetensors":
            target_dir = Path(MODELS_PATH) / "safetensors" / model_id.split("/")[-1]
        else:
            raise ValueError(f"Unknown model type: {model_type}")

        # Send started event
        if download_id in download_events:
            download_events[download_id].put({
                "type": "started",
                "model_id": model_id,
                "model_type": model_type,
                "filename": filename,
                "target_dir": str(target_dir),
                "expected_size": expected_size
            })

        with download_lock:
            downloads[download_id]["stop_progress"] = threading.Event()
            downloads[download_id]["status"] = "downloading"
            downloads[download_id]["expected_size"] = expected_size

        # Progress tracking function
        def send_progress_updates():
            """Send periodic progress updates while downloading."""
            while True:
                # Thread-safe status check WITH lock
                with download_lock:
                    if download_id not in downloads:
                        break
                    status = downloads[download_id].get("status")
                    stop_event = downloads[download_id].get("stop_progress")

                if status != "downloading" or (stop_event and stop_event.is_set()):
                    break
                try:
                    # Calculate current size
                    current_size = 0
                    if target_dir.exists():
                        for item in target_dir.rglob("*"):
                            if item.is_file():
                                current_size += item.stat().st_size

                    # Calculate progress percentage
                    percent = 0
                    if expected_size > 0:
                        percent = min(int(current_size / expected_size * 100), 99)

                    if download_id in download_events:
                        download_events[download_id].put({
                            "type": "progress",
                            "percent": percent,
                            "downloaded": current_size,
                            "total": expected_size
                        })
                except Exception:
                    pass  # Don't let progress tracking errors break the download

                time.sleep(3)  # Update every 3 seconds

        progress_thread = threading.Thread(target=send_progress_updates, daemon=True)
        progress_thread.start()

        # Download model or specific file
        if filename:
            # Download single file
            target_file = target_dir / filename
            target_dir.mkdir(parents=True, exist_ok=True)
            hf_hub_download(
                repo_id=model_id,
                filename=filename,
                local_dir=str(target_dir),
                local_dir_use_symlinks=False
            )
            # Use the filename in the record
            downloads[download_id]["filename"] = filename
        else:
            # Download entire repo
            snapshot_download(
                repo_id=model_id,
                local_dir=str(target_dir),
                local_dir_use_symlinks=False
            )

        # Wait for download thread to finish
        progress_thread.join(timeout=3600)  # Max 1 hour

        # Update status to completed FIRST
        with download_lock:
            downloads[download_id]["status"] = "completed"
            downloads[download_id]["end_time"] = datetime.now().isoformat()
            if "stop_progress" in downloads[download_id]:
                downloads[download_id]["stop_progress"].set()

        # Then wait for progress thread (short timeout)
        progress_thread.join(timeout=5)

        if download_id in download_events:
            download_events[download_id].put({"type": "completed"})

    except Exception as e:
        # Send failed event
        with download_lock:
            downloads[download_id]["stop_progress"] = threading.Event()
            downloads[download_id]["stop_progress"].set()
            downloads[download_id]["status"] = "failed"
            downloads[download_id]["error"] = str(e)
            downloads[download_id]["end_time"] = datetime.now().isoformat()

        if download_id in download_events:
            download_events[download_id].put({
                "type": "error",
                "message": str(e)
            })

    finally:
        try:
            save_queue()
        except Exception:
            pass  # Log error but don't fail


@app.before_request
def check_rate_limit():
    """Simple rate limiting for download requests."""
    if request.path == "/download" and request.method == "POST":
        client_ip = request.remote_addr
        now = datetime.now()

        if client_ip in download_requests:
            requests = download_requests[client_ip]
            recent = [r for r in requests if r > now - timedelta(minutes=1)]
            if len(recent) >= RATE_LIMIT:
                return jsonify({"error": "Rate limit exceeded"}), 429
            download_requests[client_ip] = recent + [now]
        else:
            download_requests[client_ip] = [now]


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


@app.route("/download", methods=["POST"])
def start_download():
    """Start a model download from Hugging Face."""
    data = request.get_json()

    if not data or "model_id" not in data:
        return jsonify({"error": "model_id is required"}), 400

    model_id = data["model_id"]
    model_type = data.get("model_type", "gguf")
    filename = data.get("filename")  # Optional: download specific file
    expected_size = data.get("expected_size", 0)  # Expected file size in bytes

    # Validate model type
    if model_type not in ["gguf", "safetensors"]:
        return jsonify({"error": "model_type must be 'gguf' or 'safetensors'"}), 400

    # Basic validation of model_id to prevent directory traversal
    if ".." in model_id or "/" == model_id[0]:
        return jsonify({"error": "Invalid model_id"}), 400

    # Validate filename if provided
    if filename:
        if ".." in filename or "/" in filename:
            return jsonify({"error": "Invalid filename"}), 400

    # Check capacity BEFORE creating download
    with download_lock:
        active_count = sum(1 for dl in downloads.values() if dl.get("status") in ["queued", "downloading"])
        if active_count >= MAX_DOWNLOAD_WORKERS:
            return jsonify({"error": f"Too many downloads queued (max {MAX_DOWNLOAD_WORKERS})"}), 429

    # Create download record
    download_id = str(uuid.uuid4())

    with download_lock:
        downloads[download_id] = {
            "id": download_id,
            "model_id": model_id,
            "model_type": model_type,
            "status": "queued",
            "created_at": datetime.now().isoformat(),
            "total_bytes": 0,
            "downloaded_bytes": 0
        }
        if filename:
            downloads[download_id]["filename"] = filename
        # Create event queue for SSE
        download_events[download_id] = queue.Queue(maxsize=100)

    save_queue()

    # Submit to thread pool instead of creating thread
    future = download_executor.submit(
        download_worker,
        download_id, model_id, model_type, filename, expected_size
    )

    with download_lock:
        downloads[download_id]["future"] = future

    return jsonify({
        "download_id": download_id,
        "status": "queued",
        "model_id": model_id,
        "model_type": model_type,
        "filename": filename
    }), 202


@app.route("/models", methods=["GET"])
def list_models():
    """List all installed models."""
    models = scanner.scan_all()
    return jsonify({"models": models})


@app.route("/downloads", methods=["GET"])
def list_downloads():
    """List all active downloads."""
    with download_lock:
        # Create a copy without non-serializable objects
        downloads_list = []
        for dl_id, dl_info in downloads.items():
            dl_copy = dl_info.copy()
            # Remove non-serializable objects
            dl_copy.pop("future", None)
            dl_copy.pop("subscribers", None)
            dl_copy.pop("stop_progress", None)
            downloads_list.append(dl_copy)
        return jsonify({"downloads": downloads_list})


@app.route("/models/<model_id>", methods=["DELETE"])
def delete_model(model_id: str):
    """Delete a model."""
    # Validate model_id
    if not scanner.validate_model_id(model_id):
        return jsonify({"error": "Invalid model_id"}), 400

    # Delete the model
    success = scanner.delete_model(model_id)

    if success:
        return jsonify({"status": "deleted", "model_id": model_id})
    else:
        return jsonify({"error": "Model not found"}), 404


@app.route("/downloads/<download_id>/cancel", methods=["POST"])
def cancel_download(download_id: str):
    """Cancel an active download."""
    if download_id not in downloads:
        return jsonify({"error": "Download not found"}), 404

    # Mark as cancelled
    with download_lock:
        downloads[download_id]["status"] = "cancelled"
        downloads[download_id]["end_time"] = datetime.now().isoformat()

    # Clean up event queue
    if download_id in download_events:
        download_events[download_id].put({"type": "cancelled"})

    return jsonify({"status": "cancelled"})


@app.route("/downloads/<download_id>/retry", methods=["POST"])
def retry_download(download_id: str):
    """Retry a failed download."""
    if download_id not in downloads:
        return jsonify({"error": "Download not found"}), 404

    with download_lock:
        dl_info = downloads[download_id]
        if dl_info["status"] not in ["failed", "cancelled"]:
            return jsonify({"error": "Can only retry failed downloads"}), 400

        model_id = dl_info["model_id"]
        model_type = dl_info["model_type"]
        filename = dl_info.get("filename")
        expected_size = dl_info.get("expected_size", 0)

    # Create new download with same parameters
    new_download_id = str(uuid.uuid4())

    with download_lock:
        downloads[new_download_id] = {
            "id": new_download_id,
            "model_id": model_id,
            "model_type": model_type,
            "status": "queued",
            "created_at": datetime.now().isoformat(),
            "total_bytes": 0,
            "downloaded_bytes": 0
        }
        if filename:
            downloads[new_download_id]["filename"] = filename
        download_events[new_download_id] = queue.Queue(maxsize=100)

    save_queue()

    future = download_executor.submit(
        download_worker,
        new_download_id, model_id, model_type, filename, expected_size
    )

    with download_lock:
        downloads[new_download_id]["future"] = future

    return jsonify({
        "download_id": new_download_id,
        "status": "queued",
        "model_id": model_id,
        "model_type": model_type,
        "filename": filename
    }), 202


@app.route("/downloads/<download_id>", methods=["DELETE"])
def delete_download(download_id: str):
    """Delete a download entry from the queue."""
    if download_id not in downloads:
        return jsonify({"error": "Download not found"}), 404

    # Store info for response
    dl_info = downloads[download_id].copy()

    # Remove from downloads
    with download_lock:
        del downloads[download_id]

    # Clean up event queue
    if download_id in download_events:
        del download_events[download_id]

    # Save updated queue
    save_queue()

    return jsonify({
        "status": "deleted",
        "model_id": dl_info.get("model_id"),
        "filename": dl_info.get("filename")
    })


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    """Clear Hugging Face cache for partial downloads."""
    import shutil

    model_id = request.json.get("model_id") if request.is_json else None

    cleared_dirs = []

    if model_id:
        # Clear cache for specific model only
        for cache_dir in Path(MODELS_PATH).rglob(f"*/{model_id}*/.cache/huggingface"):
            try:
                shutil.rmtree(cache_dir)
                cleared_dirs.append(str(cache_dir))
            except Exception:
                pass
    else:
        # Clear cache directories within all model folders
        for cache_dir in Path(MODELS_PATH).rglob("*/.cache/huggingface"):
            try:
                shutil.rmtree(cache_dir)
                cleared_dirs.append(str(cache_dir))
            except Exception:
                pass

    return jsonify({
        "status": "cleared",
        "cleared_dirs": cleared_dirs,
        "count": len(cleared_dirs)
    })


@app.route("/downloads/clear", methods=["POST"])
def clear_downloads():
    """Clear all downloads from the queue."""
    global downloads
    global download_events

    with download_lock:
        cleared_count = len(downloads)
        downloads.clear()
        download_events.clear()

    # Also clear the queue file
    if QUEUE_FILE.exists():
        try:
            QUEUE_FILE.unlink()
        except Exception:
            pass

    return jsonify({
        "status": "cleared",
        "cleared_count": cleared_count
    })


@app.route("/scan", methods=["POST"])
def scan_repo():
    """Scan a HuggingFace repo to list available files."""
    data = request.get_json()
    if not data or "model_id" not in data:
        return jsonify({"error": "model_id is required"}), 400

    model_id = data["model_id"]

    # Basic validation
    if ".." in model_id or model_id.startswith("/"):
        return jsonify({"error": "Invalid model_id"}), 400

    try:
        api = HfApi()
        repo_files = api.list_repo_files(
            repo_id=model_id,
            repo_type="model",
            token=os.environ.get("HF_TOKEN")
        )

        files = []
        for file_path in repo_files:
            if file_path.endswith((".gguf", ".safetensors")):
                file_type = "gguf" if file_path.endswith(".gguf") else "safetensors"
                files.append({
                    "filename": file_path,
                    "type": file_type,
                    "size_bytes": 0,  # Optional: get via API
                    "size_gb": 0,
                    "model_id": model_id
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
            # Check if download exists
            if download_id not in downloads:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Download not found'})}\n\n"
                return

            # Send current status first
            dl_info = downloads[download_id]
            yield f"data: {json.dumps({'type': 'status', 'status': dl_info['status']})}\n\n"

            # Add subscriber tracking
            with download_lock:
                if "subscribers" not in downloads[download_id]:
                    downloads[download_id]["subscribers"] = set()
                downloads[download_id]["subscribers"].add(id(event_stream))

            # Check if event queue exists (might not after restart)
            if download_id not in download_events:
                # Download exists but no event queue - likely orphaned from restart
                if dl_info['status'] in ['queued', 'downloading']:
                    # Update to failed since no worker is running
                    with download_lock:
                        downloads[download_id]['status'] = 'failed'
                        downloads[download_id]['error'] = 'Download interrupted by restart'
                yield f"data: {json.dumps({'type': 'error', 'message': 'Download interrupted - please retry'})}\n\n"
                return

            # Then stream events from queue
            while True:
                try:
                    event = download_events[download_id].get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"

                    # End stream on completion/error
                    if event["type"] in ["completed", "error"]:
                        break
                except queue.Empty:
                    # Send keepalive
                    yield ": keepalive\n\n"

                    # Check if download still exists
                    if download_id not in downloads:
                        break

        except GeneratorExit:
            # Client disconnected - cleanup
            with download_lock:
                if download_id in downloads and "subscribers" in downloads[download_id]:
                    downloads[download_id]["subscribers"].discard(id(event_stream))
            logging.info(f"SSE client disconnected for download {download_id}")
            raise

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no-cache",
            "Connection": "keep-alive"
        }
    )


@app.route("/", methods=["GET"])
def index():
    """Serve the main page."""
    with open(Path(__file__).parent / "static" / "index.html") as f:
        return f.read()


def cleanup_executor():
    """Clean up thread pool on shutdown."""
    download_executor.shutdown(wait=False)


atexit.register(cleanup_executor)


# Load queue on startup
load_queue()
start_periodic_save()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
