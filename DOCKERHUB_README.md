# Model Manager

Web service for downloading and managing Hugging Face models (GGUF / SafeTensors) onto your
own storage — local disk, ZFS dataset, or NFS mount. Flask backend + single-page UI.

## Quick Start
```bash
docker pull kazimurtaza/model-manager:latest

# Bind-mount your models dir (ZFS / local / NFS) to /models in the container:
docker run -d --name model-manager --restart unless-stopped \
  -p 5000:5000 \
  -v /tank/models:/models \
  -v "$PWD/data":/app/data \
  kazimurtaza/model-manager:latest
```
Access at http://localhost:5000

## Highlights
- Real, **cancellable** downloads — Cancel/Clear stop the running download and clean up partials
- SCAN a repo to list all files with real sizes, then pick the file(s) you want
- Server-Sent Events progress; persistent download queue (survives restarts)
- Configurable host path (`MODELS_HOST_PATH`); per-org namespaced storage

## Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `MODELS_HOST_PATH` | `/mnt/models` | Host dir bind-mounted to `/models` (ZFS / local / NFS) |
| `HF_TOKEN` | _(empty)_ | Hugging Face token (gated/private repos only) |
| `MAX_DOWNLOAD_WORKERS` | `3` | Max concurrent downloads (queue is unlimited) |
| `QUEUE_SAVE_INTERVAL` | `30` | Seconds between periodic queue saves |
| `COMPLETED_AUTO_CLEAR_SECONDS` | `10` | Seconds before a completed row auto-clears |

## Documentation
Full docs, API reference, and deployment notes (Docker LXC / Portainer):
https://github.com/kazimurtaza/model-manager

## Category
Developer Tools / Machine Learning
