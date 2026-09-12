// ============ 端点函数：组件只 import 这里，不直接拼 URL ============
import { api, apiFetch, downloadUrl } from "./client";
import type {
  Capabilities,
  Label,
  CookiesTestResponse,
  FileContentResponse,
  FsBrowseResponse,
  FsPickResponse,
  Health,
  HistoryImportResult,
  JobDetail,
  JobLabelsResponse,
  JobListItem,
  JobProgressResponse,
  JobResponse,
  JobsPage,
  LabelsResponse,
  LlmConfig,
  LlmSlot,
  LlmSlotKey,
  Settings,
  TemplateResponse,
  TemplatesResponse,
} from "./types";

// ---------- 健康 / 能力 ----------

export const fetchHealth = () => api.get<Health>("/health");

export const fetchCapabilities = () => api.get<Capabilities>("/capabilities");

// ---------- 任务 ----------

export interface CreateJobPayload {
  source_type: "url" | "local";
  url?: string;
  audio_path?: string;
  title?: string;
  labels?: string[];
  summary_template?: string;
}

export const createJob = (payload: CreateJobPayload) => api.post<JobResponse>("/jobs", payload);

export interface ListJobsParams {
  label?: string | null;
  unlabeled?: boolean;
  limit?: number;
  offset?: number;
  q?: string;
}

export function listJobsQ(params: ListJobsParams): string {
  const qs = new URLSearchParams();
  if (params.unlabeled) qs.set("unlabeled", "1");
  else if (params.label) qs.set("label", params.label);
  if (params.q) qs.set("q", params.q);
  qs.set("limit", String(params.limit ?? 50));
  qs.set("offset", String(params.offset ?? 0));
  return qs.toString();
}

export const fetchJobs = (params: ListJobsParams = {}) =>
  api.get<JobsPage>(`/jobs?${listJobsQ(params)}`);

export const fetchJob = (jobId: string) => api.get<JobDetail>(`/jobs/${encodeURIComponent(jobId)}`);

export const fetchJobProgress = (jobId: string) =>
  api.get<JobProgressResponse>(`/jobs/${encodeURIComponent(jobId)}/progress`);

export const cancelJob = (jobId: string) =>
  api.post<JobResponse>(`/jobs/${encodeURIComponent(jobId)}/cancel`);

export const retryJob = (jobId: string, summaryTemplate?: string) =>
  api.post<JobResponse>(
    `/jobs/${encodeURIComponent(jobId)}/retry`,
    summaryTemplate ? { summary_template: summaryTemplate } : undefined,
  );

export const deleteJob = (jobId: string) =>
  api.del<{ job_id: string; deleted: boolean }>(`/jobs/${encodeURIComponent(jobId)}`);

export const fetchJobFile = (jobId: string, path: string) =>
  api.get<FileContentResponse>(
    `/jobs/${encodeURIComponent(jobId)}/file?path=${encodeURIComponent(path)}`,
  );

export const saveJobSummary = (jobId: string, content: string) =>
  api.put<{ summary_edited_at: number }>(`/jobs/${encodeURIComponent(jobId)}/summary`, { content });

export const setJobLabels = (jobId: string, labels: string[]) =>
  api.put<JobLabelsResponse>(`/jobs/${encodeURIComponent(jobId)}/labels`, { labels });

/** 产物导出：真实 attachment 下载（禁 Blob），返回接口地址本身。 */
export function jobExportUrl(jobId: string, path: string, format: string): string {
  return downloadUrl(
    `/jobs/${encodeURIComponent(jobId)}/export?path=${encodeURIComponent(path)}&format=${encodeURIComponent(format)}`,
  );
}

// ---------- 设置 ----------

export const fetchSettings = () => api.get<Settings>("/settings");

export const updateSettings = (payload: Partial<Settings>) => api.put<Settings>("/settings", payload);

export const testCookies = (browser: string) =>
  api.post<CookiesTestResponse>("/cookies/test", { browser });

// ---------- 模板 ----------

export const fetchTemplates = () => api.get<TemplatesResponse>("/templates");

export const fetchTemplate = (name: string) =>
  api.get<TemplateResponse>(`/templates/${encodeURIComponent(name)}`);

export const saveTemplate = (name: string, prompt: string) =>
  api.put<TemplateResponse>(`/templates/${encodeURIComponent(name)}`, { prompt });

export const deleteTemplate = (name: string) =>
  api.del<{ name: string; deleted: boolean }>(`/templates/${encodeURIComponent(name)}`);

// ---------- 标签 ----------

export const fetchLabels = () => api.get<LabelsResponse>("/labels");

export const createLabel = (name: string) => api.post<Label>("/labels", { name });

export const renameLabel = (labelId: number, name: string) =>
  api.put<Label>(`/labels/${labelId}`, { name });

export const deleteLabel = (labelId: number) => api.del<{ deleted: boolean }>(`/labels/${labelId}`);

export const mergeLabels = (sourceId: number, targetId: number) =>
  api.post<{ merged: boolean }>("/labels/merge", { source_id: sourceId, target_id: targetId });

// ---------- LLM 配置（BYOK）：两槽位 ----------

export const fetchLlmConfig = () => api.get<LlmConfig>("/llm");

export const updateLlmConfig = (payload: Partial<Record<LlmSlotKey, Partial<LlmSlot>>>) =>
  api.put<LlmConfig>("/llm", payload);

export const importLlmFromEnv = () => api.post<LlmConfig>("/llm/import");

// ---------- 本地文件 ----------

export const browseFs = (path: string) =>
  api.get<FsBrowseResponse>(`/fs/browse?path=${encodeURIComponent(path)}`);

export const pickFile = () => api.post<FsPickResponse>("/fs/pick");

// ---------- 诊断日志 / 历史数据（真实附件下载） ----------

export const logsExportUrl = () => downloadUrl("/logs/export");

export const historyExportUrl = () => downloadUrl("/jobs/export");

export const importHistory = (file: File) =>
  apiFetch<HistoryImportResult>("/jobs/import", { method: "POST", rawBody: file });

/** 从 Content-Disposition 解析文件名（HEAD 探测用）。 */
export function parseDownloadName(disposition: string): string {
  const star = disposition.match(/filename\*=UTF-8''([^;]+)/);
  if (star) {
    try {
      return decodeURIComponent(star[1]);
    } catch {
      /* 非法编码回落兜底名 */
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/);
  return plain ? plain[1] : "";
}

export type { JobListItem };
