// 最近来源历史（localStorage）：URL 与本地路径两类对称。
// 语义（铁律）：成功提交才记录、去重置顶、上限 10、末项「清空记录」。
import { HISTORY_KEY, LAST_LABELS_KEY, RECENT_CLEAR_VALUE, RECENT_SOURCE_LIMIT } from "./constants";

function read<T>(key: string, fallback: T): T {
  try {
    const raw = JSON.parse(localStorage.getItem(key) || "null");
    return raw ?? fallback;
  } catch {
    return fallback;
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* localStorage 不可用时静默 */
  }
}

export function rememberRecentSource(key: string, value: string): void {
  if (!value) return;
  const items = read<string[]>(key, []).filter((p) => p !== value);
  items.unshift(value);
  write(key, items.slice(0, RECENT_SOURCE_LIMIT));
}

export function storedRecentSources(key: string): string[] {
  const items = read<string[]>(key, []);
  return Array.isArray(items) ? items : [];
}

export function clearRecentSources(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

export const clearValue = RECENT_CLEAR_VALUE;

// ---- 最近任务（历史列表本地记忆，用于服务重启后补拉缺失条目） ----
export function rememberJob(jobId: string): void {
  const ids = read<string[]>(HISTORY_KEY, []);
  write(HISTORY_KEY, [jobId, ...ids.filter((id) => id !== jobId)].slice(0, 20));
}

export function storedJobIds(): string[] {
  return read<string[]>(HISTORY_KEY, []);
}

export function forgetJob(jobId: string): void {
  write(HISTORY_KEY, read<string[]>(HISTORY_KEY, []).filter((id) => id !== jobId));
}

// ---- 上次提交的标签（系列任务连续创建场景预填） ----
export function rememberLastLabels(labels: string[]): void {
  write(LAST_LABELS_KEY, labels);
}

export function storedLastLabels(): string[] {
  const v = read<string[]>(LAST_LABELS_KEY, []);
  return Array.isArray(v) ? v : [];
}
