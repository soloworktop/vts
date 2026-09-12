// ============ 进度事件流消费（事件名是契约，未知事件忽略而非报错） ============
// 传输形态：后端以「事件链」交付（GET /api/v1/jobs/{id} 的 progress 数组，见
// constants.py::JobEvent）。前端按事件名消费：
//   - 已收录的事件名驱动 stepper / 事件链 / 阶段内进度；
//   - **未收录的事件名一律忽略**（向前兼容：插件或新版本可能推送未知事件，
//     绝不能因为不认识某个事件就把页面渲染炸掉）。
// 该 reducer 是纯函数，便于测试与多视图复用。

import type { ProgressEvent } from "../api/types";
import { EVENT_LABELS } from "./constants";
import { formatDownloadProgress } from "./format";

// 已知事件名集合（constants.py::JobEvent 的镜像）。判断"未知"以此为准。
export const KNOWN_EVENT_NAMES = new Set<string>([
  "started",
  "download_start",
  "download_progress",
  "download_done",
  "subtitle_start",
  "subtitle_done",
  "subtitle_skipped",
  "transcribe_start",
  "transcribe_progress",
  "transcribe_done",
  "polish_start",
  "polish_done",
  "polish_skipped",
  "summarize_start",
  "summarize_done",
  "summarize_skipped",
  "finalized",
  "completed",
  "error",
  "cancelled",
  "cancel_requested",
]);

export function isKnownEvent(name: string): boolean {
  return KNOWN_EVENT_NAMES.has(name);
}

export interface StageState {
  doneSet: Set<string>; // 出现过 *_done 的阶段
  skippedSet: Set<string>; // 出现过 *_skipped 的阶段（已结论但被跳过：无可用字幕 / 无 Key）
  activeStage: string | null; // 最近一个尚未完成的 *_start 阶段
  failedStage: string | null; // 失败任务：出错时正在进行的阶段（标红）
}

/** 事件链 → 阶段状态（stepper 用）。 */
export function computeStages(events: ProgressEvent[], status?: string): StageState {
  const known = events.filter((e) => e && isKnownEvent(e.event));
  const doneSet = new Set(
    known.filter((i) => i.event.endsWith("_done")).map((i) => i.event.slice(0, -5)),
  );
  const skippedSet = new Set(
    known.filter((i) => i.event.endsWith("_skipped")).map((i) => i.event.slice(0, -8)),
  );
  const concluded = (stage: string) => doneSet.has(stage) || skippedSet.has(stage);
  const activeEvent = [...known]
    .reverse()
    .find((i) => i.event.endsWith("_start") && !concluded(i.event.slice(0, -6)));
  const activeStage = activeEvent ? activeEvent.event.slice(0, -6) : null;
  const failedStage = status === "failed" ? activeStage : null;
  return { doneSet, skippedSet, activeStage, failedStage };
}

/** 事件细节后缀（耗时/标题/时长等），对照旧 app.js eventDetailSuffix。 */
export function eventDetailSuffix(item: ProgressEvent): string {
  const p = (item.payload || {}) as Record<string, unknown>;
  const bits: string[] = [];
  if (item.event === "completed" && typeof p.elapsed === "number") {
    bits.push(`耗时 ${p.elapsed.toFixed(1)}s`);
  }
  if ((item.event === "download_done" || item.event === "subtitle_done") && p.title) {
    bits.push(String(p.title));
  }
  if (item.event === "download_done" && p.duration) bits.push(`时长 ${p.duration}s`);
  if (item.event === "subtitle_skipped" && p.reason && p.reason !== "no usable subtitle") {
    bits.push(String(p.reason));
  }
  if (
    ["transcribe_done", "polish_done", "subtitle_done", "summarize_done"].includes(item.event) &&
    typeof p.elapsed === "number"
  ) {
    bits.push(`${p.elapsed.toFixed(1)}s`);
  }
  return bits.length ? ` · ${bits.join(" · ")}` : "";
}

export function eventLabel(name: string): string {
  return EVENT_LABELS[name] || name;
}

/** 事件链里可展示的日志行（跳过纯进度事件）。 */
export function loggableEvents(events: ProgressEvent[]): ProgressEvent[] {
  return events.filter(
    (e) => e && isKnownEvent(e.event) && e.event !== "transcribe_progress" && e.event !== "download_progress",
  );
}

/** 阶段内进度槽：下载（百分比/速度）与转写（分片 n/m）共用，只认 *_start→done 区间内最新一条。 */
export function chunkProgressText(events: ProgressEvent[], activeStage: string | null): string {
  if (!activeStage) return "";
  const doneIdx = events.findIndex((e) => e.event === "transcribe_done");
  const scoped = doneIdx >= 0 ? events.slice(0, doneIdx) : events;
  const lastChunk = [...scoped].reverse().find((i) => i.event === "transcribe_progress");
  const dlDoneIdx = events.findIndex((e) => e.event === "download_done");
  const dlScoped = dlDoneIdx >= 0 ? events.slice(0, dlDoneIdx) : events;
  const lastDownload = [...dlScoped].reverse().find((i) => i.event === "download_progress");
  if (activeStage === "download" && lastDownload && lastDownload.payload) {
    return formatDownloadProgress(lastDownload.payload as Record<string, unknown>);
  }
  if (lastChunk && activeStage === "transcribe" && lastChunk.payload) {
    const done = Number(lastChunk.payload.done);
    const total = Number(lastChunk.payload.total);
    if (Number.isFinite(done) && Number.isFinite(total)) {
      return `转写分片 ${done}/${total}`;
    }
  }
  return "";
}

/** 卡片最近事件一行：跳过纯进度事件，展示带耗时细节的最新节点。 */
export function latestEventLine(events: ProgressEvent[]): string {
  const last = [...events]
    .reverse()
    .find(
      (i) =>
        i &&
        isKnownEvent(i.event) &&
        i.event !== "transcribe_progress" &&
        i.event !== "download_progress",
    );
  if (!last) return "";
  return `${eventLabel(last.event)}${eventDetailSuffix(last)}`;
}
