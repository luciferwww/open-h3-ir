from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archive import record_directory, remove_record, valid_record_id
from .config import HISTORY_LIMIT


def _write_json(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=path.stem + "-", suffix=".tmp", delete=False) as outgoing:
            temporary = Path(outgoing.name)
            outgoing.write(text)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as incoming:
            data = json.load(incoming)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return data


class Storage:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.lock = threading.RLock()

    def save(self, job: dict[str, Any]) -> None:
        with self.lock:
            path = record_directory(self.directory, job["id"]) / "record.json"
            is_new = not path.is_file()
            _write_json(path, job)
            if is_new or job["status"] not in ("queued", "running"):
                self._prune()

    def _prune(self) -> None:
        for job in self.recent(limit=None)[HISTORY_LIMIT:]:
            if job["status"] not in ("queued", "running"):
                self.delete(job["id"])

    def get(self, job_id: str) -> dict[str, Any] | None:
        if not valid_record_id(job_id):
            return None
        with self.lock:
            path = record_directory(self.directory, job_id) / "record.json"
            job = _read_json(path)
            if job is not None and (
                    job.get("id") != job_id or not isinstance(job.get("created_at"), str)
                    or not isinstance(job.get("status"), str)):
                raise ValueError(f"Invalid creation record in {path}.")
            return job

    def delete(self, job_id: str) -> bool:
        with self.lock:
            job = self.get(job_id)
            if job is None:
                return False
            if job["status"] in ("queued", "running"):
                raise ValueError("Active creations cannot be deleted.")
            remove_record(self.directory, job_id)
            return True

    def recent(self, limit: int | None = HISTORY_LIMIT) -> list[dict[str, Any]]:
        with self.lock:
            jobs = []
            for path in (self.directory / "creations").glob("*/record.json"):
                if not valid_record_id(path.parent.name):
                    raise ValueError(f"Invalid creation directory: {path.parent}.")
                job = self.get(path.parent.name)
                if job is not None:
                    jobs.append(job)
            jobs.sort(key=lambda job: (job["created_at"], job["id"]), reverse=True)
            return jobs if limit is None or limit < 0 else jobs[:limit]

    def interrupt_unfinished(self) -> None:
        with self.lock:
            for job in self.recent(limit=None):
                if job["status"] not in ("queued", "running"):
                    continue
                job["status"] = "interrupted"
                job["revision"] = job.get("revision", 0) + 1
                steps = job.setdefault("progress", [])
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                steps.append({
                    "stage": "interrupted", "message": "Service restarted before completion",
                    "elapsed_s": steps[-1]["elapsed_s"] if steps else 0,
                    "at": job["finished_at"]})
                job["error"] = {"code": "service-restarted",
                                "message": "服务在任务完成前停止。请重新提交，原请求已保留。"}
                self.save(job)
            self._prune()

    def settings(self) -> dict[str, Any] | None:
        with self.lock:
            return _read_json(self.directory / "settings.json")

    def save_settings(self, data: dict[str, Any]) -> None:
        with self.lock:
            _write_json(self.directory / "settings.json", data)
