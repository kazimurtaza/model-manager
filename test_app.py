"""Real test suite for model-manager.

Exercises the actual app module (no network): helpers, persistence, validation,
partial-file cleanup, and full-folder sizing. DATA_PATH/MODELS_PATH are redirected
to temp dirs before importing app so nothing touches /app/data or /models.
"""
import os
import tempfile

_DATA = tempfile.mkdtemp(prefix="mm-data-")
_MODELS = tempfile.mkdtemp(prefix="mm-models-")
os.environ["DATA_PATH"] = _DATA
os.environ["MODELS_PATH"] = _MODELS

import pytest  # noqa: E402

import app  # noqa: E402  (after env so QUEUE_FILE/scanner point at temp dirs)
from models import ModelScanner  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_state():
    with app.download_lock:
        app.downloads.clear()
        app.download_events.clear()
    yield
    with app.download_lock:
        app.downloads.clear()
        app.download_events.clear()


def test_health():
    client = app.app.test_client()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "healthy"


def test_repo_dirname_and_target():
    assert app.repo_to_dirname("org/name") == "org__name"
    assert app.repo_to_dirname("name") == "name"
    assert str(app.target_dir_for("a/b", "gguf")).replace("\\", "/").endswith("gguf/a__b")


def test_download_validation_returns_400_not_submitted():
    client = app.app.test_client()
    assert client.post("/download", json={}).status_code == 400
    assert client.post("/download", json={"model_id": "o/n", "model_type": "bad"}).status_code == 400
    assert client.post("/download", json={"model_id": "../x", "model_type": "gguf"}).status_code == 400
    assert client.post("/download", json={"model_id": "o/n", "model_type": "gguf",
                                          "filename": "../evil"}).status_code == 400
    # nothing was queued
    assert app.downloads == {}


def test_validate_model_id_rejects_dot_and_traversal():
    s = app.scanner
    assert s.validate_model_id("gguf_.") is False          # the rmtree-whole-tree edge
    assert s.validate_model_id("safetensors_.") is False
    assert s.validate_model_id("gguf_org__name") is True    # namespaced form accepted
    assert s.validate_model_id("gguf_..") is False
    assert s.validate_model_id("nonsense") is False


def test_save_keeps_failed_drops_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QUEUE_FILE", tmp_path / "queue.json")
    with app.download_lock:
        app.downloads["keep"] = {"id": "keep", "status": app.STATUS_FAILED,
                                  "model_id": "o/n", "model_type": "gguf"}
        app.downloads["gone"] = {"id": "gone", "status": app.STATUS_COMPLETED,
                                  "model_id": "o/n", "model_type": "gguf"}
    app.save_queue()

    with app.download_lock:
        app.downloads.clear()
    app.load_queue()

    with app.download_lock:
        assert "keep" in app.downloads          # failed persists across restart
        assert "gone" not in app.downloads      # completed never persisted


def test_delete_partial_single_file_only(tmp_path):
    d = tmp_path / "gguf" / "org__name"
    d.mkdir(parents=True)
    f = d / "model.gguf"
    f.write_bytes(b"x" * 10)
    (d / "config.json").write_text("{}")
    app.delete_partial({"target_dir": str(d), "filename": "model.gguf"})
    assert not f.exists()
    assert (d / "config.json").exists()         # only the named file removed


def test_delete_partial_whole_dir(tmp_path):
    d = tmp_path / "gguf" / "org__name"
    d.mkdir(parents=True)
    (d / "model.gguf").write_bytes(b"x")
    app.delete_partial({"target_dir": str(d), "filename": None})
    assert not d.exists()


def test_downloads_listing_strips_internal_fields():
    with app.download_lock:
        app.downloads["d1"] = {
            "id": "d1", "status": app.STATUS_QUEUED,
            "proc": object(), "future": object(), "stop_progress": object(),
        }
    r = app.app.test_client().get("/downloads").get_json()
    dl = r["downloads"][0]
    for key in ("proc", "future", "stop_progress", "subscribers"):
        assert key not in dl


def test_scan_full_folder_footprint(tmp_path, monkeypatch):
    models_root = tmp_path / "models"
    d = models_root / "gguf" / "org__name"
    d.mkdir(parents=True)
    (d / "model.gguf").write_bytes(b"x" * 100)
    (d / "config.json").write_bytes(b"y" * 50)   # support file now counted in size
    # ModelScanner.__init__ prefers the MODELS_PATH env var over its arg, so set it.
    monkeypatch.setenv("MODELS_PATH", str(models_root))
    sc = ModelScanner(str(models_root))
    models = sc.scan_all()
    m = next(x for x in models if x["id"] == "gguf_org__name")
    assert m["size_bytes"] == 150                # weights + config (full footprint)
    assert m["file_count"] == 1                  # weights files only
