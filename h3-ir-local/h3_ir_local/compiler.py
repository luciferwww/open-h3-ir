"""The only adapter to compiler internals, deliberately pinned to open-h3-ir 0.4.1."""
from __future__ import annotations

from dataclasses import replace
from inspect import signature
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import TypeAdapter

from h3ir import service
from h3ir.backend import Backend
from h3ir.compile import compile_brief, refine
from h3ir.config import Config
from h3ir.models import Brief
from h3ir.plan import ProfileOptions

from .archive import AssetArchive, digests

BRIEF = TypeAdapter(Brief)
SUPPORTS_PROGRESS = "progress" in signature(compile_brief).parameters


def prepare(body: service.BriefIn, archive: AssetArchive | None = None, *,
            source: AssetArchive | None = None) -> Brief:
    for asset in body.assets:
        if (asset.path or asset.url or asset.paired_video_path or not asset.sha256):
            raise HTTPException(422, detail={
                "code": "uploads-only", "message": "本地工作台只接收上传素材，不接受文件路径或 URL。"})
    if archive is not None:
        for digest in digests(body):
            archive.retain(digest, source=source)
        archive.restore_uploads(body)
    brief = service._to_brief(body)
    if archive is not None:
        brief = bind_archive(brief, archive)
    return brief


def bind_archive(brief: Brief, archive: AssetArchive) -> Brief:
    assets = []
    for asset in brief.assets:
        path = archive.retain(asset.sha256)
        assets.append(replace(asset, path=str(path)))
    return replace(brief, assets=assets)


def effective_request(request: dict[str, Any], context: str) -> dict[str, Any]:
    brief = BRIEF.validate_json(context)
    return {**request, **{key: getattr(brief, key)
                         for key in ("intent", "seconds", "shots", "constraints")}}


def run(job: dict[str, Any], cfg: Config, *,
        progress: Callable[[str, str], None] | None = None) -> tuple[dict[str, Any], str]:
    if progress is not None:
        progress("preparing", "Preparing the request and uploaded references")
    body = service.BriefIn.model_validate(job["request"])
    archive = AssetArchive(cfg.paths.state_dir.parent, job["id"])
    context = job.get("brief_json")
    brief = (bind_archive(BRIEF.validate_json(context), archive) if context
             else prepare(body, archive))
    opts = ProfileOptions(name=cfg.profile)
    with Backend(cfg) as backend:
        options = {"backend": backend, "opts": opts, "seed": body.seed,
                   "thinking_prose": body.effort == "max", "transcripts": dict(body.transcripts)}
        if progress is not None and SUPPORTS_PROGRESS:
            options["progress"] = progress
        elif progress is not None:
            progress("compiling", "Installed compiler has no stage callbacks; compiling without detailed progress")
        if job.get("change"):
            doc, brief, changed = refine(brief, job["change"], **options)
        else:
            doc = compile_brief(brief, **options)
            changed = []
    if progress is not None:
        progress("result", "Checking the final document and assembling the result")
    if not doc.ok:
        raise HTTPException(422, detail={
            "code": "invalid-ir", "message": "编译结果未通过校验，未作为有效结果交付。",
            "findings": [{"rule": f.rule, "message": f.msg} for f in doc.errors]})
    envelope = service._envelope(job["id"], doc, brief)
    envelope.update(version=job["version"], changed=changed)
    return envelope, BRIEF.dump_json(brief).decode("utf-8")


def probe(cfg: Config) -> dict[str, Any]:
    with Backend(replace(cfg, llm=replace(cfg.llm, timeout_s=15))) as backend:
        backend.require_available()
        return {"ok": True, "model": backend.model_id(),
                "message": "端点可达，模型已解析；这不代表已验证视觉能力。"}
