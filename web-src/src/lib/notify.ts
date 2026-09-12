// 任务终态系统通知（页面隐藏时才打扰用户）
const notifiedKeys = new Set<string>();

export function requestNotifyIfNeeded(): void {
  try {
    if ("Notification" in window && Notification.permission === "default") {
      void Notification.requestPermission();
    }
  } catch {
    /* 不支持则静默 */
  }
}

export function notifyTerminal(jobId: string, status: string, title?: string | null): void {
  try {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    if (!document.hidden) return;
    const key = `${jobId}:${status}`;
    if (notifiedKeys.has(key)) return;
    notifiedKeys.add(key);
    const labels: Record<string, string> = { completed: "任务完成", failed: "任务失败", cancelled: "任务已停止" };
    new Notification(`video-to-summary · ${labels[status] || status}`, {
      body: String(title || jobId).slice(0, 80),
    });
  } catch {
    /* 通知失败静默 */
  }
}
