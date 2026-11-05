from datetime import datetime, timedelta, timezone
import json

import pytest
from starlette.testclient import TestClient

from h3ir.models import Brief
from h3ir.service import BriefIn
from h3_ir_local import compiler
from h3_ir_local.main import create_app
from h3_ir_local.storage import Storage


def job(index, status="completed"):
    return {
        "id": f"job-{index:03d}", "parent_id": None, "version": 1,
        "created_at": (datetime(2020, 1, 1, tzinfo=timezone.utc) +
                       timedelta(seconds=index)).isoformat(),
        "status": status, "request": BriefIn(intent=f"scene {index}").model_dump(mode="json"),
        "brief_json": compiler.BRIEF.dump_json(Brief(f"scene {index}")).decode(),
        "attachments": [], "progress": [], "result": None, "error": None,
    }


def test_saves_remove_old_rows_not_just_hide_them(tmp_path):
    store = Storage(tmp_path)
    store.save_settings({"model": "keep-settings"})
    for index in range(130):
        store.save(job(index, ("completed", "failed", "interrupted")[index % 3]))
    expected = [f"job-{index:03d}" for index in range(129, 29, -1)]
    assert [row["id"] for row in store.recent(limit=None)] == expected
    for index in range(30):
        assert store.get(f"job-{index:03d}") is None
    retained = store.get("job-030")
    retained["result"] = {"updated": True}
    store.save(retained)
    reopened = Storage(tmp_path)
    assert [row["id"] for row in reopened.recent(limit=None)] == expected
    assert reopened.settings() == {"model": "keep-settings"}


def test_old_active_jobs_survive_until_they_finish(tmp_path):
    store = Storage(tmp_path)
    store.save(job(0, "queued"))
    store.save(job(1, "running"))
    for index in range(2, 102):
        store.save(job(index))
    assert len(store.recent(limit=None)) == 102
    assert store.get("job-000")["status"] == "queued"
    assert store.get("job-001")["status"] == "running"
    store.save(job(0, "failed"))
    assert store.get("job-000") is None
    assert store.get("job-001") is not None
    store.save(job(1, "completed"))
    assert len(store.recent(limit=None)) == 100
    assert store.get("job-001") is None


def test_startup_trims_preexisting_rows_after_interrupting_old_tasks(tmp_path):
    store = Storage(tmp_path)
    records = [job(index, "running" if index in (0, 134) else "completed")
               for index in range(135)]
    for row in records:
        folder = tmp_path / "creations" / row["id"]
        folder.mkdir(parents=True)
        (folder / "record.json").write_text(json.dumps(row), encoding="utf-8")
    for job_id in ("job-000", "job-134"):
        folder = tmp_path / "creations" / job_id / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "fixture").write_bytes(b"fixture")
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/jobs/job-000").status_code == 404
        history = client.get("/api/jobs").json()
        assert len(history) == 100
        assert history[0]["id"] == "job-134"
        assert history[0]["status"] == "interrupted"
        assert client.get("/api/bootstrap").json()["capabilities"]["history_limit"] == 100
        assert len(store.recent(limit=None)) == 100
        assert not (tmp_path / "creations" / "job-000").exists()
        assert (tmp_path / "creations" / "job-134" / "attachments" / "fixture").read_bytes() == b"fixture"


def test_history_list_includes_protected_active_jobs_outside_latest_hundred(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        store = Storage(tmp_path)
        store.save(job(0, "queued"))
        store.save(job(1, "running"))
        for index in range(2, 102):
            store.save(job(index))
        history = client.get("/api/jobs").json()
        assert len(history) == 102
        assert {row["id"] for row in history if row["status"] in ("queued", "running")} == {
            "job-000", "job-001"}


def test_record_and_settings_are_plain_utf8_json_without_a_database(tmp_path):
    store = Storage(tmp_path)
    saved = job(0, "failed")
    saved["error"] = {"code": "test", "message": "\u6d4b\u8bd5\u6545\u969c"}
    store.save(saved)
    settings = {"model": "vision-test", "timeout_s": 60}
    store.save_settings(settings)
    record = tmp_path / "creations" / saved["id"] / "record.json"
    assert record.is_file()
    text = record.read_text(encoding="utf-8")
    assert "\u6d4b\u8bd5\u6545\u969c" in text
    assert json.loads(text) == saved
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == settings
    assert not list(tmp_path.glob("*.sqlite3*"))
    assert Storage(tmp_path).get(saved["id"]) == saved
    assert Storage(tmp_path).settings() == settings
    assert store.delete(saved["id"]) is True
    assert not record.parent.exists()
    assert store.settings() == settings


def test_failed_json_replacement_preserves_previous_files(tmp_path, monkeypatch):
    from h3_ir_local import storage
    store = Storage(tmp_path)
    original = job(0)
    store.save(original)
    store.save_settings({"model": "original"})

    def failed_replace(source, target):
        raise PermissionError("simulated failed JSON replacement")

    monkeypatch.setattr(storage.os, "replace", failed_replace)
    with pytest.raises(PermissionError):
        store.save({**original, "revision": 2})
    with pytest.raises(PermissionError):
        store.save_settings({"model": "changed"})
    assert Storage(tmp_path).get(original["id"]) == original
    assert Storage(tmp_path).settings() == {"model": "original"}
    assert not list(tmp_path.rglob("*.tmp"))


def test_corrupt_json_is_reported_and_not_treated_as_missing(tmp_path):
    store = Storage(tmp_path)
    saved = job(0)
    store.save(saved)
    path = tmp_path / "creations" / saved["id"] / "record.json"
    path.write_text('{"id":', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        store.get(saved["id"])
    with pytest.raises(json.JSONDecodeError):
        store.recent()
    (tmp_path / "settings.json").write_text('{"model":', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        store.settings()
