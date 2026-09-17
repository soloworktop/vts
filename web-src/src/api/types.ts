// ============ 手写等价类型（对照 app.py 的 JSONResponse 字面量逐字段声明） ============
// 事实源：src/video_to_summary/web/app.py（每个字段的出处见行内注释）。
// 未知/插件新增字段一律用 index 签名或显式 unknown 保留，前端不因多余字段报错。

// ---- 能力声明 GET /api/v1/capabilities（app.py::capabilities_api） ----
// 插件经 hooks.declare_capabilities 动态声明的可选能力集合；核心构建（0 插件）恒为空对象。
export type Capabilities = Record<string, boolean>;

// ---- 健康检查 GET /api/v1/health（app.py::health_api） ----
export interface Health {
  status: string;
  version: string;
  llm_configured: boolean;
  config_import: Record<string, { count: number; at?: number }>;
  db_newer_version: number | null;
  // 浏览器上传大小上限（MB，VTS_UPLOAD_MAX_MB）：新建任务页选文件时预校验
  upload_max_mb: number;
}

// ---- 任务 ----
export type JobStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

// 进度事件（constants.py::JobEvent；事件名是契约，前端按名消费、未知名忽略）
export interface ProgressEvent {
  event: string;
  payload?: Record<string, unknown>;
  ts?: number;
}

// 列表条目（tasks.py::list_jobs；FTS 检索时附加 match_snippet/match_kinds）
export interface JobListItem {
  job_id: string;
  status: JobStatus;
  title: string;
  created_at: number;
  source: string;
  source_type: string;
  source_url: string;
  source_path: string;
  summary_template: string;
  retried_at: number | null;
  retry_count: number;
  error: string;
  labels: string[];
  match_snippet?: string;
  match_kinds?: string[];
}

// 详情 GET /api/v1/jobs/{id}（app.py:402）
export interface JobDetail {
  job_id: string;
  status: JobStatus;
  title: string;
  source: string;
  source_type: string;
  source_url: string;
  source_path: string;
  summary_template: string;
  created_at: number;
  retried_at: number | null;
  retry_count: number;
  summary_edited_at: number | null;
  error: string;
  result_paths: Record<string, string>;
  progress: ProgressEvent[];
  labels: string[];
}

export interface JobsPage {
  jobs: JobListItem[];
  total: number;
}

export interface JobResponse {
  job_id: string;
  status: string;
}

export interface JobProgressResponse {
  progress: ProgressEvent[];
}

// ---- 设置 GET/PUT /api/v1/settings（settings_store.py::job_defaults） ----
export interface Settings {
  summary_template: string;
  audio_format: string;
  polish_preset: string;
  subtitle_preference: string; // auto | manual_only | off
  subtitle_language: string;
  cookies_browser: string;
  proxy: string;
  polish_transcript: boolean;
}

// ---- 模板 GET/PUT/DELETE /api/v1/templates* ----
export interface TemplatesResponse {
  templates: string[];
  builtins: string[];
}

export interface TemplatePayload {
  prompt: string;
}

export interface TemplateResponse {
  name: string;
  template: TemplatePayload;
}

// ---- 标签 GET/POST/PUT/DELETE /api/v1/labels* ----
export interface Label {
  id: number;
  name: string;
  count: number;
}

export interface LabelsResponse {
  labels: Label[];
  uncategorized_count: number;
}

export interface JobLabelsResponse {
  labels: string[];
}

// ---- LLM 配置（BYOK）GET/PUT /api/v1/llm、POST /api/v1/llm/import ----
// 固定两槽位：summary = 推理模型（总结与文本润色共用），asr = 语音识别模型
export type LlmSlotKey = "summary" | "asr";

export interface LlmSlot {
  base_url: string;
  api_key: string; // 掩码值（llm_store 经 mask_api_key 返回）
  model: string;
  configured: boolean;
}

export type LlmConfig = Record<LlmSlotKey, LlmSlot>;

// ---- cookies 预检 POST /api/v1/cookies/test ----
export interface CookiesTestResponse {
  ok: boolean;
  total: number;
  domains: [string, number][];
  youtube_login_hint: boolean;
  /** cookie 名含 SESSDATA 即视为 B 站已登录（不回任何 cookie 值） */
  bilibili_login_hint: boolean;
  error: string | null;
}

// ---- 本地目录浏览 GET /api/v1/fs/browse ----
export interface FsEntry {
  name: string;
  path: string;
}

export interface FsBrowseResponse {
  path: string;
  parent: string | null;
  dirs: FsEntry[];
  files: FsEntry[];
  error: string | null;
}

export interface FsPickResponse {
  path: string | null;
  error?: string | null;
}

// ---- 产物文件 GET /api/v1/jobs/{id}/file ----
export interface FileContentResponse {
  path: string;
  content: string;
}

// ---- 历史导出/导入 POST /api/v1/jobs/import ----
export interface HistoryImportResult {
  imported: number;
  artifacts_restored: number;
  skipped_existing?: number;
}
