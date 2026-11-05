from __future__ import annotations

import hashlib
import base64
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.testclient import TestClient

from h3ir.backend import Backend, BackendUnavailable
from h3ir.compile import compile_brief as real_compile
from h3ir.config import get_config
from h3ir.models import AssetKind, Brief, DialogueLine, Role
from h3ir.service import BriefIn
from h3ir import compile as core, uploads
from h3ir.models import AssetCard

from h3_ir_local import compiler
from h3_ir_local.main import create_app
from h3_ir_local.storage import Storage

HEADERS = {"X-H3-Local": "1"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def test_complete_snapshot_survives_eviction_restart_and_refinement(tmp_path, monkeypatch):
    seen = []

    def deterministic(brief, **options):
        seen.append(brief)
        return real_compile(brief, **options, llm=False)

    def analyse(backend, refs, **kwargs):
        return {ref.sha256: AssetCard(
            sha256=ref.sha256, kind=ref.kind, summary="A woman on a beach.",
            style="Live-action, cinematic", environment="a sandy beach",
            subjects=[{"kind": "person", "descriptor": "the woman", "attributes": ["swimsuit"]}])
                for ref in refs}

    def amend(brief, change, **options):
        updated = replace(brief, intent="She looks towards the mountains.", seconds=8,
                          shots=2, constraints=["No music.", "Slow camera movement."])
        return deterministic(updated, **options), updated, ["intent", "seconds", "shots", "constraints"]

    monkeypatch.setattr(Backend, "require_available", lambda self: None)
    monkeypatch.setattr(Backend, "server_version", lambda self: "test")
    monkeypatch.setattr(core, "analyse_all", analyse)
    monkeypatch.setattr(compiler, "compile_brief", deterministic)
    monkeypatch.setattr(compiler, "refine", amend)
    with client_for(tmp_path) as client:
        client.put("/api/settings", json={"base_url": "https://example.invalid/v1",
                                           "model": "vision-test", "timeout_s": 45,
                                           "api_key": "do-not-archive-this-key"})
        digest = put(client, PNG)
        payload = {"intent": "A woman stands on a beach.", "seconds": 6, "aspect": "9:16",
                   "shots": 1, "megapixels": 1.0, "seed": 123, "effort": "max",
                   "creativity": "bold", "silent": False, "constraints": ["No music."],
                   "onscreen_text": ["OPEN"], "dialogue": [{"text": "Wait here.", "language": "English"}],
                   "director_profile": {"name": "local", "notes": "Natural light."},
                   "transcripts": {digest: "Caller supplied exact transcript."},
                   "assets": [{"sha256": digest, "kind": "image", "role": "subject",
                               "sizing": "max", "note": "The opening frame.",
                               "provenance": {"source": "user camera"}}],
                   "asset_names": ["海边原图.png"]}
        response = client.post("/api/jobs", json=payload)
        assert response.status_code == 202, response.text
        job = finished(client, response.json()["id"])
        assert job["status"] == "completed", job
        expected = BriefIn.model_validate({k: v for k, v in payload.items() if k != "asset_names"}).model_dump(mode="json")
        assert job["request"] == expected
        removed = {"effective_request", "compiler_config", "compiler_version",
                   "snapshot_version", "snapshot_notice", "model"}
        assert removed.isdisjoint(job)
        assert removed.isdisjoint(Storage(tmp_path).get(job["id"]))
        assert "do-not-archive-this-key" not in json.dumps(job)
        assert job["attachments"][0] == {
            "index": 0, "sha256": digest, "kind": "image", "name": "海边原图.png",
            "original_name": True, "bytes": len(PNG), "available": True}
        assert Path(seen[0].assets[0].path) == tmp_path / "creations" / job["id"] / "attachments" / digest
        assert client.get(f"/api/jobs/{job['id']}/assets/0").content == PNG
        uploads.sweep(now=time.time() + 3 * 86400)
        assert uploads.stored(digest) is None
        assert client.get(f"/api/jobs/{job['id']}/assets/0").content == PNG
    with client_for(tmp_path) as restarted:
        original = restarted.get("/api/jobs/" + job["id"]).json()
        assert original == job
        child_response = restarted.post("/api/jobs/" + job["id"] + "/refinements",
                                         json={"change": "Eight seconds; show the mountains."})
        assert child_response.status_code == 202, child_response.text
        child = finished(restarted, child_response.json()["id"])
        assert child["status"] == "completed", child
        assert child["request"]["seconds"] == 8
        assert child["request"]["intent"] == "She looks towards the mountains."
        assert child["request"]["shots"] == 2
        assert child["request"]["constraints"] == ["No music.", "Slow camera movement."]
        assert removed.isdisjoint(child)
        assert child["attachments"] == job["attachments"]
        parent_file = tmp_path / "creations" / job["id"] / "attachments" / digest
        child_file = tmp_path / "creations" / child["id"] / "attachments" / digest
        assert parent_file.read_bytes() == child_file.read_bytes() == PNG
        assert not parent_file.samefile(child_file)
        assert restarted.get("/api/jobs/" + job["id"]).json() == original
        assert restarted.delete("/api/jobs/" + job["id"]).status_code == 200
        assert not parent_file.parent.parent.exists()
        assert restarted.get(f"/api/jobs/{child['id']}/assets/0").content == PNG
        again = restarted.post("/api/jobs", json={
            **child["request"], "asset_names": [child["attachments"][0]["name"]],
            "source_job_id": child["id"]})
        assert again.status_code == 202, again.text
        reused = finished(restarted, again.json()["id"])
        assert reused["status"] == "completed", reused
        assert reused["request"] == child["request"]
        assert "Wait here." in reused["result"]["ir"]["prompt"]
        assert len(list((tmp_path / "creations").glob("*/attachments/*"))) == 2


def test_attachment_metadata_pairing_downloads_and_failed_inputs_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "run", lambda job, cfg, **kwargs:
                        ({"source": "draft"}, job["brief_json"]))
    with client_for(tmp_path) as client:
        image, video, audio = (put(client, content) for content in (PNG, b"video content", b"audio content"))
        payload = {"intent": "Use the supplied references.",
                   "assets": [{"sha256": image, "kind": "image", "role": "replacement_subject",
                               "replaces": "the pilot"},
                              {"sha256": video, "kind": "video", "role": "edit_source",
                               "seconds": 5, "frames": 124},
                              {"sha256": audio, "kind": "audio", "role": "voice_timbre",
                               "paired_video_sha256": video}],
                   "asset_names": ["portrait.png", "clip.mp4", "voice.wav"],
                   "transcripts": {audio: "Exact spoken words."}}
        job = finished(client, client.post("/api/jobs", json=payload).json()["id"])
        assert job["status"] == "completed", job
        assert job["request"]["assets"][0]["replaces"] == "the pilot"
        assert job["request"]["assets"][2]["paired_video_sha256"] == video
        for index, content in enumerate((PNG, b"video content", b"audio content")):
            response = client.get(f"/api/jobs/{job['id']}/assets/{index}?download=true")
            assert response.content == content
            assert "attachment" in response.headers["content-disposition"]
            assert payload["asset_names"][index] in response.headers["content-disposition"]
            assert hashlib.sha256(response.content).hexdigest() == job["attachments"][index]["sha256"]
        assert client.get(f"/api/jobs/{job['id']}/assets/1", headers={"Range": "bytes=0-4"}).content == b"video"
        assert client.get(f"/api/jobs/{job['id']}/assets/99").status_code == 404
        assert client.get(f"/api/jobs/{job['id']}/assets/-1").status_code == 404
        assert client.get("/api/assets/" + image).status_code == 405
        for names in (["one.png"], ["../outside.png", "a.mp4", "b.wav"],
                      ["x\n.png", "a.mp4", "b.wav"]):
            assert client.post("/api/jobs", json={**payload, "asset_names": names}).status_code == 422
        unsafe_name = finished(client, client.post("/api/jobs", json={
            "intent": "fox", "assets": [{"sha256": image}], "asset_names": ["drawing.svg"]}).json()["id"])
        response = client.get(f"/api/jobs/{unsafe_name['id']}/assets/0")
        assert response.headers["content-type"] == "application/octet-stream"
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["x-content-type-options"] == "nosniff"

        def offline(job, cfg, **kwargs):
            raise BackendUnavailable("offline")

        monkeypatch.setattr(compiler, "run", offline)
        failed = finished(client, client.post("/api/jobs", json=payload).json()["id"])
        assert failed["status"] == "failed"
        assert failed["request"]["transcripts"] == payload["transcripts"]
        assert failed["attachments"] == job["attachments"]
        assert client.get(f"/api/jobs/{failed['id']}/assets/2").content == b"audio content"


def test_missing_or_corrupt_source_refuses_copy_without_accepting_job(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "run", lambda job, cfg, **kwargs:
                        ({"source": "draft"}, job["brief_json"]))
    with client_for(tmp_path) as client:
        digest = put(client, PNG)
        job = finished(client, client.post("/api/jobs", json={
            "intent": "fox", "assets": [{"sha256": digest}]}).json()["id"])
        uploads.sweep(now=time.time() + 3 * 86400)
        original = tmp_path / "creations" / job["id"] / "attachments" / digest
        original.write_bytes(b"corrupted source")
        response = client.post("/api/jobs/" + job["id"] + "/refinements",
                               json={"change": "Slow camera."})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "asset-digest-mismatch"
        payload = {**job["request"], "source_job_id": job["id"]}
        response = client.post("/api/jobs", json=payload)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "asset-digest-mismatch"
        assert len(client.get("/api/jobs").json()) == 1
        assert len(list((tmp_path / "creations").iterdir())) == 1
        original.unlink()
        assert client.get(f"/api/jobs/{job['id']}/assets/0").status_code == 410
        assert client.post("/api/jobs", json=payload).status_code == 422
        assert len(list((tmp_path / "creations").iterdir())) == 1
        payload["source_job_id"] = "deleted"
        assert client.post("/api/jobs", json=payload).status_code == 404
        assert len(client.get("/api/jobs").json()) == 1


def test_archive_refuses_corrupt_bytes_or_disk_failure_without_accepting_job(tmp_path, monkeypatch):
    from h3_ir_local import archive
    with client_for(tmp_path) as client:
        digest = put(client, PNG)
        uploads.path_for(digest).write_bytes(b"corrupted upload cache")
        response = client.post("/api/jobs", json={"intent": "fox", "assets": [{"sha256": digest}]})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "asset-digest-mismatch"
        assert client.get("/api/jobs").json() == []
        assert not list((tmp_path / "creations").iterdir())
        uploads.path_for(digest).write_bytes(PNG)

        def disk_full(*args):
            raise OSError("simulated disk full")

        original_replace = archive.os.replace
        monkeypatch.setattr(archive.os, "replace", disk_full)
        response = client.post("/api/jobs", json={"intent": "fox", "assets": [{"sha256": digest}]})
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "internal-error"
        assert client.get("/api/jobs").json() == []
        assert not list((tmp_path / "creations").iterdir())
        monkeypatch.setattr(archive.os, "replace", original_replace)

        def failed_save(self, job):
            raise OSError("simulated database write failure")

        monkeypatch.setattr(Storage, "save", failed_save)
        assert client.post("/api/jobs", json={
            "intent": "fox", "assets": [{"sha256": digest}]}).status_code == 500
        assert client.get("/api/jobs").json() == []
        assert not list((tmp_path / "creations").iterdir())


def test_delete_file_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    from h3_ir_local import archive
    monkeypatch.setattr(compiler, "run", lambda job, cfg, **kwargs:
                        ({"source": "draft"}, job["brief_json"]))
    with client_for(tmp_path) as client:
        digest = put(client, PNG)
        job = finished(client, client.post("/api/jobs", json={
            "intent": "fox", "assets": [{"sha256": digest}]}).json()["id"])

        def locked(path):
            raise PermissionError("simulated locked creation directory")

        monkeypatch.setattr(archive.shutil, "rmtree", locked)
        response = client.delete("/api/jobs/" + job["id"])
        assert response.status_code == 500
        assert Storage(tmp_path).get(job["id"]) is not None
        assert client.get(f"/api/jobs/{job['id']}/assets/0").content == PNG


def test_active_creation_keeps_its_directory(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked(job, cfg, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test worker was not released")
        return {"source": "draft"}, job["brief_json"]

    monkeypatch.setattr(compiler, "run", blocked)
    with client_for(tmp_path) as client:
        try:
            digest = put(client, PNG)
            job_id = client.post("/api/jobs", json={
                "intent": "fox", "assets": [{"sha256": digest}]}).json()["id"]
            assert entered.wait(3)
            assert client.delete("/api/jobs/" + job_id).status_code == 409
            assert (tmp_path / "creations" / job_id / "attachments" / digest).read_bytes() == PNG
        finally:
            release.set()
        assert finished(client, job_id)["status"] == "completed"
        assert client.delete("/api/jobs/" + job_id).status_code == 200
        assert not (tmp_path / "creations" / job_id).exists()


def test_delete_is_persistent_and_preserves_versions_and_assets(tmp_path, monkeypatch):
    def deterministic(brief, **options):
        return real_compile(brief, **options, llm=False)

    def amend(brief, change, **options):
        amended = replace(brief, intent=brief.intent + " " + change)
        return deterministic(amended, **options), amended, ["intent"]

    monkeypatch.setattr(Backend, "require_available", lambda self: None)
    monkeypatch.setattr(Backend, "server_version", lambda self: "test")
    monkeypatch.setattr(compiler, "compile_brief", deterministic)
    monkeypatch.setattr(compiler, "refine", amend)
    with client_for(tmp_path) as client:
        digest = put(client, b"shared image bytes")
        asset_path = Path(compiler.prepare(BriefIn.model_validate({
            "intent": "fox", "assets": [{"sha256": digest, "kind": "image"}]})).assets[0].path)
        parent = finished(client, client.post("/api/jobs", json={"intent": "fox"}).json()["id"])
        parent_url = "/api/jobs/" + parent["id"]
        child = finished(client, client.post(parent_url + "/refinements",
                                            json={"change": "Darker."}).json()["id"])
        assert client.delete(parent_url, headers={"X-H3-Local": ""}).status_code == 403
        assert client.delete(parent_url, headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.get(parent_url).status_code == 200
        response = client.delete(parent_url)
        assert response.status_code == 200
        assert response.json() == {"id": parent["id"], "deleted": True}
        assert client.get(parent_url).status_code == 404
        assert client.get(parent_url + "/events").status_code == 404
        assert client.delete(parent_url).status_code == 404
        assert client.post(parent_url + "/refinements", json={"change": "Brighter."}).status_code == 404
        assert parent["id"] not in [job["id"] for job in client.get("/api/jobs").json()]
        assert client.get("/api/jobs/" + child["id"]).json() == child
        assert asset_path.exists()
        assert Storage(tmp_path).get(parent["id"]) is None
    with client_for(tmp_path) as restarted:
        assert restarted.get(parent_url).status_code == 404
        refined = finished(restarted, restarted.post("/api/jobs/" + child["id"] + "/refinements",
                                                     json={"change": "Blue sky."}).json()["id"])
        assert refined["status"] == "completed" and refined["version"] == 3
        assert asset_path.exists()


def test_pruned_parent_does_not_break_retained_child_or_shared_files(tmp_path, monkeypatch):
    def run(job, cfg, **options):
        brief = compiler.BRIEF.validate_json(job["brief_json"])
        if job.get("change"):
            brief = replace(brief, intent=brief.intent + " " + job["change"])
        return {"source": "draft"}, compiler.BRIEF.dump_json(brief).decode()

    monkeypatch.setattr(compiler, "run", run)
    with client_for(tmp_path) as client:
        digest = put(client, PNG)
        response = client.post("/api/jobs", json={
            "intent": "A beach.", "assets": [{"sha256": digest, "role": "subject"}],
            "asset_names": ["beach.png"]})
        parent = finished(client, response.json()["id"])
        child = finished(client, client.post("/api/jobs/" + parent["id"] + "/refinements",
                                            json={"change": "Show mountains."}).json()["id"])
        store = Storage(tmp_path)
        old = store.get(parent["id"])
        old["created_at"] = "2000-01-01T00:00:00+00:00"
        store.save(old)
        for index in range(99):
            store.save({**store.get(child["id"]), "id": f"filler-{index}",
                        "created_at": f"2001-01-{index // 24 + 1:02d}T{index % 24:02d}:00:00+00:00"})
        assert client.get("/api/jobs/" + parent["id"]).status_code == 404
        assert not (tmp_path / "creations" / parent["id"]).exists()
        assert len(store.recent(limit=None)) == 100
        assert client.get(f"/api/jobs/{child['id']}/assets/0").content == PNG
        uploads.sweep(now=time.time() + 3 * 86400)
    with client_for(tmp_path) as restarted:
        refined = finished(restarted, restarted.post(
            "/api/jobs/" + child["id"] + "/refinements",
            json={"change": "Slow camera."}).json()["id"])
        assert refined["status"] == "completed"
        assert refined["version"] == 3
        assert refined["request"]["intent"] == "A beach. Show mountains. Slow camera."
        assert restarted.get(f"/api/jobs/{refined['id']}/assets/0").content == PNG
        assert len(Storage(tmp_path).recent(limit=None)) == 100
        assert (tmp_path / "creations" / child["id"] / "attachments" / digest).read_bytes() == PNG
        assert len(list((tmp_path / "creations").glob("*/attachments/*"))) == 2


def test_delete_rejects_active_jobs_and_accepts_failed_or_interrupted(tmp_path, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def blocked(job, cfg, *, progress):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test compiler was not released")
        raise BackendUnavailable("offline")

    monkeypatch.setattr(compiler, "run", blocked)
    try:
        with client_for(tmp_path) as client:
            job_id = client.post("/api/jobs", json={"intent": "fox"}).json()["id"]
            assert entered.wait(2)
            url = "/api/jobs/" + job_id
            response = client.delete(url)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "job-active"
            assert client.get(url).json()["status"] == "running"
            assert client.get("/api/health").json()["busy"] is True
            release.set()
            assert finished(client, job_id)["status"] == "failed"
            assert client.delete(url).status_code == 200
            assert client.get(url).status_code == 404
            for status in ("queued", "interrupted"):
                stored = {"id": status, "created_at": "2026-01-01", "status": status}
                Storage(tmp_path).save(stored)
                response = client.delete("/api/jobs/" + status)
                assert response.status_code == (409 if status == "queued" else 200)
                assert (Storage(tmp_path).get(status) is not None) == (status == "queued")
    finally:
        release.set()


def client_for(directory: Path) -> TestClient:
    return TestClient(create_app(directory), base_url="http://127.0.0.1",
                      headers=HEADERS, raise_server_exceptions=False)


def finished(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get("/api/jobs/" + job_id)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(.01)
    raise AssertionError("job did not finish")


def put(client: TestClient, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    response = client.put("/api/assets/" + digest, content=data)
    assert response.status_code in (200, 201), response.text
    return digest


def test_page_contract_and_local_boundary(tmp_path):
    with client_for(tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "H3-IR Local" in page.text and "vue@3.5.13" in page.text
        assert "__VUE_URL__" not in page.text
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["contract"]["package_version"] == "0.4.1"
        assert "replacement_subject" in bootstrap["contract"]["roles"]["image"]
        assert bootstrap["capabilities"]["assets"]["paths"] is False
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/", headers={"host": "evil.invalid"}).status_code == 400
        assert client.post("/api/jobs", json={"intent": "fox"},
                           headers={"X-H3-Local": ""}).status_code == 403
        assert client.post("/api/jobs", json={"intent": "fox"},
                           headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/").headers["cache-control"] == "no-store"
        assert client.get("/api/jobs/missing").status_code == 404


def test_upload_conversion_preserves_roles_pairing_and_words(tmp_path):
    with client_for(tmp_path) as client:
        image = put(client, b"image bytes")
        video = put(client, b"video bytes")
        audio = put(client, b"audio bytes")
        payload = {
            "intent": "Replace the pilot.",
            "assets": [
                {"sha256": image, "kind": "image", "role": "replacement_subject",
                 "replaces": "the pilot in the blue coat", "note": "new pilot"},
                {"sha256": video, "kind": "video", "role": "edit_source"},
                {"sha256": audio, "kind": "audio", "role": "voice_timbre",
                 "paired_video_sha256": video, "note": "low measured voice"},
            ],
            "dialogue": [{"text": "别改这句话！", "language": "Chinese", "speaker": "pilot"}],
            "transcripts": {audio: "Actual words."},
            "director_profile": {"name": "local", "notes": "Muted light."},
        }
        brief = compiler.prepare(BriefIn.model_validate(payload))
        assert brief.assets[0].replaces == "the pilot in the blue coat"
        assert brief.assets[0].role is Role.REPLACEMENT_SUBJECT
        assert brief.assets[0].role_stated is True
        assert brief.assets[2].paired_video_sha256 == video
        assert brief.assets[2].kind is AssetKind.AUDIO
        assert brief.dialogue[0].text == "别改这句话！"
        assert brief.dialogue[0].speaker_hint == "pilot"
        assert brief.director_profile == payload["director_profile"]
        restored = compiler.BRIEF.validate_json(compiler.BRIEF.dump_json(brief))
        assert restored == brief
        inferred = compiler.prepare(BriefIn(intent="photo", assets=[{"sha256": image}]))
        assert inferred.assets[0].role_stated is False
        assert client.put("/api/assets/" + "0" * 64, content=b"wrong").status_code == 422
        assert client.put("/api/assets/not-a-hash", content=b"wrong").status_code == 422
        assert client.get("/api/assets/" + image).status_code == 405
        for field in ("path", "url", "paired_video_path"):
            bad = {"intent": "fox", "assets": [{"sha256": image, field: "not-allowed"}]}
            result = client.post("/api/jobs", json=bad)
            assert result.status_code == 422
            assert result.json()["detail"]["code"] == "uploads-only"
        assert client.post("/api/jobs", json={"intent": "fox", "unexpected": True}).status_code == 422
        invalid = client.post("/api/jobs", json={
            "intent": "fox", "assets": [{"sha256": image, "role": "nonexistent"}]})
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "unknown-role"


def test_settings_secrets_and_busy_guard(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked(job, cfg, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test worker was not released")
        return {"source": "draft", "fallback_reason": "test"}, compiler.BRIEF.dump_json(Brief("fox")).decode()

    monkeypatch.setattr(compiler, "run", blocked)
    try:
        with client_for(tmp_path) as client:
            response = client.put("/api/settings", json={
                "base_url": "https://example.invalid/v1/", "model": "vision",
                "api_key": "test-secret-value", "timeout_s": 30})
            assert response.status_code == 200, response.text
            assert response.json()["has_api_key"] is True
            assert response.json()["base_url"] == "https://example.invalid/v1"
            assert "test-secret-value" not in client.get("/api/settings").text
            assert "test-secret-value" not in json.dumps(Storage(tmp_path).settings())
            assert get_config().llm.api_key == "test-secret-value"
            client.put("/api/settings", json={"base_url": "https://example.invalid/v1",
                                              "model": "vision", "api_key": None})
            assert get_config().llm.api_key == "test-secret-value"
            result = client.post("/api/jobs", json={"intent": "fox"})
            assert result.status_code == 202
            assert entered.wait(5)
            assert client.post("/api/jobs", json={"intent": "dog"}).status_code == 409
            assert client.put("/api/settings", json={
                "base_url": "https://example.invalid/v1"}).status_code == 409
            release.set()
            assert finished(client, result.json()["id"])["status"] == "completed"
            assert client.put("/api/settings", json={
                "base_url": "https://example.invalid/v1", "api_key": ""}).json()["has_api_key"] is False
            assert client.put("/api/settings", json={
                "base_url": "https://user:password@example.invalid/v1"}).status_code == 422
            assert client.put("/api/settings", json={
                "base_url": "http://localhost:bad/v1"}).status_code == 422
    finally:
        release.set()
    with client_for(tmp_path) as restarted:
        settings = restarted.get("/api/settings").json()
        assert settings["base_url"] == "https://example.invalid/v1"


def test_real_core_result_and_refinement_survive_restart(tmp_path, monkeypatch):
    seen = []

    def deterministic(brief, **options):
        seen.append((brief, options))
        options["llm"] = False
        return real_compile(brief, **options)

    def amend(brief, change, **options):
        amended = replace(brief, intent=brief.intent + " " + change, seconds=8)
        return deterministic(amended, **options), amended, ["intent", "seconds"]

    monkeypatch.setattr(Backend, "require_available", lambda self: None)
    monkeypatch.setattr(Backend, "server_version", lambda self: "test")
    monkeypatch.setattr(compiler, "compile_brief", deterministic)
    monkeypatch.setattr(compiler, "refine", amend)
    with client_for(tmp_path) as client:
        response = client.post("/api/jobs", json={
            "intent": "A fox walks across a field.", "seed": 19,
            "transcripts": {"a" * 64: "verbatim transcript"},
            "dialogue": [{"text": "Stay here.", "language": "English"}]})
        assert response.status_code == 202, response.text
        original = finished(client, response.json()["id"])
        assert original["status"] == "completed", original
        assert "brief_json" not in original
        result = original["result"]
        assert result["source"] == "draft"
        assert result["ir"]["target"]["frames"] % 17 == 5
        assert "Stay here." in result["ir"]["prompt"]
        assert seen[0][1]["seed"] == 19
        assert seen[0][1]["transcripts"] == {"a" * 64: "verbatim transcript"}
        assert not any(f["severity"] == "ERROR" for f in result["ir"]["diagnostics"])
    with client_for(tmp_path) as restarted:
        old = restarted.get("/api/jobs/" + original["id"]).json()
        assert old["result"] == original["result"]
        response = restarted.post("/api/jobs/" + original["id"] + "/refinements",
                                  json={"change": "Darker."})
        assert response.status_code == 202, response.text
        refined = finished(restarted, response.json()["id"])
        assert refined["status"] == "completed", refined
        assert refined["parent_id"] == original["id"]
        assert refined["version"] == 2
        assert refined["result"]["ir"]["target"]["nominal_seconds"] == 8
        assert refined["result"]["changed"] == ["intent", "seconds"]
        assert seen[-1][1]["transcripts"] == {"a" * 64: "verbatim transcript"}
        assert seen[-1][1]["seed"] == 19
        assert restarted.get("/api/jobs/" + original["id"]).json()["result"] == original["result"]
        assert len(restarted.get("/api/jobs").json()) == 2
        assert restarted.post("/api/jobs/" + original["id"] + "/refinements",
                              json={"change": "  "}).status_code == 422
        assert compiler.BRIEF.validate_json(Storage(tmp_path).get(refined["id"])["brief_json"]).seconds == 8


def test_explicit_worker_failures_and_restart_recovery(tmp_path, monkeypatch):
    def unavailable(job, cfg, **kwargs):
        raise BackendUnavailable("model is offline")

    monkeypatch.setattr(compiler, "run", unavailable)
    with client_for(tmp_path) as client:
        response = client.post("/api/jobs", json={"intent": "fox"})
        job = finished(client, response.json()["id"])
        assert job["status"] == "failed"
        assert job["result"] is None
        assert job["error"]["code"] == "llm-unavailable"
        assert job["error"]["message"] == "model is offline"
        assert client.post("/api/jobs/" + job["id"] + "/refinements",
                           json={"change": "darker"}).status_code == 409
        assert client.get("/api/health").json()["busy"] is False
        stored = Storage(tmp_path).get(job["id"])
        stored["status"] = "running"
        Storage(tmp_path).save(stored)
    with client_for(tmp_path) as restarted:
        job = restarted.get("/api/jobs/" + job["id"]).json()
        assert job["status"] == "interrupted"
        assert job["error"]["code"] == "service-restarted"


def test_adapter_forwards_options_and_rejects_invalid_document(tmp_path, monkeypatch):
    seen = []

    def invalid(brief, **options):
        seen.append((brief, options))
        return SimpleNamespace(ok=False, errors=[SimpleNamespace(rule="T1", msg="missing")])

    monkeypatch.setattr(compiler, "compile_brief", invalid)
    with client_for(tmp_path):
        job = {"id": "test", "version": 1, "request": BriefIn(
            intent="fox", effort="max", seed=41, transcripts={"b" * 64: "keep me"}).model_dump()}
        try:
            compiler.run(job, get_config())
        except HTTPException as exc:
            assert exc.status_code == 422
            assert exc.detail["code"] == "invalid-ir"
        else:
            raise AssertionError("an invalid IR was delivered")
        assert seen[0][1]["thinking_prose"] is True
        assert seen[0][1]["seed"] == 41
        assert seen[0][1]["transcripts"] == {"b" * 64: "keep me"}
        assert isinstance(seen[0][1]["backend"], Backend)
        assert isinstance(seen[0][0], Brief)
        assert compiler.BRIEF.validate_json(compiler.BRIEF.dump_json(
            Brief("fox", dialogue=[DialogueLine("Exact.")]))).dialogue[0].text == "Exact."
