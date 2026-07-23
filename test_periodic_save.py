#!/usr/bin/env python3
"""Manual smoke check for the periodic queue save.

This now exercises the REAL app module. (It previously re-implemented save_queue,
testing a divergent copy.) For the automated suite, run:  pytest
"""
import os
import tempfile

# Point at temp dirs so we don't touch real storage.
os.environ.setdefault("DATA_PATH", tempfile.mkdtemp(prefix="mm-ps-data-"))
os.environ.setdefault("MODELS_PATH", tempfile.mkdtemp(prefix="mm-ps-models-"))

import app  # noqa: E402


def main():
    with app.download_lock:
        app.downloads.clear()
        app.downloads["smoke-1"] = {
            "id": "smoke-1", "status": app.STATUS_DOWNLOADING,
            "model_id": "o/n", "model_type": "gguf",
        }
    app.save_queue()
    app.start_periodic_save()
    print(f"Saved {len(app.downloads)} entries; file present at {app.QUEUE_FILE}: "
          f"{app.QUEUE_FILE.exists()}")
    print("Periodic save scheduled. Real-module smoke check OK.")


if __name__ == "__main__":
    main()
