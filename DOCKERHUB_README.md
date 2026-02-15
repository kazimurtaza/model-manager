# Model Manager

Web service for downloading and managing Hugging Face models with NFS storage.

## Features

- Download models from Hugging Face Hub
- Real-time download progress via Server-Sent Events
- List and delete installed models
- Automatic file size detection from Hugging Face API
- Individual file selection for quantizations
- Persistent download queue (survives container restarts)

## Quick Start

```bash
docker pull kazimurtaza/model-manager:latest

docker run -d \
  --name model-manager \
  --restart unless-stopped \
  -p 5000:5000 \
  -v /mnt/models:/models:rw \
  -v ./data:/app/data:rw \
  kazimurtaza/model-manager:latest
```

Access at: http://localhost:5000

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODELS_PATH` | `/models` | Path to model storage directory |
| `DATA_PATH` | `/app/data` | Path for download queue persistence |
| `HF_TOKEN` | - | Hugging Face authentication token (for gated models) |

## Documentation

For full documentation, API endpoints, and usage examples, visit:
https://github.com/kazimurtaza/model-manager

## Category

Developer Tools / Machine Learning
