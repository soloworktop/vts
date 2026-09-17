import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.background import BackgroundTask

from .. import db
from ..version import get_version

# 插件挂载点：导入期完成发现与加载。未安装插件时 0 条 = 核心即完整产品；
# 有条目但加载失败会抛 PluginLoadError 使本模块导入失败 → uvicorn 拒绝启动
# （fail-closed，绝不静默降级）。
from . import hooks
from .capabilities import current_capabilities
from .llm_store import import_from_env, llm_config, save_llm_config
from .settings_store import job_defaults, job_defaults_payload, set_job_defaults
from .tasks import (
    backfill_titles_from_outputs,
    cancel_job,
    count_jobs,
    create_job,
    delete_job,
    enqueue_job,
    get_job,
    list_jobs,
    output_base,
    resume_pending_jobs,
    retry_job,
    sweep_orphan_uploads,
    upload_base,
)
from ..summarizers.openai import SUMMARY_TEMPLATES
from .template_store import delete_template, get_template, list_templates, save_template
from .label_store import (
    LabelStoreError,
    create_label,
    delete_label,
    get_job_labels,
    list_labels,
    merge_labels,
    rename_label,
    set_job_labels,
)
from ..utils import configure_logging, subprocess_no_window_kwargs

logger = logging.getLogger("video_to_summary.web")

#: canonical API 前缀（D11）。客户端一律使用 /api/v1。
API_PREFIX = "/api/v1"
# 过渡别名 /api/* 已于 M1 删除（React 前端全部走 /api/v1）。

#: 静态目录覆盖点环境变量：设置且有效（指向含 index.html 的目录）时，首页 `/` 与
#: `/static/*` 改由该目录提供；未设置或无效时回落到包内 web/static/（见 _static_dir）。
STATIC_DIR_ENV = "VTS_STATIC_DIR"

#: 无效 VTS_STATIC_DIR 的回落 warning 只打一次（避免每个静态请求都刷屏日志）
_static_dir_fallback_warned = False

# 插件发现必须在任何路由注册之前完成（fail-closed：宁可服务起不来，也不带着残缺插件状态起服务）
hooks.load_plugins()


class JobPayload(BaseModel):
    source_type: str = "url"
    url: str | None = None
    audio_path: str | None = None
    title: str | None = None
    # 组织元数据（非处理配置）：与 title 同级，创建时可填标签
    labels: list[str] | None = None
    # 唯一允许的单任务处理配置：总结模板（新建任务表单可选；其余配置仍只来自全局「任务默认」）。
    # 空/缺省 = 跟随全局默认；运行时 payload 即配置（_build_settings），重试时保留该值。
    summary_template: str | None = None

    # 其余配置字段（whisper_api/llm_*/polish_*/asr_* 等）
    # 与 config.Settings 字段对齐：统一由 Settings.from_mapping 消费与归一，
    # 避免在请求模型里再维护一份 20+ 字段的声明。
    model_config = ConfigDict(extra="allow")


class JobResponse(BaseModel):
    job_id: str
    status: str


def _auth_token() -> str:
    """Web 控制台可选鉴权口令：设置 VIDEO_TO_SUMMARY_TOKEN 后，所有 /api/* 需携带。"""
    return os.environ.get("VIDEO_TO_SUMMARY_TOKEN", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("web console starting (db=%s)", os.environ.get("VIDEO_TO_SUMMARY_DB", "default"))
    try:
        # 进程重启后恢复未完成的任务
        resume_pending_jobs()
    except Exception:  # noqa: BLE001 - 启动恢复失败不阻塞服务
        logger.exception("failed to resume pending jobs on startup")
    try:
        # 历史数据修复：把「URL 当标题」的旧任务用产物 summary 的真实标题回填
        backfill_titles_from_outputs()
    except Exception:  # noqa: BLE001 - 回填失败不阻塞服务
        logger.exception("failed to backfill job titles on startup")
    try:
        # 全文检索索引补漏：为缺失 jobs_fts 行的 completed 任务重建索引
        # （v8 升级存量 / 索引失败残留；幂等，产物缺失的任务安全跳过）
        from .fts_index import backfill_missing_index

        backfill_missing_index()
    except Exception:  # noqa: BLE001 - 索引补漏失败不阻塞服务（检索自动降级）
        logger.exception("failed to backfill fts index on startup")
    try:
        # 孤儿上传清扫：进程在「上传落盘后、建任务完成前」崩溃残留的
        # uploads/<uuid>/ 目录（引用判定失败时自动放弃，绝不误删）
        sweep_orphan_uploads()
    except Exception:  # noqa: BLE001 - 清扫失败不阻塞服务
        logger.exception("failed to sweep orphan uploads on startup")
    yield


app = FastAPI(title="VTS web console", lifespan=lifespan)

# 仅允许本地同源页面访问；其余来源一律拒绝，降低 CSRF / 跨站读取风险
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


def _auth_header_ok(auth: str, x_token: str, token: str) -> bool:
    """鉴权头比较：编码为 bytes 再做常量时间比较。

    hmac.compare_digest 对含非 ASCII 的 str 会抛 TypeError（恶意/异常请求会变成
    500 而非 401），因此统一 encode（errors=replace 容错）后再比较。
    """
    return hmac.compare_digest(
        auth.encode("utf-8", errors="replace"), f"Bearer {token}".encode("utf-8")
    ) or hmac.compare_digest(
        x_token.encode("utf-8", errors="replace"), token.encode("utf-8")
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """可选鉴权 + 本地接口的跨站写防护。

    - 设置 VIDEO_TO_SUMMARY_TOKEN 后，/api/* 需携带 Bearer Token 或 X-Auth-Token
      （hmac.compare_digest 常量时间比较，避免时序侧信道）。
    - 写方法（POST/PUT/DELETE/PATCH）带 Origin 头时必须与 Host 同源：CORS 只防
      跨站「读」，防不了跨站页面用无 Content-Type 的简单 POST 打 /jobs、/cancel
      等接口。浏览器同源请求的 Origin 与 Host 恒一致；curl/CLI 不带 Origin 不受
      影响。默认未设置 Token 时保持本地回环可用（兼容旧行为），文档已明确暴露到
      局域网/公网前必须配置。
    """
    token = _auth_token()
    if request.url.path.startswith("/api/"):
        if (
            request.method in {"POST", "PUT", "DELETE", "PATCH"}
            and not _origin_same_as_host(request)
        ):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        if token and not _auth_header_ok(
            request.headers.get("Authorization", ""),
            request.headers.get("X-Auth-Token", ""),
            token,
        ):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


def _origin_same_as_host(request: Request) -> bool:
    """带 Origin 头的请求必须与 Host 同源；不带 Origin 的非浏览器客户端恒放行。"""
    origin = request.headers.get("Origin", "")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    try:
        origin_netloc = (urlsplit(origin).netloc or "").lower()
    except ValueError:
        return False
    return origin_netloc == host.lower()


def _static_dir() -> Path:
    """生效的 Web 静态资源目录（首页 index.html 与 /static/* 共用同一目录）。

    优先级：``VTS_STATIC_DIR`` 指向的目录（必须是**含 ``index.html`` 的目录**）
    → 包内 ``web/static/``（与未设置时行为完全一致）。

    环境变量设置了但无效（不存在 / 不是目录 / 缺 index.html）时**回落**到包内
    目录并打一次 warning——自部署用户配错路径时不应白屏，也不应误判成
    「前端未构建」。
    """
    global _static_dir_fallback_warned
    override = os.environ.get(STATIC_DIR_ENV, "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
        if not _static_dir_fallback_warned:
            _static_dir_fallback_warned = True
            logger.warning(
                "VTS_STATIC_DIR=%r 不是含 index.html 的有效目录，回落到包内 web/static/",
                override,
            )
    return Path(__file__).with_name("static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_path = _static_dir() / "index.html"
    if index_path.is_file():
        return index_path.read_text(encoding="utf-8")
    # 前端未构建（开发态直接跑服务 / 源码检出未执行 pnpm build）：给一页清晰提示，
    # 而不是 500。构建产物不入库，见 web-src/README.md。
    return _frontend_not_built_html()





def _frontend_not_built_html() -> str:
    """未构建占位页：指引用户运行 pnpm install && pnpm build（或开发态 pnpm dev）。"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>VTS 控制台 · 前端未构建</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #f3efe9; color: #3c3834; }
  .card { background: #fff; border-radius: 16px; padding: 40px 48px; max-width: 560px;
          box-shadow: 0 12px 40px rgba(0,0,0,.12); text-align: center; }
  h1 { font-size: 20px; margin: 0 0 12px; }
  p { line-height: 1.7; margin: 8px 0; color: #6b645c; }
  code { background: #f2ede6; padding: 2px 8px; border-radius: 6px; font-size: 14px; }
  .cmd { margin: 16px 0; }
  .cmd div { background: #2d2a26; color: #f5efe7; border-radius: 10px; padding: 12px 16px;
             font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; margin-top: 6px;
             text-align: left; word-break: break-all; }
</style>
</head>
<body>
  <div class="card">
    <h1>🚧 前端尚未构建</h1>
    <p>当前运行的是 VTS 后端服务，但 <code>web/static/</code> 下没有前端构建产物。</p>
    <div class="cmd">
      <p>生产态（构建前端到 <code>web/static/</code>）：</p>
      <div>cd web-src && pnpm install && pnpm build</div>
    </div>
    <div class="cmd">
      <p>开发态（Vite dev server，代理 /api 到本服务）：</p>
      <div>cd web-src && pnpm dev</div>
    </div>
    <p>详见 <code>web-src/README.md</code> 与 <code>scripts/web.sh</code>。</p>
  </div>
</body>
</html>
"""


def _guide_html_path() -> Path | None:
    """使用手册（HTML 版）路径：包内 ``web/static/user-guide.html``。

    本构建自带一页极简说明（不依赖仓库磁盘上的 docs/）；文件缺失返回 None（404）。
    """
    bundled = Path(__file__).with_name("static") / "user-guide.html"
    return bundled if bundled.is_file() else None


@app.get("/guide")
async def user_guide_page() -> Response:
    """使用手册：侧栏「使用指引」与浏览器 /guide 直达共用同一入口。"""
    guide_path = _guide_html_path()
    if guide_path is None:
        raise HTTPException(status_code=404, detail="使用手册缺失")
    return FileResponse(guide_path, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


class _RevalidateStaticFiles(StaticFiles):
    """静态资源可缓存但每次必须 revalidate（Cache-Control: no-cache + ETag/304）。

    本地单机形态零成本：文件未变走 304，变更即时下发新内容——
    否则浏览器启发式缓存会让前端更新后必须手动强刷才能看到。

    服务目录**每次请求按 ``_static_dir()`` 解析**（VTS_STATIC_DIR 覆盖点），而不是
    挂载期固化。lookup_path 在 worker 线程里执行，不能直接改写共享的
    ``self.all_directories``（并发请求会互相覆盖），故本地解析后复用同一套
    防路径逃逸 / 真实文件校验语义。
    """

    def lookup_path(self, path: str):
        directory = str(_static_dir())
        if path.startswith(("/", "\\")):
            return "", None
        joined_path = os.path.join(directory, path)
        if self.follow_symlink:
            full_path = os.path.abspath(joined_path)
            directory = os.path.abspath(directory)
        else:
            full_path = os.path.realpath(joined_path)
            directory = os.path.realpath(directory)
        if os.path.commonpath([full_path, directory]) != str(directory):
            # 不放过把路径带出服务目录的畸形请求
            return "", None
        try:
            return full_path, os.stat(full_path)
        except (FileNotFoundError, NotADirectoryError):
            return "", None

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidateStaticFiles(directory=Path(__file__).with_name("static")), name="web-static")


# ============ 版本化路由注册：canonical /api/v1（过渡别名已于 M1 删除） ============

def _register(path: str, methods: list[str], **kwargs):
    def decorator(func):
        app.api_route(f"{API_PREFIX}{path}", methods=methods, **kwargs)(func)
        return func

    return decorator


def api_route(path: str, *, methods: list[str], **kwargs):
    """注册 GET/HEAD 等多方法端点（版本化 + 过渡别名）。"""
    return _register(path, methods, **kwargs)


def api_get(path: str, **kwargs):
    return _register(path, ["GET"], **kwargs)


def api_post(path: str, **kwargs):
    return _register(path, ["POST"], **kwargs)


def api_put(path: str, **kwargs):
    return _register(path, ["PUT"], **kwargs)


def api_delete(path: str, **kwargs):
    return _register(path, ["DELETE"], **kwargs)


# ============ 能力声明 ============

@api_get("/capabilities")
async def capabilities_api() -> JSONResponse:
    """能力集合：核心不预置任何能力位，返回插件经 declare_capabilities 声明的聚合。

    前端据此显示/隐藏对应的能力相关 UI。
    """
    return JSONResponse(current_capabilities())


# ============ 任务 ============

def _create_job_from_values(
    *,
    source_type: str,
    url: str | None = None,
    audio_path: str | None = None,
    title: str | None = None,
    labels: list[str] | None = None,
    summary_template: str | None = None,
) -> JobResponse:
    """建任务核心：POST /jobs 与 POST /jobs/upload 共用。

    处理配置只来自全局默认；调用方提供源/标题/标签 + 唯一的单任务配置「总结模板」。
    """
    job_payload = job_defaults_payload()
    for key, value in (
        ("source_type", source_type),
        ("url", url),
        ("audio_path", audio_path),
        ("title", title),
        ("labels", labels),
    ):
        if value is not None:
            job_payload[key] = value
    # 单任务总结模板：显式选择时校验存在性（内置 + 自定义 + 兼容别名），覆盖全局默认
    # 写入 payload（别名归一化为现名，如历史名 "default" → "通用"）；运行时
    # _build_settings 从 payload 构造 Settings，模板即随之生效
    if summary_template:
        from video_to_summary.summarizers.openai import TEMPLATE_ALIASES

        template_name = TEMPLATE_ALIASES.get(summary_template, summary_template)
        if template_name not in list_templates():
            raise HTTPException(status_code=400, detail=f"unknown summary template: {summary_template}")
        job_payload["summary_template"] = template_name

    try:
        job = create_job(job_payload)
    except LabelStoreError as exc:
        # 创建时标签非法（保留名/超量/超长）→ 400，而不是 500
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    enqueue_job(job)
    return JobResponse(job_id=job.job_id, status=job.status)


@api_post("/jobs", response_model=JobResponse)
async def create_job_api(payload: JobPayload) -> JobResponse:
    if payload.source_type == "url" and not payload.url:
        raise HTTPException(status_code=400, detail="url is required for url source")
    if payload.source_type == "local" and not payload.audio_path:
        raise HTTPException(status_code=400, detail="audio_path is required for local source")

    return _create_job_from_values(
        source_type=payload.source_type,
        url=payload.url,
        audio_path=payload.audio_path,
        title=payload.title,
        labels=payload.labels,
        summary_template=payload.summary_template,
    )


# ============ 浏览器上传任务源 ============
# 远程 / Docker 部署下容器看不到用户本机文件，「本地文件」源的补充入口：
# 浏览器直传 → 服务端流式落盘 uploads/<uuid>/ → 与 POST /jobs 完全相同的建任务核心。
# 本机部署不需要它（服务端原生选择框 / 目录浏览零拷贝直读），前端两个入口并存。

# 流式写盘的分块大小（UploadFile.read 步长；磁盘顺序写，1MB 足够大文件吞吐）
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _upload_max_bytes() -> int:
    """上传大小上限（字节）：``VTS_UPLOAD_MAX_MB``，默认 2048MB。"""
    try:
        mb = max(1, int(os.environ.get("VTS_UPLOAD_MAX_MB", "2048")))
    except (TypeError, ValueError):
        mb = 2048
    return mb * 1024 * 1024


def _sanitize_upload_name(raw: str) -> str:
    """上传文件名清洗：剥离目录成分（防穿越）→ 危险字符折叠为 - → 限长。

    与产物下载名（_DOWNLOAD_UNSAFE_RE）同一字符口径；空结果回退固定名，
    绝不让恶意文件名逃出服务端生成的 uuid 目录。
    """
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = _DOWNLOAD_UNSAFE_RE.sub("-", name)[:_DOWNLOAD_NAME_MAX].strip("-. ")
    return name or "upload.bin"


def _parse_labels_form(raw: str) -> list[str] | None:
    """multipart 的 labels 表单字段（JSON 数组字符串）解析；空串 = 未提供。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="labels 必须是 JSON 数组字符串，如 [\"a\",\"b\"]") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise HTTPException(status_code=400, detail="labels 必须是字符串数组")
    return data


def _cleanup_upload_dir(upload_dir: Path) -> None:
    """上传中断/建任务失败时的半传文件回收（best-effort，不掩盖原始异常）。"""
    shutil.rmtree(upload_dir, ignore_errors=True)


@api_post("/jobs/upload", response_model=JobResponse)
async def create_job_upload_api(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    labels: str = Form(default=""),
    summary_template: str = Form(default=""),
) -> JobResponse:
    """浏览器直传本地音视频文件创建任务（一步完成：落盘 + 建任务）。

    - 文件流式写盘 ``uploads/<uuid>/<安全文件名>``（.part 临时 + os.replace 原子
      落盘，仓库「产物一律原子写」铁律），分块计数超限即断（413）并清理半传文件
    - 建任务复用 POST /jobs 的核心（全局默认配置 + 标签 + 单任务总结模板）；
      建任务失败时回收已落盘文件，不留孤儿
    - 上传文件归服务端托管：DELETE /jobs/{id} 时连带删除（用户自己的本地文件
      永不删除，语义见 delete_job）
    - 扩展名白名单与 /fs/browse 一致（_MEDIA_EXTS）
    """
    safe_name = _sanitize_upload_name(file.filename or "")
    ext = Path(safe_name).suffix.lower()
    if ext not in _MEDIA_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext or '(无扩展名)'}；"
                                                   f"支持：{', '.join(sorted(_MEDIA_EXTS))}")
    parsed_labels = _parse_labels_form(labels)

    max_bytes = _upload_max_bytes()
    # Content-Length 预检：超限请求在读体之前就拒绝，不浪费带宽与磁盘
    declared = request.headers.get("content-length")
    if declared and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件超过大小上限（{max_bytes // (1024 * 1024)}MB）")

    upload_dir = Path(upload_base()) / uuid.uuid4().hex
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / safe_name
    tmp_target = upload_dir / f"{safe_name}.part"
    try:
        import aiofiles

        async with aiofiles.open(tmp_target, "wb") as fh:
            received = 0
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                received += len(chunk)
                if received > max_bytes:
                    # Content-Length 可伪造/缺失（chunked 上传），流式计数是最终防线
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小上限（{max_bytes // (1024 * 1024)}MB）",
                    )
                await fh.write(chunk)
        os.replace(tmp_target, target)  # 原子落盘：绝无半写文件被当作有效产物
    except HTTPException:
        _cleanup_upload_dir(upload_dir)
        raise
    except Exception as exc:  # noqa: BLE001 - 客户端断开/磁盘失败等统一 400
        _cleanup_upload_dir(upload_dir)
        logger.warning("upload aborted before job creation: %s", exc)
        raise HTTPException(status_code=400, detail="上传中断或写入失败，请重试") from exc

    try:
        return _create_job_from_values(
            source_type="local",
            audio_path=str(target),
            title=(title or "").strip() or None,
            labels=parsed_labels,
            summary_template=(summary_template or "").strip() or None,
        )
    except Exception:
        # 建任务失败（标签非法/模板不存在等）→ 上传文件一并回收，不留孤儿
        _cleanup_upload_dir(upload_dir)
        raise


@api_get("/jobs")
async def list_jobs_api(
    label: str = "",
    unlabeled: int = 0,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str = "",
) -> JSONResponse:
    jobs = list_jobs(limit=limit, offset=offset, label=label or None, unlabeled=bool(unlabeled))
    total = count_jobs(label=label or None, unlabeled=bool(unlabeled))
    query = (q or "").strip()
    if not query:
        return JSONResponse({"jobs": jobs, "total": total})
    # 全文检索：正文（jobs_fts）+ 元数据（标题/源/payload LIKE）合并命中集，
    # 再按既有排序/分页/标签过滤取窗口；命中项附 <mark> 片段与命中来源
    from .fts_index import search_jobs_meta

    matches = search_jobs_meta(query)
    ids = list(matches.keys())
    if not ids:
        return JSONResponse({"jobs": [], "total": 0})
    jobs = list_jobs(
        limit=limit,
        offset=offset,
        label=label or None,
        unlabeled=bool(unlabeled),
        job_ids=ids,
    )
    total = count_jobs(label=label or None, unlabeled=bool(unlabeled), job_ids=ids)
    for item in jobs:
        match = matches.get(item["job_id"]) or {}
        item["match_snippet"] = match.get("snippet", "")
        item["match_kinds"] = match.get("kinds", [])
    return JSONResponse({"jobs": jobs, "total": total})


def _unlink_quiet(path: str) -> None:
    """后台清理临时导出文件：文件已不存在/被占用时不抛（避免清理本身报错）。"""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@api_get("/jobs/export")
async def export_jobs_api() -> FileResponse:
    """导出历史任务数据（终态任务 + 标签 + 自定义模板 + 产物文件，zip）。

    历史数据操作不做任何门控；打包写临时文件，响应完成后后台清理。
    """
    from datetime import datetime

    from .history_io import build_export_bundle

    tmp_path, _manifest = await asyncio.to_thread(build_export_bundle)
    date_tag = datetime.now().strftime("%Y%m%d")
    filename = f"vts-history-{date_tag}.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_unlink_quiet, str(tmp_path)),
    )


@api_post("/jobs/import")
async def import_jobs_api(request: Request) -> JSONResponse:
    """导入历史任务数据（raw body 上传 zip，免新增 multipart 依赖）。

    版本兼容 fail-closed：包 schema_version 高于本机 → 400 拒绝；job_id 冲突
    跳过（重复导入幂等）；全部 DB 写单事务原子生效。
    """
    import json as _json

    from .history_io import HistoryBundleError, import_bundle

    max_bytes = int(os.environ.get("VTS_IMPORT_MAX_MB", "512")) * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail=f"导出包超过大小上限（{max_bytes // (1024 * 1024)}MB）")
    data = await request.body()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"导出包超过大小上限（{max_bytes // (1024 * 1024)}MB）")
    if not data:
        raise HTTPException(status_code=400, detail="请求体为空：请上传导出的 zip 包")

    output_dir = Path(output_base())
    try:
        counters = await asyncio.to_thread(import_bundle, data, output_dir)
    except HistoryBundleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 导入层未知异常不外露细节
        logger.exception("history import failed")
        raise HTTPException(status_code=500, detail="导入失败：包内容可能损坏，详见服务日志") from exc
    logger.info("history import done: %s", _json.dumps(counters, ensure_ascii=False))
    return JSONResponse(counters)


@api_get("/jobs/{job_id}")
async def get_job_api(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    payload = job.payload or {}
    return JSONResponse(
        {
            "job_id": job.job_id,
            "status": job.status,
            "title": job.title,
            "source": job.source,
            # 源信息与列表接口对齐：历史详情页据此渲染可点击源链接 / 本地文件名
            "source_type": payload.get("source_type") or "",
            "source_url": payload.get("url") or "",
            "source_path": payload.get("audio_path") or payload.get("file_path") or "",
            # 任务使用的总结模板：详情页重试「换模板」下拉的默认选中值
            "summary_template": payload.get("summary_template") or "",
            "created_at": job.created_at,
            "retried_at": job.retried_at,
            "retry_count": job.retry_count,
            # 总结编辑标记（NULL=未编辑；前端据此显示「已编辑 · 时间」徽标）
            "summary_edited_at": job.summary_edited_at,
            "error": job.error,
            "result_paths": job.result_paths,
            "progress": job.progress.to_list(),
            "labels": get_job_labels(job.job_id),
        }
    )


@api_get("/jobs/{job_id}/progress")
async def job_progress_api(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse({"progress": job.progress.to_list()})


@api_post("/jobs/{job_id}/cancel")
async def cancel_job_api(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not cancel_job(job_id):
        # 终态任务（completed/failed/cancelled）无需停止
        raise HTTPException(status_code=409, detail=f"job already in terminal status: {job.status}")
    job = get_job(job_id)
    return JSONResponse({"job_id": job_id, "status": job.status if job else "cancelled"})


@api_post("/jobs/{job_id}/retry")
async def retry_job_api(job_id: str, payload: dict | None = None) -> JSONResponse:
    # 可选 body {"summary_template": "..."}：重试时换模板；缺省保留原模板
    template = (payload or {}).get("summary_template")
    try:
        job = retry_job(job_id, summary_template=template)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"job_id": job.job_id, "status": job.status})


@api_delete("/jobs/{job_id}")
async def delete_job_api(job_id: str) -> JSONResponse:
    try:
        delete_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"job_id": job_id, "deleted": True})


@api_get("/jobs/{job_id}/result")
async def job_result_api(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="job is not completed")
    return JSONResponse({"result_paths": job.result_paths})


# ============ 健康检查 ============

@api_get("/health")
async def health_api() -> JSONResponse:
    # llm_configured：是否任一槽位（推理/语音识别）填了真实 Key——BYOK 版前端据此显示
    # 「已配置 / 未配置」（未配置是常态路径，不是故障）；
    # config_import 带出旧版 JSON 一次性迁移的结果（前端据此弹一次性提示）；
    # version 来自 git tag（发布构建）或 git describe（开发态），前端侧栏展示；
    # db_newer_version：本地数据库由更新版本创建时返回其 schema 版本号（否则 None），
    # 前端据此常驻提示「请升级」（旧版读新版库 fail-open，见 db._apply_migrations）；
    # upload_max_mb：浏览器上传的大小上限（VTS_UPLOAD_MAX_MB），前端选文件时预校验
    from .llm_store import has_configured_key

    return JSONResponse(
        {
            "status": "ok",
            "version": get_version(),
            "llm_configured": has_configured_key(),
            "config_import": _legacy_import_info(),
            "db_newer_version": db.db_newer_version(),
            "upload_max_mb": _upload_max_bytes() // (1024 * 1024),
        }
    )


@api_get("/logs/export")
async def export_logs_api() -> Response:
    """导出脱敏诊断日志（环境头 + 最近任务 + 运行日志），供排查问题时附上。

    - 内容经 ``log_export.sanitize_text`` 统一脱敏（API Key/Fernet 密文/Bearer/
      cookie/查询参数 token/主目录伪名化），导出的文件即对外交付物
    - 服务端不落盘：内存 ring buffer 快照 → 直接流式返回
    - 已被 auth_middleware 覆盖（/api/*）
    """
    from ..log_export import build_log_bundle, export_filename

    content = await asyncio.to_thread(build_log_bundle)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{export_filename()}"'},
    )


def _legacy_import_info() -> dict:
    """汇总 legacy JSON 一次性迁移结果（meta 表），无迁移时返回空对象。"""
    from ..db import get_setting

    info: dict = {}
    for meta_key, name in (
        ("legacy_imported_llm_profiles", "llm_profiles"),
        ("legacy_imported_templates", "templates"),
    ):
        raw = get_setting(meta_key)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            count = int(payload.get("count") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if count > 0:
            entry: dict = {"count": count}
            at = payload.get("at")
            if isinstance(at, (int, float)):
                entry["at"] = float(at)
            info[name] = entry
    return info


# ============ 设置 ============

@api_get("/settings")
async def get_settings_api() -> JSONResponse:
    payload = job_defaults()
    # 插件额外设置项：无插件时**不**加该键，响应形状与基础版保持一致
    items = hooks.settings_items()
    if items:
        payload["plugin_sections"] = items
    return JSONResponse(payload)


@api_put("/settings")
async def update_settings_api(payload: dict) -> JSONResponse:
    try:
        set_job_defaults(payload)
    except ValueError as exc:
        # cookies_browser 等带白名单校验的键：非法值返回 400 而非静默忽略
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await get_settings_api()


# 本地文件浏览端点的音视频扩展名白名单（Web 端无法拿绝对路径，需后端枚举本机目录）
_MEDIA_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
}


@api_get("/fs/browse")
async def fs_browse_api(path: str = "") -> JSONResponse:
    """枚举本地目录中的子目录与音视频文件，供 Web 端「浏览路径」面板使用。

    本工具为单机本地应用（默认仅监听 127.0.0.1），用户本就可在表单输入任意
    本地路径，此端点只为其提供可视化导航，不扩大威胁面（可选鉴权中间件同样覆盖
    /api/*）。安全约束：仅接受绝对路径并规范化；隐藏项（以 . 开头）不展示。
    """
    from ..utils import configure_logging  # noqa: F401 (仅确保日志可用)

    if not path:
        home = str(Path.home())
        return JSONResponse({"path": home, "parent": None, "dirs": [], "files": [], "error": None})
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        return JSONResponse({"path": str(raw), "parent": None, "dirs": [], "files": [], "error": "path must be absolute"})
    resolved = raw.resolve()  # 规范化并解析符号链接，杜绝相对/逃逸形态
    if not resolved.is_dir():
        return JSONResponse({"path": str(resolved), "parent": None, "dirs": [], "files": [], "error": "not a directory"})
    def _scan_dir(res: Path):
        # 目录遍历是同步重 I/O：放到线程池，避免大目录阻塞事件循环
        try:
            entries = sorted(res.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (OSError, PermissionError):
            return "permission denied"
        dirs, files = [], []
        for p in entries:
            if p.name.startswith("."):
                continue  # 隐藏项（.DS_Store/.git 等）不展示
            try:
                if p.is_dir():
                    dirs.append({"name": p.name, "path": str(p)})
                elif p.suffix.lower() in _MEDIA_EXTS:
                    files.append({"name": p.name, "path": str(p)})
            except OSError:
                continue
        return dirs, files

    scan = await asyncio.to_thread(_scan_dir, resolved)
    if scan == "permission denied":
        return JSONResponse({"path": str(resolved), "parent": str(resolved.parent) if resolved.parent != resolved else None, "dirs": [], "files": [], "error": "permission denied"})
    dirs, files = scan
    parent = str(resolved.parent) if resolved.parent != resolved else None
    return JSONResponse({"path": str(resolved), "parent": parent, "dirs": dirs, "files": files, "error": None})


def _pick_file_native() -> dict:
    """弹出系统原生文件选择框，返回选中文件绝对路径（macOS / Windows）。

    统一模式：对话框由**独立子进程自己的主线程**持有，python 侧只等 stdout，
    不阻塞 uvicorn 事件循环 ——
    - macOS：osascript（AppleScript ``choose file``），规避 AppKit「NSWindow
      只能主线程实例化」约束（曾因 PyObjC 直调在子线程抛
      NSInternalInconsistencyException）
    - Windows：``powershell -STA`` + WinForms OpenFileDialog，规避 COM STA
      线程要求；脚本经 ``-EncodedCommand``（UTF-16LE Base64）传入，
      无命令行引号转义问题，中文标题/路径不乱码

    取消 → ``path: null``；不支持的平台（Linux）→ error，前端回退目录浏览面板。
    """
    import sys

    if sys.platform == "win32":
        return _pick_file_windows()
    if sys.platform == "darwin":
        return _pick_file_macos()
    return {"error": f"native file dialog unsupported on {sys.platform}"}


def _pick_file_macos() -> dict:
    """macOS 分支：osascript 独立进程弹 NSOpenPanel，stdout 返回 POSIX 路径。"""
    import subprocess

    script = 'POSIX path of (choose file with prompt "选择要总结的音视频文件")'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        return {"error": "osascript unavailable (非 macOS 环境?)"}
    except subprocess.TimeoutExpired:
        return {"error": "file dialog timed out"}
    if proc.returncode != 0:
        return {"path": None}  # 用户取消（含 -128 User canceled）
    path = proc.stdout.strip()
    return {"path": path} if path else {"path": None}


def _pick_file_windows() -> dict:
    """Windows 分支：powershell -STA + WinForms OpenFileDialog，stdout 返回路径。

    - ``-EncodedCommand``：脚本以 UTF-16LE Base64 传入，绕开命令行引号转义
      与中文代码页问题；输出侧 ``[Console]::OutputEncoding`` 强制 UTF-8，
      python 按 utf-8 解码（并去掉可能出现的 BOM），中文路径不丢字
    - 过滤器从 ``_MEDIA_EXTS`` 生成（与目录浏览面板白名单一致），
      同时提供「所有文件」段兜底
    - 用户取消：ShowDialog 非 OK → 无输出、退出码 0 → ``path: null``；
      PowerShell 自身报错 → 退出码非 0 → 透传 stderr 作为 error
    """
    import base64
    import subprocess

    media_patterns = ";".join(f"*{ext}" for ext in sorted(_MEDIA_EXTS))
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$dlg = New-Object System.Windows.Forms.OpenFileDialog;"
        "$dlg.Title = '选择要总结的音视频文件';"
        f"$dlg.Filter = '音视频文件|{media_patterns}|所有文件|*.*';"
        "if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dlg.FileName }"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        # 无窗口标志必须带：GUI 形态 spawn powershell 会弹控制台窗口
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            encoding="utf-8",
            timeout=600,
            **subprocess_no_window_kwargs(),
        )
    except FileNotFoundError:
        return {"error": "powershell unavailable (Windows 环境异常?)"}
    except subprocess.TimeoutExpired:
        return {"error": "file dialog timed out"}
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        return {"error": detail or "file dialog failed"}
    path = (proc.stdout or "").lstrip("\ufeff").strip()
    return {"path": path} if path else {"path": None}


@api_post("/fs/pick")
async def fs_pick_api() -> JSONResponse:
    """系统原生文件选择框（macOS / Windows 本地 GUI 会话；Linux 不支持）。

    任何 error 分支前端都会自动回退到 /api/fs/browse 目录浏览面板。
    """
    result = await asyncio.to_thread(_pick_file_native)
    return JSONResponse(result)


# ============ 浏览器 cookies ============
# 读取结果只回传「域名计数 + 登录态提示」，绝不回传 cookie 值（敏感凭据不出服务边界）

class CookiesTestPayload(BaseModel):
    browser: str


@api_post("/cookies/test")
async def cookies_test_api(payload: CookiesTestPayload) -> JSONResponse:
    """预检浏览器 cookies 可读性：加载所选浏览器的 cookie 库并统计域名分布。

    Chromium 系首次访问会触发 macOS 钥匙串授权弹窗（该请求阻塞至用户放行）；
    Safari 需要宿主进程有完全磁盘访问权限。任何读取失败都以 ok=false + error
    返回，不中断服务。响应不含任何 cookie 值。
    """
    from .settings_store import COOKIES_BROWSERS

    browser = (payload.browser or "").strip().lower()
    if browser not in COOKIES_BROWSERS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported browser: {payload.browser!r} (可选: {', '.join(sorted(COOKIES_BROWSERS))})",
        )
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail="yt-dlp is not installed") from exc

    result: dict = {
        "ok": False,
        "total": 0,
        "domains": [],
        "youtube_login_hint": False,
        "bilibili_login_hint": False,
        "error": None,
    }
    try:
        with yt_dlp.YoutubeDL({"cookiesfrombrowser": (browser,), "quiet": True, "no_warnings": True}) as ydl:
            cookies = list(ydl.cookiejar)
        domains: dict[str, int] = {}
        names: set[str] = set()
        for c in cookies:
            domain = (c.domain or "").lstrip(".")
            if domain:
                domains[domain] = domains.get(domain, 0) + 1
            names.add(c.name or "")
        result.update(
            ok=True,
            total=len(cookies),
            domains=sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[:8],
            # SID/LOGIN_INFO/SAPISID 存在即视为含 YouTube 登录态（不泄露任何值）
            youtube_login_hint=bool(names & {"SID", "LOGIN_INFO", "SAPISID"}),
            # SESSDATA 存在即视为含 B 站登录态（不泄露任何值）
            bilibili_login_hint="SESSDATA" in names,
        )
    except Exception as exc:  # noqa: BLE001 - 钥匙串拒绝/浏览器未装/库被锁均转为可读错误
        result["error"] = str(exc)[:300]
        logger.info("cookies test failed for %s: %s", browser, exc)
    return JSONResponse(result)


# ============ 历史任务标签 ============
# 任务侧按名字操作（自动 get-or-create）；管理侧按 id（重命名/删除/合并）。
# ValueError -> 400、KeyError -> 404，沿用项目既有风格。

@api_get("/labels")
async def list_labels_api() -> JSONResponse:
    return JSONResponse(list_labels())


class LabelCreatePayload(BaseModel):
    name: str


@api_post("/labels")
async def create_label_api(payload: LabelCreatePayload) -> JSONResponse:
    try:
        label = create_label(payload.name)
    except LabelStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(label)


class LabelRenamePayload(BaseModel):
    name: str


@api_put("/labels/{label_id}")
async def rename_label_api(label_id: int, payload: LabelRenamePayload) -> JSONResponse:
    try:
        label = rename_label(label_id, payload.name)
    except LabelStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(label)


@api_delete("/labels/{label_id}")
async def delete_label_api(label_id: int) -> JSONResponse:
    try:
        delete_label(label_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"deleted": True})


class LabelMergePayload(BaseModel):
    source_id: int
    target_id: int


@api_post("/labels/merge")
async def merge_labels_api(payload: LabelMergePayload) -> JSONResponse:
    try:
        merge_labels(payload.source_id, payload.target_id)
    except LabelStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"merged": True})


class JobLabelsPayload(BaseModel):
    labels: list[str] = []


@api_put("/jobs/{job_id}/labels")
async def set_job_labels_api(job_id: str, payload: JobLabelsPayload) -> JSONResponse:
    if get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        normalized = set_job_labels(job_id, payload.labels)
    except LabelStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"labels": normalized})


# ============ 总结模板 ============

@api_get("/templates")
async def list_templates_api() -> JSONResponse:
    # builtins 供前端把内置模板渲染为只读（不可编辑/删除）
    return JSONResponse({"templates": list_templates(), "builtins": list(SUMMARY_TEMPLATES)})


@api_get("/templates/{template_name}")
async def get_template_api(template_name: str) -> JSONResponse:
    try:
        template = get_template(template_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"name": template_name, "template": template})


@api_put("/templates/{template_name}")
async def save_template_api(template_name: str, payload: dict) -> JSONResponse:
    try:
        template = save_template(template_name, payload.get("prompt", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"name": template_name, "template": template})


@api_delete("/templates/{template_name}")
async def delete_template_api(template_name: str) -> JSONResponse:
    # 仅可删除 DB 自定义行；内置模板只读（ValueError → 400）
    try:
        deleted = delete_template(template_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"name": template_name, "deleted": deleted})


# ============ LLM 配置（BYOK） ============
# 本构建的核心配置面：用户自备任意 OpenAI 兼容端点（base_url / api_key / model），
# LLM 配置（BYOK）：固定两个槽位——推理模型（summary，总结与文本润色共用）与
# 语音识别模型（asr）。Key 一律经 crypto.py（Fernet）加密落库，对外返回掩码值。
# 本版没有任何管理员限制。

@api_get("/llm")
async def get_llm_config_api() -> JSONResponse:
    """两槽位配置视图（Key 掩码 + configured 布尔，不含真实 Key）。"""
    return JSONResponse(llm_config())


@api_put("/llm")
async def update_llm_config_api(payload: dict) -> JSONResponse:
    """部分更新槽位：payload 只需携带要修改的用途（summary / asr）。"""
    try:
        config = save_llm_config(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(config)


@api_post("/llm/import")
async def import_llm_from_env() -> JSONResponse:
    """从 ``.env`` 导入 LLM 配置（SUMMARY_* / ASR_* 三元组；LLM_* / OPENAI_API_KEY 为兼容别名）。"""
    return JSONResponse(import_from_env(dotenv_path=".env"))


# ============ 产物文件与导出 ============

def _resolve_job_file(job_id: str, rel: str) -> Path:
    """解析产物文件路径（``/file`` 与 ``/export`` 共用）。

    兼容两种入参：完整 result_paths（output/<job_id>/xxx.md）与相对 job 目录的
    纯文件名；严格限制在本次 job 自己的输出目录内，防止跨 job 读取
    （../other_job/... 逃逸）。不存在/目录/逃逸分别 404/404/400。
    """
    job_dir = (Path(output_base()) / job_id).resolve()
    parts = Path(rel).parts
    if parts and parts[0] == "output":
        parts = parts[1:]
    if parts and parts[0] == job_id:
        parts = parts[1:]
    target = job_dir.joinpath(*parts).resolve() if parts else job_dir
    if target != job_dir and not str(target).startswith(str(job_dir) + os.sep):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or target.is_dir():
        raise HTTPException(status_code=404, detail="file not found")
    return target


def _require_completed_job_with_file(job_id: str, rel: str | None) -> tuple:
    """/file 与 /export 共用的前置校验：任务存在 + 已完成 + path 给定且合法。"""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="job is not completed")
    if not rel:
        raise HTTPException(status_code=400, detail="path is required")
    return job, _resolve_job_file(job_id, rel)


# 下载文件名中不允许出现的字符（路径分隔/控制符/响应头注入字符折叠为 -）
_DOWNLOAD_UNSAFE_RE = re.compile(r"[/\\<>:\"|?*\x00-\x1f]+")
_DOWNLOAD_NAME_MAX = 80


def _download_filename(ext: str, job_title: str, job_id: str) -> tuple[str, str]:
    """(UTF-8 展示名, ASCII 兜底名)：``<标题>.<ext>``。

    展示名保留中文走 RFC 5987 ``filename*``；兜底名把非 ASCII/危险字符折叠
    为 -，供不支持 ``filename*`` 的场景可见。ext 必须是导出格式自身的扩展名
    （md/txt…），**不得**携带源产物文件的后缀（summary.md 的 "md" 曾被拼进
    文件名 stem，出现「标题-md.pdf」）。
    """
    base = _DOWNLOAD_UNSAFE_RE.sub("-", (job_title or "").strip())[:_DOWNLOAD_NAME_MAX].strip("-. ") or job_id
    ascii_base = re.sub(r"[^0-9A-Za-z._-]+", "-", base)[:_DOWNLOAD_NAME_MAX].strip("-.") or "job"
    return f"{base}.{ext}", f"{ascii_base}.{ext}"


@api_get("/jobs/{job_id}/file")
async def job_file_api(job_id: str, request: Request) -> JSONResponse:
    _job, target = _require_completed_job_with_file(job_id, request.query_params.get("path"))
    content = await asyncio.to_thread(target.read_text, encoding="utf-8")
    return JSONResponse({"path": str(target), "content": content})


async def job_export_api(job_id: str, request: Request) -> Response:
    """产物导出下载（真实 attachment）。

    ``format=md``（缺省）原文件直出。本版不提供 PDF 导出；请求 pdf 时明确 400
    而不是静默降级。

    同时注册 HEAD：前端导出后用轻量 HEAD 读 Content-Disposition 里的文件名
    以便 toast 告知「导出到哪 / 文件名」。HEAD 分支只算文件名与媒体类型就返回，
    不读文件——否则一次导出会重复读两遍。
    """
    from urllib.parse import quote

    from fastapi.responses import Response as FastAPIResponse

    job, target = _require_completed_job_with_file(job_id, request.query_params.get("path"))
    fmt = (request.query_params.get("format") or "md").lower()

    # 先定文件名与媒体类型（HEAD 与 GET 共用；不触碰产物文件）
    if fmt == "pdf":
        raise HTTPException(
            status_code=400,
            detail="本版不提供 PDF 导出：Markdown/纯文本为标准产物，请改用 format=md。",
        )
    if fmt != "md":
        raise HTTPException(status_code=400, detail="format 仅支持 md")
    # Markdown 导出保留产物自身扩展名（summary → .md / 转写 → .txt …）
    ext = "markdown" if target.suffix.lower() == ".markdown" else (target.suffix.lstrip(".").lower() or "txt")
    pretty, fallback = _download_filename(ext, job.title, job_id)
    media_type = "text/markdown; charset=utf-8" if ext in ("md", "markdown") else "text/plain; charset=utf-8"

    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(pretty)}"
    if request.method == "HEAD":
        return FastAPIResponse(
            b"",
            media_type=media_type,
            headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
        )

    body = await asyncio.to_thread(target.read_bytes)
    return FastAPIResponse(
        body,
        media_type=media_type,
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


def _register_export_methods(path: str, handler) -> None:
    """产物导出的 GET 与 HEAD **分别注册**。

    Starlette 不会把 HEAD 交给只注册 GET 的路由（实测直接 405），因此 HEAD 必须显式注册；
    但把同一处理函数挂在同一路径的两个方法上，又会让 FastAPI 的默认 operationId
    （只用 ``methods`` 里第一个方法拼 id）在两个操作上重复。故两种方法各用独立的
    ``name`` 注册。
    """
    for method, route_name in (("GET", "job_export_api"), ("HEAD", "job_export_head")):
        app.api_route(
            f"{API_PREFIX}{path}",
            methods=[method],
            name=route_name,
            include_in_schema=True,
        )(handler)


_register_export_methods("/jobs/{job_id}/export", job_export_api)


@api_put("/jobs/{job_id}/summary")
async def update_job_summary_api(job_id: str, request: Request) -> JSONResponse:
    """保存用户编辑后的总结内容（覆盖写回 summary 产物文件，原子替换）。

    只接受 ``result_paths["summary"]`` 指向的文件（不接受任意 path）；
    仅 completed 任务可编辑；成功后落 ``summary_edited_at`` 标记并重建该任务
    的全文检索索引（编辑稿可被检索命中）。
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="job is not completed")
    summary_path = (job.result_paths or {}).get("summary")
    if not summary_path:
        raise HTTPException(status_code=400, detail="该任务没有总结产物，无法编辑")

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - 非 JSON body 统一 400 口径
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="总结内容不能为空")

    target = _resolve_job_file(job_id, summary_path)
    # 原子落盘：同目录临时文件 + os.replace（产物写入仓库约定，绝不半写）
    import tempfile as _tempfile

    fd, tmp_name = _tempfile.mkstemp(dir=str(target.parent), prefix=".edit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    from . import tasks as _tasks

    edited_at = time.time()
    _tasks.set_summary_edited(job_id, edited_at)
    # 编辑稿入全文检索索引（best-effort，失败不影响保存结果）
    try:
        from .fts_index import index_job_artifacts

        index_job_artifacts(job_id, _tasks.get_job(job_id).result_paths)
    except Exception:  # noqa: BLE001
        logger.warning("summary edit: fts reindex failed for job %s", job_id, exc_info=True)
    return JSONResponse({"summary_edited_at": edited_at})


# 插件额外路由：在核心路由之后挂载（插件路由不得覆盖核心路径）。
# 注册失败按 fail-closed 处理——半注册的路由表比没有更危险。
hooks.register_plugin_routes(app)

__all__ = ["app"]
