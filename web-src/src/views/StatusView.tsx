// ============ 任务状态视图：轮询 + 卡片渲染（只看非 completed 任务） ============
import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  cancelJob,
  deleteJob,
  fetchJob,
  fetchJobs,
  fetchTemplates,
  retryJob,
  type JobListItem,
} from "../api/endpoints";
import type { JobDetail } from "../api/types";
import { STATUS_LABELS, STATUS_STAGES } from "../lib/constants";
import { computeStages, chunkProgressText, latestEventLine } from "../lib/events";
import { formatTime, friendlyError } from "../lib/format";
import { forgetJob } from "../lib/recent";
import { notifyTerminal } from "../lib/notify";
import { useAppStore } from "../store/useAppStore";
import { useToast } from "../components/Toast";
import { Stepper } from "../components/Stepper";
import { JobError } from "../components/JobError";

export function StatusView() {
  const view = useAppStore((s) => s.view);
  const setView = useAppStore((s) => s.setView);
  const setSettingsTab = useAppStore((s) => s.setSettingsTab);
  const globalDefaults = useAppStore((s) => s.globalDefaults);
  const setRunningCount = useAppStore((s) => s.setRunningCount);
  const replaceSeenStatuses = useAppStore((s) => s.replaceSeenStatuses);
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [cards, setCards] = useState<JobListItem[]>([]);
  const [details, setDetails] = useState<Map<string, JobDetail>>(new Map());
  const [retryPicks, setRetryPicks] = useState<Map<string, string>>(new Map());

  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: fetchTemplates });
  const templateOptions = templatesData?.templates ?? [];

  const poll = useCallback(async () => {
    if (view !== "status" || document.visibilityState !== "visible") return;
    try {
      // 显式 200：状态页要看到全部非 completed 任务，不受历史分页默认 50 限制
      const page = await fetchJobs({ limit: 200 });
      const jobs = page.jobs;
      setRunningCount(jobs.filter((j) => j.status === "running" || j.status === "pending").length);

      // 有任务进入终态 → 立即同步一次历史列表
      if (detectTransitions(jobs)) {
        window.dispatchEvent(new CustomEvent("vts:refresh-history"));
      }

      const visible = jobs
        .filter((j) => j.status !== "completed")
        .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      setCards(visible);

      // 进行中任务并发拉详情：取完整事件流驱动 stepper
      const actives = visible.filter((j) => j.status === "running" || j.status === "pending");
      const list = await Promise.all(
        actives.map(async (j) => {
          try {
            return await fetchJob(j.job_id);
          } catch {
            return null;
          }
        }),
      );
      const map = new Map<string, JobDetail>();
      for (const d of list) {
        if (d) map.set(d.job_id, d);
      }
      setDetails(map);
    } catch {
      /* 网络抖动忽略，下个周期重试 */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, setRunningCount]);

  useEffect(() => {
    poll();
    const timer = setInterval(() => void poll(), 2000);
    return () => clearInterval(timer);
  }, [poll]);

  // 终态迁移检测：running/pending → completed/failed/cancelled → toast + 系统通知
  // poll 是稳定 useCallback（依赖挂载期内不变），闭包会捕获旧 state——
  // 这里必须经 useAppStore.getState() 读最新快照，否则通知永不触发或重复轰炸。
  function detectTransitions(jobs: JobListItem[]): number {
    const next = new Map(jobs.map((j) => [j.job_id, j.status]));
    const seen = useAppStore.getState().seenStatuses;
    let transitions = 0;
    for (const [jobId, prev] of seen) {
      if (prev !== "running" && prev !== "pending") continue;
      const now = next.get(jobId);
      if (now === "completed") {
        transitions += 1;
        const job = jobs.find((j) => j.job_id === jobId);
        const t = job?.title || jobId.slice(0, 8);
        showToast(`✓「${t}」已完成`, {
          ttl: 9000,
          action: { label: "查看总结", onClick: () => gotoHistory(jobId) },
        });
        notifyTerminal(jobId, "completed", job?.title);
      } else if (now === "failed" || now === "cancelled") {
        transitions += 1;
        const job = jobs.find((j) => j.job_id === jobId);
        notifyTerminal(jobId, now, job?.title);
      }
    }
    replaceSeenStatuses(next);
    return transitions;
  }

  const gotoHistory = (jobId: string) => {
    useAppStore.getState().setCurrentHistoryJobId(jobId);
    window.location.hash = `history/${jobId}`;
    setView("history");
  };

  const onStop = useCallback(
    async (jobId: string) => {
      try {
        const res = await cancelJob(jobId);
        showToast(res.status === "cancelled" ? "任务已停止" : "已请求停止，等待任务退出…");
      } catch (e) {
        if (e instanceof Error && String(e).includes("409")) {
          showToast("任务已结束");
        } else {
          showToast(friendlyError(e, "停止失败"), { kind: "error" });
        }
      }
      void poll();
    },
    [poll, showToast],
  );

  const onRetry = useCallback(
    async (jobId: string, template?: string) => {
      try {
        await retryJob(jobId, template || undefined);
        showToast("已重新提交任务");
        void poll();
      } catch (e) {
        showToast(friendlyError(e, "重试失败"), { kind: "error" });
      }
    },
    [poll, showToast],
  );

  const onDelete = useCallback(
    async (jobId: string) => {
      if (!window.confirm("确定删除该任务？将同时清理其所有产物文件（转写/总结/音频缓存），此操作不可恢复。")) return;
      try {
        await deleteJob(jobId);
        forgetJob(jobId);
        showToast("任务已删除");
        queryClient.invalidateQueries({ queryKey: ["history"] });
        void poll();
      } catch (e) {
        showToast(friendlyError(e, "删除失败"), { kind: "error" });
      }
    },
    [poll, showToast, queryClient],
  );

  const visibleStages = useMemo(() => {
    const subtitleOff = globalDefaults?.subtitle_preference === "off";
    return STATUS_STAGES.filter((s) => !(s.key === "subtitle" && subtitleOff));
  }, [globalDefaults]);

  if (!cards.length) {
    return (
      <section id="view-status" className="view">
        <header className="view-head">
          <h2>任务状态</h2>
          <p>正在进行的任务与需要处理的失败任务；完成后自动移入「历史任务」。</p>
        </header>
        <section className="panel status-panel">
          <div id="statusList" className="status-list"></div>
          <div id="statusEmpty" className="empty-state">
            <div className="empty-hero">
              <div className="empty-ico">🛰️</div>
              <div className="empty-title">没有进行中的任务</div>
              <p className="empty-hint">去「新建总结」提交一条视频链接，这里会实时显示流水线进度。</p>
            </div>
            <button id="gotoNewBtn" className="secondary" onClick={() => setView("new")}>
              去新建总结
            </button>
          </div>
        </section>
      </section>
    );
  }

  return (
    <section id="view-status" className="view">
      <header className="view-head">
        <h2>任务状态</h2>
        <p>正在进行的任务与需要处理的失败任务；完成后自动移入「历史任务」。</p>
      </header>
      <section className="panel status-panel">
        <div id="statusList" className="status-list">
          {cards.map((job) => {
            const status = job.status;
            const label = STATUS_LABELS[status] || status;
            const title = job.title || job.job_id;
            const detail = details.get(job.job_id);
            const active = status === "running" || status === "pending";
            const stages = active ? computeStages(detail?.progress ?? []) : null;
            const eventLine = active && detail ? latestEventLine(detail.progress) : "";
            const chunk = active && detail ? chunkProgressText(detail.progress, stages?.activeStage ?? null) : "";
            const tpl = (job.summary_template || "").trim();
            const retryPick = retryPicks.get(job.job_id);
            const retryValue =
              retryPick !== undefined && templateOptions.includes(retryPick)
                ? retryPick
                : templateOptions.includes(tpl)
                  ? tpl
                  : templateOptions[0] || "";

            return (
              <div className="status-card" data-id={job.job_id} key={job.job_id}>
                <div className="status-card-head">
                  <span className={`job-status status-${status}`}>{label}</span>
                  <span className="status-card-title" title={title}>
                    {title}
                  </span>
                  {tpl && (
                    <span className="tag-pill tpl-pill status-card-tpl" title={`总结模板：${tpl}`}>
                      📝 {tpl}
                    </span>
                  )}
                  <span className="status-card-time">{formatTime(job.created_at)}</span>
                  <div className="status-card-actions">
                    {active && (
                      <button type="button" className="secondary" data-action="stop" onClick={() => void onStop(job.job_id)}>
                        停止
                      </button>
                    )}
                    {!active && (
                      <>
                        <button type="button" className="secondary" data-action="retry" onClick={() => void onRetry(job.job_id, retryValue)}>
                          重试
                        </button>
                        <button type="button" className="secondary" data-action="delete" onClick={() => void onDelete(job.job_id)}>
                          删除
                        </button>
                      </>
                    )}
                    <button type="button" className="secondary" data-action="view" onClick={() => gotoHistory(job.job_id)}>
                      查看
                    </button>
                  </div>
                </div>

                {active && <Stepper stages={visibleStages} doneSet={stages?.doneSet ?? new Set()} skippedSet={stages?.skippedSet ?? new Set()} activeStage={stages?.activeStage ?? null} />}

                <div className="status-card-foot">
                  {active && (
                    <>
                      <span className="status-card-event" data-event>
                        {eventLine}
                      </span>
                      <span className={`chunk-progress${chunk ? "" : " hidden"}`}>{chunk}</span>
                    </>
                  )}
                  {!active && (
                    <label className="card-retry-tpl">
                      重试模板{" "}
                      <select
                        className="card-retry-template"
                        title="重试时使用的总结模板"
                        value={retryValue}
                        onChange={(e) => {
                          const next = new Map(retryPicks);
                          next.set(job.job_id, e.target.value);
                          setRetryPicks(next);
                        }}
                      >
                        {templateOptions.map((name) => (
                          <option key={name} value={name}>
                            {name}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </div>

                {status === "failed" && job.error && (
                  <div className="card-error visible">
                    <JobError
                      rawError={job.error}
                      onAction={(panels) => {
                        setSettingsTab(panels[0] ?? "tabDefaults");
                        setView("settings");
                      }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>
    </section>
  );
}
