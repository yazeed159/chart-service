"""Tests for import-job ownership and the upload size cap (run: python -m pytest tests/)."""
import io, os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("POLYGON_API_KEY", "test")
from flask import Flask
import import_routes as ir

JOB = "a" * 32


def client(tmp, user):
    ir.IMPORT_RUNS_DIR = type(ir.IMPORT_RUNS_DIR)(tmp)
    ir.resolve_user_id = lambda h: user
    app = Flask(__name__)
    app.register_blueprint(ir.bp)
    return app.test_client()


def test_owner_can_read_and_owner_field_is_hidden():
    with tempfile.TemporaryDirectory() as tmp:
        c = client(tmp, "alice")
        ir.IMPORT_RUNS_DIR = type(ir.IMPORT_RUNS_DIR)(tmp)
        ir._write_job(JOB, {"status": "processing", "total": 1}, owner="alice")
        ir._cancel_events[JOB] = ir.threading.Event()   # a running job has a live worker thread
        try:
            r = c.get(f"/import-trades/{JOB}")
            assert r.status_code == 200 and r.get_json()["status"] == "processing" and "owner" not in r.get_json()
        finally:
            ir._cancel_events.pop(JOB, None)


def test_processing_job_with_no_worker_thread_reports_interrupted():
    # The server restarted mid-run: the file still says "processing" but nothing is running it.
    with tempfile.TemporaryDirectory() as tmp:
        c = client(tmp, "alice")
        ir._cancel_events.pop(JOB, None)
        for status in ("processing", "cancelling"):
            ir._write_job(JOB, {"status": status, "total": 3, "completed": 1}, owner="alice")
            r = c.get(f"/import-trades/{JOB}")
            assert r.status_code == 404 and r.get_json()["error"] == "interrupted"


def test_finished_job_is_still_readable_without_a_worker_thread():
    with tempfile.TemporaryDirectory() as tmp:
        c = client(tmp, "alice")
        ir._cancel_events.pop(JOB, None)
        for status in ("done", "cancelled"):
            ir._write_job(JOB, {"status": status, "total": 3, "completed": 3}, owner="alice")
            r = c.get(f"/import-trades/{JOB}")
            assert r.status_code == 200 and r.get_json()["status"] == status


def test_other_user_gets_404_for_status_and_cancel():
    with tempfile.TemporaryDirectory() as tmp:
        c = client(tmp, "mallory")
        ir._write_job(JOB, {"status": "processing", "total": 1}, owner="alice")
        ir._cancel_events[JOB] = ir.threading.Event()
        assert c.get(f"/import-trades/{JOB}").status_code == 404
        assert c.post(f"/import-trades/{JOB}/cancel").status_code == 404
        assert not ir._cancel_events[JOB].is_set()
        ir._cancel_events.pop(JOB)


def test_no_token_is_401_and_ownerless_legacy_job_is_hidden():
    with tempfile.TemporaryDirectory() as tmp:
        c = client(tmp, None)
        ir._write_job(JOB, {"status": "done"})
        assert c.get(f"/import-trades/{JOB}").status_code == 401
        c = client(tmp, "alice")
        assert c.get(f"/import-trades/{JOB}").status_code == 404


def test_owner_survives_later_writes():
    with tempfile.TemporaryDirectory() as tmp:
        client(tmp, "alice")
        ir._write_job(JOB, {"status": "processing"}, owner="alice")
        ir._write_job(JOB, {"status": "done"})  # background-thread style write, no owner passed
        assert json.loads(ir._job_path(JOB).read_text())["owner"] == "alice"


def test_oversized_upload_is_rejected_with_json_413():
    import config
    app = config.app
    app.config["MAX_CONTENT_LENGTH"] = 1024
    @app.route("/_t", methods=["POST"])
    def _t():
        from flask import request
        return str(len(request.files["f"].read()))
    r = app.test_client().post("/_t", data={"f": (io.BytesIO(b"x" * 5000), "a.csv")}, content_type="multipart/form-data")
    assert r.status_code == 413 and "too large" in r.get_json()["error"]
