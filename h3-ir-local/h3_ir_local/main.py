from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path, PureWindowsPath
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from h3ir import contract, service
from h3ir.analyse import AssetAnalysisError, ToolMissing
from h3ir.backend import BackendError, BackendUnavailable
from h3ir.compile import BriefRefused
from h3ir.config import Config, get_config, set_config

from . import compiler
from .archive import AssetArchive, remove_record
from .config import (CORE_VERSION, HISTORY_LIMIT, HOST, PORT, SSE_HEARTBEAT_SECONDS, VUE_URL,
                     ModelSettings, data_directory)
from .storage import Storage

log = logging.getLogger("h3_ir_local")


class Refinement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change: str = Field(min_length=1)


class JobIn(service.BriefIn):
    asset_names: list[str | None] = Field(default_factory=list)
    source_job_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def filenames(self) -> "JobIn":
        if self.asset_names and len(self.asset_names) != len(self.assets):
            raise ValueError("asset_names 必须与 assets 一一对应。")
        for name in self.asset_names:
            if name is None:
                continue
            if (not name or len(name) > 255 or PureWindowsPath(name).name != name
                    or any(ord(char) < 32 for char in name)):
                raise ValueError("附件名称必须是不包含路径或控制字符的文件名。")
        return self


def public_job(job: dict[str, Any], directory: Path | None = None) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if key != "brief_json"}
    if "progress" in result:
        result["progress"] = [dict(step) for step in result["progress"]]
    if directory is not None:
        archive = AssetArchive(directory, job["id"])
        result["attachments"] = [
            {**asset, "available": archive.path(asset["sha256"]).is_file()}
            for asset in job.get("attachments", [])]
    return result


class Workspace:
    def __init__(self, directory: Path):
        installed = version("open-h3-ir")
        if installed != CORE_VERSION:
            raise RuntimeError(f"Expected open-h3-ir=={CORE_VERSION}, found {installed}.")
        self.storage = Storage(directory)
        self.directory = directory
        self.storage.interrupt_unfinished()
        self.lock = threading.Lock()
        self.loop = asyncio.get_running_loop()
        self.subscribers: set[asyncio.Event] = set()
        self.subscriber_lock = threading.Lock()
        self.busy = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="h3-compiler")
        base = Config()
        self.cfg = replace(base, paths=replace(base.paths, state_dir=directory / "core"),
                           assets=replace(base.assets, allow_paths=False))
        saved = self.storage.settings()
        if saved:
            self.cfg = self.updated_config(ModelSettings(**saved))
        set_config(self.cfg)

    def updated_config(self, settings: ModelSettings) -> Config:
        key = self.cfg.llm.api_key if settings.api_key is None else settings.api_key
        return replace(self.cfg, llm=replace(self.cfg.llm, base_url=settings.base_url,
                                             model=settings.model, api_key=key,
                                             timeout_s=settings.timeout_s))

    def settings(self) -> dict[str, Any]:
        llm = self.cfg.llm
        return {"base_url": llm.base_url, "model": llm.model, "timeout_s": llm.timeout_s,
                "has_api_key": bool(llm.api_key and llm.api_key != "not-needed"),
                "busy": self.busy}

    def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        with self.subscriber_lock:
            self.subscribers.add(event)
        return event

    def unsubscribe(self, event: asyncio.Event) -> None:
        with self.subscriber_lock:
            self.subscribers.discard(event)

    def notify(self) -> None:
        with self.subscriber_lock:
            for event in self.subscribers:
                self.loop.call_soon_threadsafe(event.set)

    def configure(self, settings: ModelSettings) -> dict[str, Any]:
        with self.lock:
            if self.busy:
                raise HTTPException(409, detail={"code": "compiler-busy",
                                                 "message": "任务运行中，不能修改模型配置。"})
            cfg = self.updated_config(settings)
            self.storage.save_settings(settings.model_dump(exclude={"api_key"}))
            self.cfg = cfg
            set_config(cfg)
            return self.settings()

    def submit(self, body: service.BriefIn, parent: dict[str, Any] | None = None,
               change: str | None = None, asset_names: list[str | None] | None = None,
               source_job_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            if self.busy:
                raise HTTPException(409, detail={"code": "compiler-busy",
                                                 "message": "已有编译任务运行中，请等待完成。"})
            source_id = parent["id"] if parent else source_job_id
            if source_id is not None and self.storage.get(source_id) is None:
                raise HTTPException(404, detail={
                    "code": "unknown-source-job", "message": "附件来源创作已删除，请重新上传附件。"})
            source = AssetArchive(self.directory, source_id) if source_id is not None else None
            job_id = uuid.uuid4().hex
            archive = AssetArchive(self.directory, job_id)
            names = asset_names or []
            if parent:
                names = [a["name"] if a["original_name"] else None for a in parent["attachments"]]
            try:
                attachments = archive.manifest(body, names, source=source)
                if parent is None:
                    brief = compiler.prepare(body, archive)
                else:
                    brief = compiler.bind_archive(compiler.BRIEF.validate_json(parent["brief_json"]),
                                                  archive)
            except Exception:
                remove_record(self.directory, job_id)
                raise
            job = {
                "id": job_id, "parent_id": parent["id"] if parent else None,
                "version": parent["version"] + 1 if parent else 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "queued", "request": body.model_dump(mode="json"),
                "revision": 1,
                "progress": [{"stage": "queued", "message": "Waiting for the compiler",
                              "elapsed_s": 0, "at": datetime.now(timezone.utc).isoformat()}],
                "change": change, "brief_json": compiler.BRIEF.dump_json(brief).decode("utf-8"),
                "attachments": attachments,
                "result": None, "error": None,
            }
            try:
                self.storage.save(job)
            except Exception:
                remove_record(self.directory, job_id)
                raise
            self.busy = True
            self.notify()
            response = public_job(job, self.directory)
            self.executor.submit(self.execute, job, self.cfg)
            return response

    def execute(self, job: dict[str, Any], cfg: Config) -> None:
        started = time.monotonic()

        def progress(stage: str, message: str) -> None:
            job["progress"].append({"stage": stage, "message": message,
                                    "elapsed_s": round(time.monotonic() - started, 2),
                                    "at": datetime.now(timezone.utc).isoformat()})
            job["revision"] += 1
            self.storage.save(job)
            self.notify()

        try:
            job["status"] = "running"
            progress("starting", "Starting the compiler")
            result, context = compiler.run(job, cfg, progress=progress)
            job.update(status="completed", result=result, brief_json=context,
                       request=compiler.effective_request(job["request"], context))
        except HTTPException as exc:
            job.update(status="failed", error={"http_status": exc.status_code,
                                              "detail": exc.detail})
        except (BriefRefused, BackendError, AssetAnalysisError) as exc:
            code, status = "compile-failed", 422
            if isinstance(exc, BriefRefused):
                code = exc.code
            elif isinstance(exc, ToolMissing):
                code, status = "analysis-tool-missing", 503
            elif isinstance(exc, AssetAnalysisError):
                code = "asset-unreadable"
            elif isinstance(exc, BackendUnavailable):
                code, status = "llm-unavailable", 503
            elif isinstance(exc, BackendError):
                code, status = "llm-error", 502
            job.update(status="failed", error={"code": code, "http_status": status,
                                              "message": str(exc)})
            log.warning("Job %s failed: %s", job["id"], code)
        except Exception:
            log.exception("Unexpected compiler failure for job %s", job["id"])
            job.update(status="failed", error={
                "code": "internal-error", "message": "编译发生内部错误，请查看服务终端日志。"})
        finally:
            try:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["progress"].append({
                    "stage": job["status"],
                    "message": "Compilation completed" if job["status"] == "completed" else "Compilation failed",
                    "elapsed_s": round(time.monotonic() - started, 2), "at": job["finished_at"]})
                job["revision"] += 1
                self.storage.save(job)
            except (OSError, ValueError, TypeError):
                log.exception("Could not persist final status for job %s", job["id"])
                raise
            finally:
                with self.lock:
                    self.busy = False
                self.notify()


def create_app(directory: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        previous = get_config()
        workspace = None
        try:
            workspace = Workspace(directory or data_directory())
            app.state.workspace = workspace
            yield
        finally:
            if workspace is not None:
                workspace.executor.shutdown(wait=True)
            set_config(previous)

    app = FastAPI(title="H3-IR Local", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.add_exception_handler(RequestValidationError, service.unreadable_request)

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-h3-local") != "1":
                return JSONResponse(status_code=403, content={
                    "detail": {"code": "local-header-required", "message": "需要 X-H3-Local: 1 请求头。"}})
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse(status_code=403, content={
                    "detail": {"code": "origin-refused", "message": "拒绝非同源请求。"}})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.error("Request failed: %s", request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={
            "detail": {"code": "internal-error", "message": "服务内部错误，请查看终端日志。"}})

    def ws() -> Workspace:
        return app.state.workspace

    def lookup(job_id: str) -> dict[str, Any]:
        job = ws().storage.get(job_id)
        if job is None:
            raise HTTPException(404, detail={"code": "unknown-job", "message": "任务不存在。"})
        return job

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(Path(__file__).with_name("index.html").read_text(encoding="utf-8")
                            .replace("__VUE_URL__", VUE_URL))

    @app.get("/api/health")
    def health():
        return {"ok": True, "compiler_version": version("open-h3-ir"),
                "busy": ws().busy, "ffmpeg": bool(shutil.which("ffmpeg")),
                "ffprobe": bool(shutil.which("ffprobe"))}

    @app.get("/api/bootstrap")
    def bootstrap():
        return {"contract": contract.contract(),
                "capabilities": {**service.capabilities(), "history_limit": HISTORY_LIMIT},
                "settings": ws().settings()}

    @app.get("/api/settings")
    def settings():
        return ws().settings()

    @app.put("/api/settings")
    def save_settings(body: ModelSettings):
        return ws().configure(body)

    @app.post("/api/probe")
    def probe():
        try:
            return compiler.probe(ws().cfg)
        except BackendError as exc:
            raise HTTPException(503, detail={"code": "llm-unavailable",
                                             "message": str(exc)}) from exc

    @app.put("/api/assets/{digest}")
    async def upload(digest: str, request: Request):
        return await service.put_asset(digest, request)

    @app.get("/api/jobs")
    def history():
        return [{"id": j["id"], "parent_id": j["parent_id"], "version": j["version"],
                 "created_at": j["created_at"], "status": j["status"],
                 "intent": j["request"]["intent"], "change": j.get("change")}
                for j in ws().storage.recent(limit=None)]

    @app.post("/api/jobs", status_code=202)
    def create(body: JobIn):
        request = service.BriefIn.model_validate(body.model_dump(exclude={"asset_names", "source_job_id"}))
        return ws().submit(request, asset_names=body.asset_names, source_job_id=body.source_job_id)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return public_job(lookup(job_id), ws().directory)

    @app.get("/api/jobs/{job_id}/assets/{index}")
    def historical_asset(job_id: str, index: int, download: bool = False):
        job = lookup(job_id)
        assets = job.get("attachments", [])
        if index < 0 or index >= len(assets):
            raise HTTPException(404, detail={"code": "unknown-attachment", "message": "附件不存在。"})
        asset = assets[index]
        path = AssetArchive(ws().directory, job_id).path(asset["sha256"])
        if not path.is_file():
            raise HTTPException(410, detail={
                "code": "historical-asset-missing", "message": "历史附件已缺失，请重新上传原文件。"})
        media_type = mimetypes.guess_type(asset["name"])[0] or "application/octet-stream"
        if not media_type.startswith(asset["kind"] + "/") or media_type == "image/svg+xml":
            media_type = "application/octet-stream"
        return FileResponse(path, filename=asset["name"], media_type=media_type,
                            content_disposition_type=(
                                "attachment" if download or media_type == "application/octet-stream" else "inline"))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        workspace = ws()
        with workspace.lock:
            job = lookup(job_id)
            if job["status"] in ("queued", "running"):
                raise HTTPException(409, detail={
                    "code": "job-active", "message": "正在编译或等待中的任务不能删除，请等待任务结束。"})
            if not workspace.storage.delete(job_id):
                raise HTTPException(404, detail={"code": "unknown-job", "message": "任务不存在。"})
        workspace.notify()
        return {"id": job_id, "deleted": True}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request):
        lookup(job_id)
        workspace = ws()

        async def events():
            changed = workspace.subscribe()
            revision = -1
            try:
                yield "retry: 2000\n\n"
                while not await request.is_disconnected():
                    changed.clear()
                    job = await run_in_threadpool(workspace.storage.get, job_id)
                    if job is None:
                        data = json.dumps({"id": job_id, "status": "deleted"})
                        yield f"event: job\ndata: {data}\n\n"
                        return
                    current = job.get("revision", 0)
                    if current != revision:
                        data = json.dumps(public_job(job, workspace.directory), ensure_ascii=False)
                        yield f"id: {current}\nevent: job\ndata: {data}\n\n"
                        revision = current
                    if job["status"] not in ("queued", "running"):
                        return
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                workspace.unsubscribe(changed)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    @app.post("/api/jobs/{job_id}/refinements", status_code=202)
    def refine_job(job_id: str, body: Refinement):
        parent = lookup(job_id)
        if parent["status"] != "completed":
            raise HTTPException(409, detail={"code": "job-not-completed",
                                             "message": "只能修改已成功完成的任务。"})
        if not body.change.strip():
            raise HTTPException(422, detail={"code": "change-empty", "message": "修改说明不能为空。"})
        return ws().submit(service.BriefIn.model_validate(parent["request"]),
                           parent=parent, change=body.change)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="H3-IR Local")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=HOST, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
