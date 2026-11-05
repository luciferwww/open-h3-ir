from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from h3ir import service, uploads


def digests(body: service.BriefIn) -> list[str]:
    return list(dict.fromkeys(
        digest for asset in body.assets
        for digest in (asset.sha256, asset.paired_video_sha256) if digest))


def valid_record_id(job_id: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]+", job_id) is not None


def record_directory(directory: Path, job_id: str) -> Path:
    if not valid_record_id(job_id):
        raise ValueError("Invalid creation ID.")
    root = directory / "creations"
    target = root / job_id
    if root.resolve().parent != directory.resolve() or target.resolve().parent != root.resolve():
        raise ValueError("Creation directory must remain inside the workspace.")
    return target


def remove_record(directory: Path, job_id: str) -> None:
    target = record_directory(directory, job_id)
    if target.exists():
        record = target / "record.json"
        for child in target.iterdir():
            if child == record:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        record.unlink(missing_ok=True)
        target.rmdir()


class AssetArchive:
    def __init__(self, directory: Path, job_id: str):
        self.directory = record_directory(directory, job_id)
        self.root = self.directory / "attachments"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, digest: str) -> Path:
        if not uploads.is_digest(digest):
            raise HTTPException(422, detail={
                "code": "asset-name-not-a-digest", "message": "附件名称必须是有效的 SHA-256。"})
        return self.root / digest

    def retain(self, digest: str, *, source: AssetArchive | None = None) -> Path:
        target = self.path(digest)
        if target.is_file():
            return target
        incoming_path = source.path(digest) if source is not None else None
        if incoming_path is None or not incoming_path.is_file():
            incoming_path = uploads.stored(digest)
        if incoming_path is None:
            raise HTTPException(422, detail={
                "code": "asset-not-uploaded", "message": "附件不在来源创作或上传缓存中，请重新上传。",
                "missing": [digest]})
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with incoming_path.open("rb") as incoming, tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix="archive-", suffix=".part", delete=False) as outgoing:
                temporary = Path(outgoing.name)
                checksum = hashlib.sha256()
                while chunk := incoming.read(1024 * 1024):
                    checksum.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if checksum.hexdigest() != digest:
                raise HTTPException(422, detail={
                    "code": "asset-digest-mismatch", "message": "附件内容与 SHA-256 不一致，未写入历史归档。"})
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target

    def restore_uploads(self, body: service.BriefIn) -> None:
        for digest in digests(body):
            source = self.path(digest)
            if uploads.stored(digest) is not None or not source.is_file():
                continue
            try:
                # The core converter accepts uploads only; rehydrate through its bounded,
                # hash-checking receiver, then compilation reads the durable archive instead.
                with source.open("rb") as incoming, uploads.receiver(
                        digest, declared=source.stat().st_size) as receiver:
                    while chunk := incoming.read(1024 * 1024):
                        receiver.write(chunk)
                    receiver.finish()
            except uploads.TooLarge as exc:
                raise HTTPException(413, detail={
                    "code": "asset-too-large", "message": str(exc)}) from exc
            except uploads.StoreFull as exc:
                raise HTTPException(507, detail={
                    "code": "upload-store-full", "message": str(exc)}) from exc
            except uploads.DigestMismatch as exc:
                raise HTTPException(422, detail={
                    "code": "asset-digest-mismatch", "message": str(exc)}) from exc

    def manifest(self, body: service.BriefIn, names: list[str | None], *,
                 source: AssetArchive | None = None) -> list[dict[str, Any]]:
        for digest in digests(body):
            self.retain(digest, source=source)
        entries = []
        for index, asset in enumerate(body.assets):
            path = self.path(asset.sha256 or "")
            available = path.is_file()
            entries.append({
                "index": index, "sha256": asset.sha256, "kind": asset.kind,
                "name": names[index] if names and names[index] else asset.sha256,
                "original_name": bool(names and names[index]), "bytes": path.stat().st_size if available else None,
                "available": available})
        return entries
