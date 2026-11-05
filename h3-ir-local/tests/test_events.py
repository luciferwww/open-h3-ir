from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn

from h3_ir_local import compiler
from h3_ir_local.main import create_app
from h3_ir_local.storage import Storage
from h3ir.models import Brief

HEADERS = {"X-H3-Local": "1"}


def test_sse_closes_cleanly_if_record_is_deleted_after_initial_lookup(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    store = Storage(tmp_path)
    store.save({"id": "removed", "created_at": "2026-01-01", "status": "completed",
                "request": {"intent": "fox"}})
    original_get = Storage.get
    removed = False

    def delete_after_lookup(self, job_id):
        nonlocal removed
        job = original_get(self, job_id)
        if job_id == "removed" and not removed:
            assert job is not None
            removed = True
            assert self.delete(job_id)
        return job

    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        monkeypatch.setattr(Storage, "get", delete_after_lookup)
        response = client.get("/api/jobs/removed/events")
        assert response.status_code == 200
        data = [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith("data: ")]
        assert data == [{"id": "removed", "status": "deleted"}]
        assert not app.state.workspace.subscribers
        assert client.get("/api/jobs/removed/events").status_code == 404


@contextmanager
def live_server(directory):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(directory), log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not worker.is_alive() or time.monotonic() > deadline:
                raise AssertionError("test server failed to start")
            time.sleep(.01)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=HEADERS, timeout=10) as client:
            yield client
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        if worker.is_alive():
            raise AssertionError("test server failed to stop")


def snapshots(response):
    for line in response.iter_lines():
        if line.startswith("data: "):
            yield json.loads(line[6:])


def test_live_sse_delivers_before_completion_reconnects_and_closes(tmp_path, monkeypatch):
    from h3_ir_local import main
    monkeypatch.setattr(main, "SSE_HEARTBEAT_SECONDS", .05)
    release = threading.Event()
    worker_finished = threading.Event()

    def run(job, cfg, *, progress):
        progress("analysis", "Reading reference assets")
        try:
            if not release.wait(5):
                raise RuntimeError("test did not receive live progress")
            progress("writing", "Writing the IR")
            return ({"source": "written", "ir": {"prompt": "A real result"}},
                    compiler.BRIEF.dump_json(Brief(job["request"]["intent"])).decode())
        finally:
            worker_finished.set()

    monkeypatch.setattr(compiler, "run", run)
    try:
        with live_server(tmp_path) as client:
            response = client.post("/api/jobs", json={"intent": "fox"})
            assert response.status_code == 202
            job_id = response.json()["id"]
            url = f"/api/jobs/{job_id}/events"
            revision = None
            with client.stream("GET", url) as stream:
                assert stream.headers["content-type"].startswith("text/event-stream")
                assert stream.headers["x-accel-buffering"] == "no"
                for job in snapshots(stream):
                    if job["progress"][-1]["stage"] == "analysis":
                        assert job["status"] == "running"
                        assert not release.is_set()
                        assert not worker_finished.is_set()
                        revision = job["revision"]
                        break
            assert revision is not None, "analysis must arrive before the blocked compiler finishes"
            heartbeat_seen = False
            with client.stream("GET", url) as stream:
                for line in stream.iter_lines():
                    if line == ": heartbeat":
                        heartbeat_seen = True
                        assert not release.is_set()
                        assert not worker_finished.is_set()
                        break
            assert heartbeat_seen
            with client.stream("GET", url, headers={"Last-Event-ID": str(revision)}) as stream:
                updates = snapshots(stream)
                replay = next(updates)
                assert replay["progress"][-1]["stage"] == "analysis"
                release.set()
                received = list(updates)
            final = received[-1]
            assert final["status"] == "completed"
            assert final["result"]["ir"]["prompt"] == "A real result"
            assert "brief_json" not in final
            assert [s["stage"] for s in final["progress"]] == [
                "queued", "starting", "analysis", "writing", "completed"]
            assert final["revision"] > revision
            assert all(a["elapsed_s"] <= b["elapsed_s"]
                       for a, b in zip(final["progress"], final["progress"][1:]))
            with client.stream("GET", url) as stream:
                complete = list(snapshots(stream))
            assert len(complete) == 1 and complete[0]["status"] == "completed"
            assert client.get("/api/jobs/" + job_id).json()["progress"] == final["progress"]
            assert client.get("/api/jobs/missing/events").status_code == 404
    finally:
        release.set()


def test_sse_reports_failures_and_restarted_tasks(tmp_path, monkeypatch):
    def broken(job, cfg, *, progress):
        progress("connection", "Connecting")
        raise RuntimeError("deliberate test failure")

    monkeypatch.setattr(compiler, "run", broken)
    with live_server(tmp_path) as client:
        job_id = client.post("/api/jobs", json={"intent": "fox"}).json()["id"]
        with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
            final = list(snapshots(stream))[-1]
        assert final["status"] == "failed"
        assert final["error"]["code"] == "internal-error"
        assert final["progress"][-1]["stage"] == "failed"
    stored = Storage(tmp_path).get(job_id)
    stored["status"] = "running"
    stored.pop("progress")
    stored.pop("revision")
    Storage(tmp_path).save(stored)
    with live_server(tmp_path) as client:
        with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
            interrupted = list(snapshots(stream))
        assert len(interrupted) == 1
        assert interrupted[0]["status"] == "interrupted"
        assert interrupted[0]["progress"][-1]["stage"] == "interrupted"
        assert interrupted[0]["progress"][0]["elapsed_s"] == 0
        assert interrupted[0]["progress"][0]["at"] == interrupted[0]["finished_at"]
        assert interrupted[0]["finished_at"] > interrupted[0]["created_at"]


def test_older_core_explicitly_reports_coarse_progress(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(compiler, "SUPPORTS_PROGRESS", False)
    calls = []

    def compile_old(brief, **options):
        assert "progress" not in options
        raise RuntimeError("stop after verifying old-core options")

    monkeypatch.setattr(compiler, "compile_brief", compile_old)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1"):
        from h3ir.config import get_config
        try:
            compiler.run({"id": "test", "request": {"intent": "fox"}}, get_config(),
                         progress=lambda stage, message: calls.append(stage))
        except RuntimeError as exc:
            assert str(exc) == "stop after verifying old-core options"
        else:
            raise AssertionError("expected the compile stub")
    assert calls == ["preparing", "compiling"]
