// ============ 历史任务视图：左列筛选/搜索/分组列表 + 右列产物详情 ============
import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchLabels,
  fetchJobs,
  historyExportUrl,
  importHistory,
} from "../api/endpoints";
import type { LabelsResponse, JobListItem } from "../api/types";
import { HISTORY_SIDE_KEY, UNCATEGORIZED_LABEL } from "../lib/constants";
import { displayTitle, formatTime, groupDayCap } from "../lib/format";
import { desktopApi, isDesktopApp, triggerAnchorDownload } from "../lib/desktop";
import { useAppStore } from "../store/useAppStore";
import { useToast } from "../components/Toast";
import { HistoryDetail } from "./HistoryDetail";

const HISTORY_PAGE_SIZE = 50;
const HISTORY_PAGE_MAX = 200;
const HISTORY_WINDOW_MAX = 200;
const HISTORY_SIDE_MIN = 240;
const HISTORY_SIDE_MAX = 560;
const HISTORY_SIDE_DEFAULT = 330;

export function HistoryView() {
  const view = useAppStore((s) => s.view);
  const { showToast } = useToast();
  const entries = useAppStore((s) => s.historyEntries);
  const total = useAppStore((s) => s.historyTotal);
  const setHistoryEntries = useAppStore((s) => s.setHistoryEntries);
  const appendHistoryEntries = useAppStore((s) => s.appendHistoryEntries);
  const resetHistoryWindow = useAppStore((s) => s.resetHistoryWindow);
  const currentHistoryJobId = useAppStore((s) => s.currentHistoryJobId);
  const setCurrentHistoryJobId = useAppStore((s) => s.setCurrentHistoryJobId);
  const searchTerm = useAppStore((s) => s.historySearchTerm);
  const setSearchTerm = useAppStore((s) => s.setHistorySearchTerm);
  const activeLabelFilter = useAppStore((s) => s.activeLabelFilter);
  const setActiveLabelFilter = useAppStore((s) => s.setActiveLabelFilter);

  const [labelsData, setLabelsData] = useState<LabelsResponse | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const historyEverLoadedRef = useRef(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // ---- 拉取（带筛选）的列表窗口 ----
  const refreshHistory = useCallback(async () => {
    const limit = Math.max(HISTORY_PAGE_SIZE, Math.min(entries.length || HISTORY_PAGE_SIZE, HISTORY_PAGE_MAX));
    const [labelsRes, jobsRes] = await Promise.all([
      fetchLabels().catch(() => null),
      fetchJobs({
        label: activeLabelFilter === UNCATEGORIZED_LABEL ? null : activeLabelFilter,
        unlabeled: activeLabelFilter === UNCATEGORIZED_LABEL,
        q: searchTerm.trim() || undefined,
        limit,
        offset: 0,
      }).catch(() => null),
    ]);
    setLabelsData(labelsRes);
    // 筛选失效自动回落「全部」
    if (labelsRes) {
      const uncat = UNCATEGORIZED_LABEL;
      if (activeLabelFilter === uncat) {
        if (!(labelsRes.uncategorized_count > 0)) {
          setActiveLabelFilter(null);
          return refreshHistory();
        }
      } else if (activeLabelFilter) {
        const exists = (labelsRes.labels || []).some((l) => l.name === activeLabelFilter);
        if (!exists) {
          setActiveLabelFilter(null);
          return refreshHistory();
        }
      }
    }
    if (!jobsRes) return;
    const list = jobsRes.jobs || [];
    const merged = [...list];
    merged.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    setHistoryEntries(merged, jobsRes.total);
    historyEverLoadedRef.current = true;
  }, [activeLabelFilter, searchTerm, entries.length, setActiveLabelFilter, setHistoryEntries]);

  const loadMore = useCallback(async () => {
    if (loadingMore) return;
    if (entries.length >= Math.min(total, HISTORY_WINDOW_MAX)) return;
    setLoadingMore(true);
    try {
      const data = await fetchJobs({
        label: activeLabelFilter === UNCATEGORIZED_LABEL ? null : activeLabelFilter,
        unlabeled: activeLabelFilter === UNCATEGORIZED_LABEL,
        q: searchTerm.trim() || undefined,
        limit: HISTORY_PAGE_SIZE,
        offset: entries.length,
      });
      if (data && Array.isArray(data.jobs)) {
        appendHistoryEntries(data.jobs, data.total);
      }
    } catch {
      /* 翻页失败静默：下一页滚动会重试 */
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, entries.length, total, activeLabelFilter, searchTerm, appendHistoryEntries]);

  // 哨兵无限滚动
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (observed) => {
        if (observed.some((e) => e.isIntersecting)) void loadMore();
      },
      { rootMargin: "240px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [loadMore]);

  // 进入历史视图 / 筛选变化 → 刷新
  useEffect(() => {
    if (view !== "history") return;
    void refreshHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, activeLabelFilter, searchTerm]);

  // 全局「vts:refresh-history」事件（任务完成/创建后触发）
  useEffect(() => {
    const onRefresh = () => void refreshHistory();
    window.addEventListener("vts:refresh-history", onRefresh);
    return () => window.removeEventListener("vts:refresh-history", onRefresh);
  }, [refreshHistory]);

  // 自动选中：进入历史视图且尚未选中 → 最新一条；选中项已不在列表（被删除等）→ 回落最新
  useEffect(() => {
    if (view !== "history" || !entries.length) return;
    const exists = entries.some((j) => j.job_id === currentHistoryJobId);
    if (!currentHistoryJobId || !exists) {
      setCurrentHistoryJobId(entries[0].job_id);
    }
  }, [view, currentHistoryJobId, entries, setCurrentHistoryJobId]);

  // ---- 历史导出/导入 ----
  const onExportHistory = () => {
    const url = historyExportUrl();
    if (isDesktopApp() && desktopApi()?.export_product) {
      const now = new Date();
      const dateTag = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
      showToast("正在打包历史数据…", { ttl: 4000 });
      desktopApi()!
        .export_product!(url, `vts-history-${dateTag}.zip`)
        .then((r) => {
          if (!r) return;
          if (r.cancelled) {
            showToast("已取消导出", { ttl: 3000 });
            return;
          }
          if (!r.saved) {
            showToast("导出失败：请重试或改用浏览器下载", { kind: "error", ttl: 5000 });
            return;
          }
          showToast(`已导出到「${r.path}」`, {
            ttl: 9000,
            action: {
              label: "打开所在文件夹",
              onClick: () => {
                try {
                  desktopApi()?.reveal_in_finder?.(r.path!);
                } catch {
                  /* ignore */
                }
              },
            },
          });
        })
        .catch(() => showToast("导出失败：请重试", { kind: "error" }));
      return;
    }
    if (isDesktopApp()) {
      // 旧包未带导出桥：回退真实 URL 下载
      triggerAnchorDownload(url);
      showToast("已触发导出：请在系统保存框选择保存位置并确认", { ttl: 5000 });
      return;
    }
    // 浏览器：真实 attachment 下载（禁 Blob）+ toast 反馈
    showToast("正在打包历史数据…", { ttl: 4000 });
    // HEAD 探测文件名（不拉整包 zip 进内存，大包双倍带宽），下载仍走真实 URL
    fetch(url, { method: "HEAD" })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const name = (res.headers.get("content-disposition") || "").match(/filename="?([^";]+)"?/)?.[1] || "vts-history.zip";
        triggerAnchorDownload(url);
        showToast(`已导出「${name}」，保存在浏览器下载目录（可在下载栏打开）`, { ttl: 6000 });
      })
      .catch(() => {
        triggerAnchorDownload(url);
        showToast("已触发导出：请在浏览器下载栏查看文件", { ttl: 4000 });
      });
  };

  const onImportFile = useCallback(
    async (file: File) => {
      showToast("正在导入历史数据…", { ttl: 8000 });
      try {
        const data = await importHistory(file);
        const skipped = data.skipped_existing ? `，跳过已有 ${data.skipped_existing}` : "";
        showToast(`导入完成：${data.imported} 条任务、${data.artifacts_restored} 个产物${skipped}`, { ttl: 6000 });
        resetHistoryWindow();
        void refreshHistory();
      } catch (e) {
        const msg = e instanceof Error ? e.message : "导入失败";
        showToast(msg, { kind: "error", ttl: 6000 });
      }
    },
    [refreshHistory, resetHistoryWindow, showToast],
  );

  // ---- 左列宽度拖拽 ----
  const [sideW, setSideW] = useState<number>(() => {
    try {
      const saved = parseInt(localStorage.getItem(HISTORY_SIDE_KEY) || "", 10);
      return Number.isFinite(saved) ? saved : HISTORY_SIDE_DEFAULT;
    } catch {
      return HISTORY_SIDE_DEFAULT;
    }
  });
  const dragging = useRef(false);

  const clampSide = (px: number) => Math.min(HISTORY_SIDE_MAX, Math.max(HISTORY_SIDE_MIN, px));

  const onResizeDown = (e: React.PointerEvent) => {
    e.preventDefault();
    dragging.current = true;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onResizeMove = (e: React.PointerEvent) => {
    if (!dragging.current) return;
    const rect = (e.currentTarget as HTMLElement).parentElement?.getBoundingClientRect();
    if (!rect) return;
    setSideW(clampSide(e.clientX - rect.left));
  };
  const onResizeUp = (e: React.PointerEvent) => {
    if (!dragging.current) return;
    dragging.current = false;
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* 已释放 */
    }
    try {
      localStorage.setItem(HISTORY_SIDE_KEY, String(sideW));
    } catch {
      /* ignore */
    }
  };

  // 渲染分组列表
  const renderGrouped = () => {
    if (!entries.length) {
      return <div className="empty">{searchTerm.trim() ? "没有匹配的任务" : "暂无历史任务"}</div>;
    }
    const rows: React.ReactNode[] = [];
    let lastCap: string | null = null;
    entries.forEach((j, idx) => {
      const cap = groupDayCap(j.created_at);
      if (cap !== lastCap) {
        if (lastCap !== null) rows.push(<i className="divider" key={`d-${idx}`}></i>);
        rows.push(
          <div className="group-cap" key={`cap-${idx}`}>
            {cap}
          </div>,
        );
        lastCap = cap;
      } else if (idx > 0) {
        rows.push(<i className="divider" key={`d-${idx}`}></i>);
      }
      rows.push(renderRow(j));
    });
    if (entries.length < Math.min(total, HISTORY_WINDOW_MAX)) {
      rows.push(
        <div ref={sentinelRef} id="historySentinel" className="history-sentinel" key="sentinel">
          加载更多…
        </div>,
      );
    }
    return rows;
  };

  const renderRow = (j: JobListItem) => {
    const tpl = (j.summary_template || "").trim();
    const jobLabels = Array.isArray(j.labels) ? j.labels : [];
    const snippet = j.match_snippet || "";
    return (
      <div
        key={j.job_id}
        className={`history-item${j.job_id === currentHistoryJobId ? " active" : ""}`}
        data-id={j.job_id}
        role="button"
        tabIndex={0}
        aria-selected={j.job_id === currentHistoryJobId}
        onClick={() => selectJob(j.job_id)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            selectJob(j.job_id);
          }
        }}
      >
        <div className="history-row">
          <span className={`status-dot status-${j.status}`} title={j.status}></span>
          <div className="history-title" title={j.title || j.job_id}>
            {displayTitle(j.title || j.job_id)}
          </div>
          <span className="history-time">{formatTime(j.created_at)}</span>
        </div>
        {(tpl || jobLabels.length > 0) && (
          <div className="history-meta">
            {tpl && (
              <span className="tag-pill tpl-pill" title={`总结模板：${tpl}`}>
                📝 {tpl}
              </span>
            )}
            {jobLabels.length > 0 && (
              <span className="history-labels">
                {jobLabels.slice(0, 3).map((n) => (
                  <span className="tag-pill" title={n} key={n}>
                    {n}
                  </span>
                ))}
                {jobLabels.length > 3 && (
                  <span className="tag-pill tag-uncat" title={jobLabels.join(", ")}>
                    +{jobLabels.length - 3}
                  </span>
                )}
              </span>
            )}
          </div>
        )}
        {snippet && (
          <div
            className="history-snippet"
            // 片段来自转写/总结（外部内容）：先整体转义，再还原服务端 <mark> 占位
            dangerouslySetInnerHTML={{
              __html: `${escapeSnippet(snippet)}${kindBadges(j.match_kinds)}`,
            }}
          />
        )}
      </div>
    );
  };

  const selectJob = (jobId: string) => {
    setCurrentHistoryJobId(jobId);
  };

  return (
    <section id="view-history" className="view view-wide">
      <header className="view-head">
        <h2>历史任务</h2>
        <p>浏览总结与转写原文；流水线进度请看「任务状态」。</p>
      </header>

      <div className="history-layout" style={{ "--history-side-w": `${sideW}px` } as React.CSSProperties}>
        <aside className="panel history-side">
          <input
            id="historySearch"
            className="history-search"
            type="search"
            placeholder="搜索标题 / 链接 / 正文…"
            autoComplete="off"
            value={searchTerm}
            onChange={(e) => {
              const v = e.target.value;
              setSearchTerm(v);
              if (searchTimer.current) clearTimeout(searchTimer.current);
              searchTimer.current = setTimeout(() => {
                searchTimer.current = null;
                void refreshHistory();
              }, 300);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape" && searchTerm) {
                if (searchTimer.current) clearTimeout(searchTimer.current);
                setSearchTerm("");
                void refreshHistory();
              }
            }}
          />
          <div className="history-io-bar">
            <button
              id="historyExportBtn"
              className="secondary"
              type="button"
              title="把全部历史任务、标签、自定义模板与产物打包为 zip 下载"
              onClick={onExportHistory}
            >
              导出历史
            </button>
            <button
              id="historyImportBtn"
              className="secondary"
              type="button"
              title="导入历史导出包（zip）：已存在的任务自动跳过"
              onClick={() => fileInputRef.current?.click()}
            >
              导入历史
            </button>
            <input
              ref={fileInputRef}
              id="historyImportInput"
              type="file"
              accept=".zip,application/zip"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file) void onImportFile(file);
              }}
            />
          </div>

          {labelsData && (labelsData.labels.length > 0 || labelsData.uncategorized_count > 0) && (
            <div id="labelFilterBar" className="label-filter-bar">
              <button
                type="button"
                className={`label-chip${activeLabelFilter === null ? " active" : ""}`}
                onClick={() => setActiveLabelFilter(null)}
              >
                全部
              </button>
              {labelsData.uncategorized_count > 0 && (
                <button
                  type="button"
                  className={`label-chip${activeLabelFilter === UNCATEGORIZED_LABEL ? " active" : ""}`}
                  onClick={() => setActiveLabelFilter(activeLabelFilter === UNCATEGORIZED_LABEL ? null : UNCATEGORIZED_LABEL)}
                >
                  未分类<span className="chip-count">{labelsData.uncategorized_count}</span>
                </button>
              )}
              {labelsData.labels.map((l) => (
                <button
                  type="button"
                  key={l.id}
                  className={`label-chip${activeLabelFilter === l.name ? " active" : ""}`}
                  onClick={() => setActiveLabelFilter(activeLabelFilter === l.name ? null : l.name)}
                >
                  {l.name}
                  <span className="chip-count">{l.count}</span>
                </button>
              ))}
              <button
                type="button"
                className="label-chip manage-link"
                onClick={() => {
                  useAppStore.getState().setSettingsTab("tabLabels");
                  useAppStore.getState().setView("settings");
                }}
              >
                管理
              </button>
            </div>
          )}

          <div id="jobHistory" className="job-history">
            {renderGrouped()}
          </div>
        </aside>

        <div
          className="history-resizer"
          id="historyResizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="拖动调整任务列表宽度"
          title="拖动调整列表宽度（双击复位）"
          tabIndex={0}
          onPointerDown={onResizeDown}
          onPointerMove={onResizeMove}
          onPointerUp={onResizeUp}
          onPointerCancel={onResizeUp}
          onDoubleClick={() => {
            setSideW(HISTORY_SIDE_DEFAULT);
            try {
              localStorage.removeItem(HISTORY_SIDE_KEY);
            } catch {
              /* ignore */
            }
          }}
        ></div>

        <section className="panel history-detail">
          {!currentHistoryJobId && (
            <div id="historyDetailEmpty" className="empty-state">
              <div className="empty-hero">
                <div className="empty-ico">🗂️</div>
                <div className="empty-title">从左侧选择一个任务</div>
                <p className="empty-hint">查看总结、转写原文等产物内容。</p>
              </div>
            </div>
          )}
          {currentHistoryJobId && <HistoryDetail key={currentHistoryJobId} jobId={currentHistoryJobId} />}
        </section>
      </div>
    </section>
  );
}

function escapeSnippet(s: string): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replaceAll("&lt;mark&gt;", "<mark>")
    .replaceAll("&lt;/mark&gt;", "</mark>");
}

function kindBadges(kinds?: string[]): string {
  const list = Array.isArray(kinds) ? kinds : [];
  if (!list.length) return "";
  const labels: Record<string, string> = { transcript: "转写", summary: "总结" };
  return `<span class="snippet-kinds">${list
    .map((k) => `<i>${escapeSnippet(labels[k] || k)}</i>`)
    .join("")}</span>`;
}
