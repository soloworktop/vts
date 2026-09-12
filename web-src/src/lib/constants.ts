// 展示常量（对照旧 app.js；文案与交互语义以旧前端为准，不做重设计）
import type { JobStatus } from "../api/types";

export const STATUS_LABELS: Record<JobStatus, string> = {
  pending: "等待中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已停止",
};

// 进度事件标签：事件名是契约（constants.py::JobEvent），未知事件忽略而非报错。
// polish_* 的 UI 入口已下线，标签保留用于历史任务回放（存量事件链仍含这些事件）。
export const EVENT_LABELS: Record<string, string> = {
  started: "任务开始",
  subtitle_start: "检查现有字幕",
  subtitle_done: "使用现有字幕（跳过下载与转写）",
  subtitle_skipped: "无可用字幕，回退下载转写",
  download_start: "获取音频开始",
  download_progress: "下载进度",
  download_done: "获取音频完成",
  transcribe_start: "转写开始",
  transcribe_progress: "转写分片",
  transcribe_done: "转写完成",
  polish_start: "文本优化开始",
  polish_done: "文本优化完成",
  summarize_start: "生成总结开始",
  summarize_done: "生成总结完成",
  summarize_skipped: "跳过总结（未配置可用 Key）",
  polish_skipped: "跳过文本优化（未配置可用 Key）",
  finalized: "产出文件",
  completed: "任务完成",
  cancel_requested: "已请求停止",
  cancelled: "任务已停止",
  error: "出错",
};

export const RESULT_KEY_LABELS: Record<string, string> = {
  summary: "总结",
  transcript: "转写",
  segments: "分段",
  subtitle: "字幕(SRT)",
};

// 状态页 stepper 的阶段（polish 阶段已从 UI 下线；字幕阶段按全局默认显隐）
export const STATUS_STAGES: { key: string; label: string }[] = [
  { key: "subtitle", label: "字幕" },
  { key: "download", label: "音频" },
  { key: "transcribe", label: "转写" },
  { key: "summarize", label: "总结" },
];

export const UNCATEGORIZED_LABEL = "未分类";

export const RECENT_URLS_KEY = "vts_recent_urls";
export const RECENT_PATHS_KEY = "vts_recent_source_paths";
export const RECENT_SOURCE_LIMIT = 10;
export const RECENT_CLEAR_VALUE = "__clear_recent__";
export const HISTORY_KEY = "vts_recent_jobs";
export const LAST_LABELS_KEY = "vts_last_labels";
export const THEME_KEY = "vts_theme";
export const HISTORY_SIDE_KEY = "vts_history_side_w";
export const LEGACY_IMPORT_ACK_KEY = "vts_legacy_import_ack";
