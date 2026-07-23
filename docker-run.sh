#!/bin/bash
# Model Manager Docker Run Script

# Optional: Mount NFS share for centralized model storage
# Replace with your NFS server and paths
# sudo mkdir -p /mnt/models
# if ! mountpoint -q /mnt/models; then
#     sudo mount -t nfs YOUR_NFS_SERVER:/path/to/models /mnt/models
# fi

# Hugging Face authentication (optional)
HF_TOKEN="${HF_TOKEN:-}"

# Concurrency settings
MAX_DOWNLOAD_WORKERS="${MAX_DOWNLOAD_WORKERS:-3}"
QUEUE_SAVE_INTERVAL="${QUEUE_SAVE_INTERVAL:-30}"
COMPLETED_AUTO_CLEAR_SECONDS="${COMPLETED_AUTO_CLEAR_SECONDS:-10}"

if [ -z "$HF_TOKEN" ]; then
    echo "Info: No HF_TOKEN set - only public models accessible"
fi

# Docker run configuration
docker run -d \
  --name model-manager \
  --restart unless-stopped \
  -e HF_TOKEN="$HF_TOKEN" \
  -e MAX_DOWNLOAD_WORKERS="$MAX_DOWNLOAD_WORKERS" \
  -e QUEUE_SAVE_INTERVAL="$QUEUE_SAVE_INTERVAL" \
  -e COMPLETED_AUTO_CLEAR_SECONDS="$COMPLETED_AUTO_CLEAR_SECONDS" \
  -p 5000:5000 \
  -v /mnt/models:/models:rw \
  -v ./data:/app/data:rw \
  model-manager:latest