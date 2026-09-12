// 通用格式化工具（对照旧 app.js formatTime/formatDownloadProgress/shortenPath 等）

export function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string,
  );
}

export function formatTime(ts?: number | null): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtBytes(n: unknown): string {
  const num = Number(n);
  if (!Number.isFinite(num) || num <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let v = num;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)}${units[i]}`;
}

export function formatDownloadProgress(p: Record<string, unknown>): string {
  const bits: string[] = [];
  if (typeof p.percent === "number") bits.push(`下载 ${p.percent.toFixed(1)}%`);
  const total = Number(p.total_bytes) || 0;
  const got = Number(p.downloaded_bytes) || 0;
  if (total > 0) bits.push(`${fmtBytes(got)}/${fmtBytes(total)}`);
  else if (got > 0) bits.push(`已下载 ${fmtBytes(got)}`);
  const speed = Number(p.speed) || 0;
  if (speed > 0) bits.push(`${fmtBytes(speed)}/s`);
  const eta = Number(p.eta);
  if (Number.isFinite(eta) && eta > 0) {
    bits.push(`剩余 ${eta >= 60 ? `${Math.round(eta / 60)} 分` : `${Math.round(eta)}s`}`);
  }
  return bits.length ? bits.join(" · ") : "下载中…";
}

export function shortenPath(p: string): string {
  const parts = String(p).split(/[\\/]/).filter(Boolean);
  const tail = parts.slice(-2).join("/");
  return tail.length > 42 ? `${tail.slice(0, 39)}…` : tail;
}

/** 列表行标题：URL 型标题去参数截断为 域名+路径。 */
export function displayTitle(raw: string): string {
  if (/^https?:\/\//i.test(raw)) {
    try {
      const parsed = new URL(raw);
      return (parsed.hostname + parsed.pathname).replace(/\/$/, "");
    } catch {
      /* 非标准 URL 原样展示 */
    }
  }
  return raw;
}

/** 历史列表按自然日分组（今天/昨天/MM-DD）。 */
export function groupDayCap(ts?: number | null): string {
  if (!ts) return "更早";
  const d = new Date(ts * 1000);
  const startToday = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
  if (ts >= startToday) return "今天";
  if (ts >= startToday - 86400) return "昨天";
  return `${d.getMonth() + 1}-${d.getDate()}`;
}

/** fetch 网络层失败统一文案（TypeError = 断网/服务未响应）。 */
export function friendlyError(e: unknown, fallback = ""): string {
  if (e instanceof TypeError) return "网络异常或服务未响应，请稍后重试";
  if (e instanceof Error && e.message) return e.message;
  return fallback || String(e);
}
