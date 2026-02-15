# Model Manager

Web service for downloading and managing Hugging Face models with NFS storage.

## Screenshot

![Model Manager UI](screenshot.jpg)

## Requirements

- Docker installed on your system
- NFS storage for models (or local directory)
- Port 5000 available

## Quick Start

### Using Docker
```bash
docker build -t model-manager:latest .
./docker-run.sh
```

### Using Docker Compose
```bash
docker-compose up -d
```

Access: http://localhost:5000

## Features

- Download models from Hugging Face Hub
- Real-time download progress via Server-Sent Events
- List and delete installed models
- Automatic file size detection from Hugging Face API
- Individual file selection for quantizations
- Persistent download queue (survives container restarts)

## API Endpoints

### Models
```
GET  /models
```
List all installed GGUF and SafeTensors models.

**Response:**
```json
{
  "models": [
    {
      "id": "gguf_model-name",
      "name": "model-name",
      "type": "gguf",
      "size_bytes": 1234567890,
      "size_gb": 11.5,
      "file_count": 1,
      "path": "gguf/model-name.gguf"
    }
  ]
}
```

### DELETE /models/{model_id}
Delete a model from disk.

**Response:**
```json
{
  "status": "deleted",
  "model_id": "gguf_model-name"
}
```

### Scan
```
POST /scan
```
Scan a Hugging Face repository to list available files and sizes.

**Request:**
```json
{
  "model_id": "unsloth/gemma-3-4b-it-GGUF"
}
```

**Response:**
```json
{
  "files": [
    {
      "filename": "gemma-3-4b-it-Q8_0.gguf",
      "model_id": "unsloth/gemma-3-4b-it-GGUF",
      "size_bytes": 7767803520,
      "size_gb": 7.23,
      "type": "gguf"
    }
  ]
}
```

### Downloads
```
GET  /downloads
```
List all active downloads.

**Response:**
```json
{
  "downloads": [
    {
      "id": "uuid",
      "model_id": "org/model",
      "model_type": "gguf",
      "filename": "specific-file.gguf",
      "status": "downloading",
      "created_at": "2026-02-15T...",
      "total_bytes": 0,
      "downloaded_bytes": 52428800
    }
  ]
}
```

Status values: `queued`, `downloading`, `completed`, `failed`, `cancelled`.

### POST /download
Start a model download.

**Request:**
```json
{
  "model_id": "org/model-name",
  "model_type": "gguf",
  "filename": "specific-file.gguf",
  "expected_size": 7767803520
}
```

**Response:**
```json
{
  "download_id": "uuid",
  "status": "queued",
  "model_id": "org/model-name",
  "model_type": "gguf",
  "filename": "specific-file.gguf"
}
```

### DELETE /downloads/{download_id}/cancel
```
POST /downloads/{download_id}/cancel
```
Cancel an active download.

**Response:**
```json
{
  "status": "cancelled"
}
```

### DELETE /downloads/{download_id}
Remove a download entry from the queue (does not delete model files).

### POST /downloads/clear
Clear all downloads from the queue.

**Response:**
```json
{
  "status": "cleared",
  "cleared_count": 3
}
```

### POST /cache/clear
Clear Hugging Face cache directories.

**Request (all):**
```json
{}
```

**Request (specific model):**
```json
{
  "model_id": "org/model-name"
}
```

**Response:**
```json
{
  "status": "cleared",
  "cleared_dirs": ["/models/gguf/model-name/.cache/huggingface"],
  "count": 1
}
```

### GET /events?download_id={id}
Server-Sent Events stream for download progress updates.

**Events:**
```json
{"type": "started", "model_id": "...", "target_dir": "..."}
{"type": "progress", "percent": 45, "downloaded": 456789012, "total": 1000000000}
{"type": "completed"}
{"type": "error", "message": "..."}
```

### GET /health
Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-02-15T10:30:00.123456"
}
```

## Storage

Models are stored in the configured models directory (default: `/models`).

**Directory structure:**
```
/models/
├── gguf/
│   ├── model-1.gguf
│   └── model-directory/
│       └── *.gguf files
└── safetensors/
    └── model-directory/
        └── *.safetensors files
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODELS_PATH` | `/models` | Path to model storage directory |
| `DATA_PATH` | `/app/data` | Path for download queue persistence |

## Authentication

The `huggingface_hub` library automatically uses the `HF_TOKEN` environment variable if present.

**For public models:** No configuration needed.

**For gated/private models:** Set `HF_TOKEN` environment variable:

```bash
# Set token (get from https://huggingface.co/settings/tokens)
export HF_TOKEN="hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# docker-run.sh will automatically pass this to the container
./docker-run.sh
```

The token is passed to the container via `-e HF_TOKEN="$HF_TOKEN"` in `docker-run.sh`.

## Project Structure

```
model-manager/
├── app.py              # Flask backend
├── models.py           # Model scanning logic
├── static/
│   └── index.html     # Web UI
├── requirements.txt      # Python dependencies
├── Dockerfile          # Container image
├── docker-run.sh       # Launch script
└── data/              # Persistent queue (created at runtime)
```

## Development

```bash
# Run Flask with debug mode
FLASK_DEBUG=1 docker run -it --rm -p 5000:5000 -v /models:/models model-manager:latest

# Check scanner logs
docker logs model-manager | grep -i scan
```

## Troubleshooting

**Container won't start:**
```bash
docker logs model-manager
# Look for Python errors
```

**Models not showing up:**
- Verify NFS mount is accessible: `docker exec model-manager ls /models`
- Check directory permissions: `docker exec model-manager ls -la /models`

**Download stuck at 0%:**
- Check browser console for SSE errors
- Verify `expected_size` was passed in download request

**NFS mount issues:**
```bash
# Test NFS connection (replace with your NFS server details)
sudo mount -t nfs nfs-server:/path/to/models /mnt/test-nfs
```
