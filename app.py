"""
Model Manager - Flask backend for Hugging Face model downloads.
"""
import os
import json
import uuid
import threading
import queue
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, request, jsonify, Response, stream_with_context
from huggingface_hub import HfApi, hf_hub_download, snapshot_download

from models import ModelScanner

app = Flask(__name__)

# Configuration
MODELS_PATH = os.environ.get("MODELS_PATH", "/models")
DATA_PATH = os.environ.get("DATA_PATH", "/app/data")
QUEUE_FILE = Path(DATA_PATH) / "download_queue.json"

# Initialize scanner
scanner = ModelScanner(MODELS_PATH)

# In-memory download tracking
downloads: Dict[str, Dict] = {}
download_events: Dict[str, queue.Queue] = {}
download_lock = threading.Lock()

# Simple in-memory rate limiter
download_requests = {}
RATE_LIMIT = 10  # requests per minute


def save_queue():
    """Save download queue to file."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_FILE, "w") as f:
        json.dump(downloads, f, indent=2, default=str)


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
            downloads[download_id]["status"] = "downloading"
            downloads[download_id]["expected_size"] = expected_size

        # Progress tracking function
        def send_progress_updates():
            """Send periodic progress updates while downloading."""
            while download_id in downloads and downloads[download_id]["status"] == "downloading":
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

        # Send completed event
        with download_lock:
            downloads[download_id]["status"] = "completed"
            downloads[download_id]["end_time"] = datetime.now().isoformat()

        if download_id in download_events:
            download_events[download_id].put({"type": "completed"})

    except Exception as e:
        # Send failed event
        with download_lock:
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

    # Start download in background thread
    thread = threading.Thread(
        target=download_worker,
        args=(download_id, model_id, model_type, filename, expected_size),
        daemon=True
    )
    thread.start()

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
        return jsonify({"downloads": list(downloads.values())})


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
    if ".." in model_id or "/" == model_id[0]:
        return jsonify({"error": "Invalid model_id"}), 400

    try:
        import requests

        # Use HuggingFace REST API to list repo files with sizes
        api_url = f"https://huggingface.co/api/models/{model_id}/tree/main"
        response = requests.get(api_url, timeout=10)

        if response.status_code != 200:
            return jsonify({"error": f"Failed to fetch repo info: {response.status_code}"}), 404

        tree_data = response.json()

        files = []

        # Handle both array and dict responses
        items = tree_data if isinstance(tree_data, list) else tree_data.get('children', [])

        for item in items:
            if isinstance(item, dict):
                if item.get('type') == 'file':
                    filename = item.get('path', '')
                    if filename.endswith((".gguf", ".safetensors")):
                        file_type = "gguf" if filename.endswith(".gguf") else "safetensors"
                        size_bytes = item.get('size', 0)
                        files.append({
                            "filename": filename,
                            "type": file_type,
                            "size_bytes": size_bytes,
                            "size_gb": round(size_bytes / (1024**3), 2) if size_bytes > 0 else 0,
                            "model_id": model_id
                        })
                # Check for nested children (subdirectories)
                if 'children' in item:
                    for child in item['children']:
                        if child.get('type') == 'file':
                            filename = child.get('path', '')
                            if filename.endswith((".gguf", ".safetensors")):
                                file_type = "gguf" if filename.endswith(".gguf") else "safetensors"
                                size_bytes = child.get('size', 0)
                                files.append({
                                    "filename": filename,
                                    "type": file_type,
                                    "size_bytes": size_bytes,
                                    "size_gb": round(size_bytes / (1024**3), 2) if size_bytes > 0 else 0,
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
            pass

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


# Load queue on startup
load_queue()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
