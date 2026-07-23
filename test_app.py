"""Real test suite for model-manager.

Exercises the actual app module (no network): helpers, persistence, validation,
partial-file cleanup, and full-folder sizing. DATA_PATH/MODELS_PATH are redirected
to temp dirs before importing app so nothing touches /app/data or /models.
"""
import os
import json
import queue
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


# --------------------------------------------------------------------------- #
# download_worker + route tests (fake subprocess — no network)
# --------------------------------------------------------------------------- #
class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self._stderr = stderr

    def communicate(self):
        return (b"", self._stderr)

    def poll(self):
        return self.returncode


def _seed(download_id, **overrides):
    rec = {"id": download_id, "model_id": "org/name", "model_type": "gguf",
           "status": app.STATUS_QUEUED, "created_at": "2026-07-24T00:00:00",
           "expected_size": 0, "target_dir": str(app.target_dir_for("org/name", "gguf"))}
    rec.update(overrides)
    with app.download_lock:
        app.downloads[download_id] = rec
        app.download_events[download_id] = queue.Queue(maxsize=100)
    return rec


def test_worker_success_completes(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: _FakeProc(0))
    did = "w-ok"
    _seed(did)
    app.download_worker(did, "org/name", "gguf")
    with app.download_lock:
        assert app.downloads[did]["status"] == app.STATUS_COMPLETED


def test_worker_failure_uses_last_stderr_line(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: _FakeProc(1, b"first line\nboom: bad repo"))
    did = "w-fail"
    _seed(did)
    app.download_worker(did, "org/name", "gguf")
    with app.download_lock:
        assert app.downloads[did]["status"] == app.STATUS_FAILED
        assert app.downloads[did]["error"] == "boom: bad repo"


def test_worker_cancelled_before_spawn_does_not_run(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    started = {"ran": False}

    def fake_popen(*a, **k):
        started["ran"] = True
        return _FakeProc(0)

    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    did = "w-cancel"
    _seed(did, status=app.STATUS_CANCELLED)
    app.download_worker(did, "org/name", "gguf")
    assert started["ran"] is False  # worker bailed before spawning the subprocess


def test_worker_removed_mid_flight_cleans_partials(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)

    class _RemovingProc:
        returncode = 0

        def communicate(self):
            with app.download_lock:
                app.downloads.pop(did, None)
                app.download_events.pop(did, None)
            return (b"", b"")

        def poll(self):
            return 0

    did = "w-rm"
    _seed(did)
    target = app.target_dir_for("org/name", "gguf")
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: _RemovingProc())
    app.download_worker(did, "org/name", "gguf")
    assert not target.exists()  # delete_partial ran on the removed-mid-flight branch


def test_cancel_route_marks_cancelled_and_removes_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    d = tmp_path / "gguf" / "org__name"
    d.mkdir(parents=True)
    (d / "model.gguf").write_bytes(b"partial")
    did = "c1"
    with app.download_lock:
        app.downloads[did] = {"id": did, "model_id": "org/name", "model_type": "gguf",
                              "status": app.STATUS_DOWNLOADING, "target_dir": str(d),
                              "filename": "model.gguf"}
        app.download_events[did] = queue.Queue(maxsize=100)
    r = app.app.test_client().post(f"/downloads/{did}/cancel")
    assert r.status_code == 200 and r.get_json()["status"] == app.STATUS_CANCELLED
    assert not (d / "model.gguf").exists()
    assert app.download_events[did].get_nowait()["type"] == "cancelled"


def test_retry_replaces_in_place(monkeypatch):
    did = "r1"
    with app.download_lock:
        app.downloads[did] = {"id": did, "model_id": "org/name", "model_type": "gguf",
                              "status": app.STATUS_FAILED, "target_dir": "/x",
                              "expected_size": 100, "error": "old", "end_time": "2026-01-01"}
        app.download_events[did] = queue.Queue(maxsize=100)
    submitted = {}
    monkeypatch.setattr(app.download_executor, "submit", lambda fn, *a, **k: submitted.update(fn=fn))
    r = app.app.test_client().post(f"/downloads/{did}/retry")
    assert r.status_code == 202
    assert r.get_json()["download_id"] == did  # same id (replace in place)
    with app.download_lock:
        assert app.downloads[did]["status"] == app.STATUS_QUEUED
        assert app.downloads[did].get("error") is None
    assert "fn" in submitted
    # only failed/cancelled can retry
    with app.download_lock:
        app.downloads[did]["status"] = app.STATUS_DOWNLOADING
    assert app.app.test_client().post(f"/downloads/{did}/retry").status_code == 400


def test_delete_model_removes_related_rows(monkeypatch):
    monkeypatch.setattr(app.scanner, "validate_model_id", lambda mid: True)
    monkeypatch.setattr(app.scanner, "delete_model", lambda mid: True)
    with app.download_lock:
        app.downloads["keep"] = {"model_id": "other/x", "model_type": "gguf",
                                 "status": app.STATUS_QUEUED}
        app.downloads["gone"] = {"model_id": "org/name", "model_type": "gguf",
                                 "status": app.STATUS_COMPLETED}
        app.download_events["keep"] = queue.Queue(maxsize=100)
        app.download_events["gone"] = queue.Queue(maxsize=100)
    r = app.app.test_client().delete("/models/gguf_org__name")
    assert r.status_code == 200
    with app.download_lock:
        assert "gone" not in app.downloads  # repo_to_dirname("org/name") == "org__name" matches
        assert "keep" in app.downloads


def test_load_queue_marks_inflight_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QUEUE_FILE", tmp_path / "q.json")
    (tmp_path / "q.json").write_text(json.dumps({
        "dl1": {"id": "dl1", "status": "downloading", "model_id": "o/n", "model_type": "gguf"},
        "dl2": {"id": "dl2", "status": "queued", "model_id": "o/n", "model_type": "gguf"},
        "dl3": {"id": "dl3", "status": "failed", "model_id": "o/n", "model_type": "gguf"},
    }))
    app.load_queue()
    with app.download_lock:
        assert app.downloads["dl1"]["status"] == app.STATUS_FAILED
        assert "interrupted by restart" in app.downloads["dl1"]["error"]
        assert app.downloads["dl2"]["status"] == app.STATUS_FAILED
        assert app.downloads["dl3"]["status"] == app.STATUS_FAILED  # failed persists unchanged


def test_worker_does_not_block_when_no_sse_consumer(monkeypatch):
    # Regression for the HIGH event-queue bug: a bounded queue with no consumer must NOT hang.
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: _FakeProc(0))
    did = "w-nobuf"
    _seed(did)
    with app.download_lock:  # shrink the queue to 1 and never drain it
        app.download_events[did] = queue.Queue(maxsize=1)
    app.download_worker(did, "org/name", "gguf")  # would hang forever under the old blocking .put()
    with app.download_lock:
        assert app.downloads[did]["status"] == app.STATUS_COMPLETED
