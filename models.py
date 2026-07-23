"""
Model scanning and filesystem operations for model-manager.
"""
import os
import re
import shutil
from pathlib import Path
from typing import List, Dict


class ModelScanner:
    """Scan /models directory for GGUF and SafeTensors models."""

    def __init__(self, models_path: str | None = None):
        self.models_path = Path(os.environ.get("MODELS_PATH", models_path or "/models")).resolve()
        self.gguf_path = self.models_path / "gguf"
        self.safetensors_path = self.models_path / "safetensors"

    def _get_directory_size(self, path: Path) -> int:
        """Calculate total size of a directory in bytes."""
        total_size = 0
        try:
            for item in path.rglob("*"):
                if item.is_file():
                    total_size += item.stat().st_size
        except PermissionError:
            pass
        return total_size

    def _scan_gguf_models(self) -> List[Dict]:
        """Scan for GGUF models."""
        models = []
        if not self.gguf_path.exists():
            return models

        # First check for .gguf files directly in the gguf directory
        for gguf_file in self.gguf_path.glob("*.gguf"):
            size_bytes = gguf_file.stat().st_size
            size_gb = round(size_bytes / (1024**3), 2)

            models.append({
                "id": f"gguf_{gguf_file.stem}",
                "name": gguf_file.stem,
                "type": "gguf",
                "size_bytes": size_bytes,
                "size_gb": size_gb,
                "file_count": 1,
                "path": str(gguf_file.relative_to(self.models_path))
            })

        # Also check for model subdirectories (containing multiple .gguf files)
        for model_dir in self.gguf_path.iterdir():
            if not model_dir.is_dir():
                continue

            # Find .gguf files in subdirectory
            gguf_files = list(model_dir.glob("*.gguf"))
            if not gguf_files:
                continue

            # Full folder footprint (weights + config/tokenizer/.cache);
            # file_count stays the count of actual model weights files.
            size_bytes = self._get_directory_size(model_dir)
            size_gb = round(size_bytes / (1024**3), 2)

            models.append({
                "id": f"gguf_{model_dir.name}",
                "name": model_dir.name,
                "type": "gguf",
                "size_bytes": size_bytes,
                "size_gb": size_gb,
                "file_count": len(gguf_files),
                "path": str(model_dir.relative_to(self.models_path))
            })

        return models

    def _scan_safetensors_models(self) -> List[Dict]:
        """Scan for SafeTensors models."""
        models = []
        if not self.safetensors_path.exists():
            return models

        for model_dir in self.safetensors_path.iterdir():
            if not model_dir.is_dir():
                continue

            # Find .safetensors files
            st_files = list(model_dir.glob("*.safetensors"))
            if not st_files:
                continue

            # Full folder footprint (weights + config/tokenizer/.cache);
            # file_count stays the count of actual model weights files.
            size_bytes = self._get_directory_size(model_dir)
            size_gb = round(size_bytes / (1024**3), 2)

            models.append({
                "id": f"safetensors_{model_dir.name}",
                "name": model_dir.name,
                "type": "safetensors",
                "size_bytes": size_bytes,
                "size_gb": size_gb,
                "file_count": len(st_files),
                "path": str(model_dir.relative_to(self.models_path))
            })

        return models

    def scan_all(self) -> List[Dict]:
        """Scan all model types and return combined list."""
        models = []
        models.extend(self._scan_gguf_models())
        models.extend(self._scan_safetensors_models())
        return sorted(models, key=lambda x: x["name"])

    def get_model_path(self, model_id: str):
        """Get the filesystem path for a model ID."""
        if model_id.startswith("gguf_"):
            model_name = model_id[5:]  # Remove 'gguf_' prefix
            path = self.gguf_path / model_name
            # If it's a directory, return it
            if path.exists() and path.is_dir():
                return path
            # If not, try as a file
            if path.with_suffix('.gguf').exists():
                return path.with_suffix('.gguf')
        elif model_id.startswith("safetensors_"):
            model_name = model_id[12:]  # Remove 'safetensors_' prefix
            path = self.safetensors_path / model_name
            # If it's a directory, return it
            if path.exists() and path.is_dir():
                return path
            # If not, try as a file
            if path.with_suffix('.safetensors').exists():
                return path.with_suffix('.safetensors')
        else:
            return None

    def delete_model(self, model_id: str) -> bool:
        """Delete a model directory or file."""
        path = self.get_model_path(model_id)
        if path is None:
            return False

        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return True
        except Exception:
            return False

    def validate_model_id(self, model_id: str) -> bool:
        """Validate model ID to prevent directory traversal."""
        # Remove prefix
        if model_id.startswith("gguf_"):
            model_name = model_id[5:]
        elif model_id.startswith("safetensors_"):
            model_name = model_id[12:]
        else:
            return False

        # Check for path traversal attempts ("." alone resolves to the type root)
        if ".." in model_name or "/" in model_name or "\\" in model_name or model_name == ".":
            return False

        # Only allow alphanumeric, dash, underscore, dot
        if not re.match(r"^[\w\-\.]+$", model_name):
            return False

        return True
