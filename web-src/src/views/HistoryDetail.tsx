// ============ 历史详情（右列）：产物浏览 / 编辑 / 导出 / 重试 / 删除 ============
import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteJob,
  fetchJob,
  fetchJobFile,
  fetchTemplates,
  jobExportUrl,
  parseDownloadName,
  retryJob,
  saveJobSummary,
  setJobLabels,
} from "../api/endpoints";
import type { JobDetail } from "../api/types";
import { STATUS_LABELS, STATUS_STAGES } from "../lib/constants";
import { computeStages, eventDetailSuffix, eventLabel, isKnownEvent, loggableEvents } from "../lib/events";
import { isMarkdownPath, renderMarkdown } from "../lib/markdown";
import { formatTime, friendlyError } from "../lib/format";
import { forgetJob } from "../lib/recent";
import { desktopApi, isDesktopApp, triggerAnchorDownload } from "../lib/desktop";
import { useAppStore } from "../store/useAppStore";
import { useToast } from "../components/Toast";
import { Stepper } from "../components/Stepper";
import { JobError } from "../components/JobError";
import { TagEditor } from "../components/TagEditor";
import { Modal } from "../components/Modal";

type ProductKey = "summary" | "transcript" | "segments" | "subtitle" | "";

interface HistoryDetailProps {
  jobId: string;
}

export function HistoryDetail({ jobId }: HistoryDetailProps) {
  const setView = useAppStore((s) => s.setView);
  // 订阅而非渲染期 getState()：设置页改字幕偏好后详情页 stepper 实时更新
  const globalDefaults = useAppStore((s) => s.globalDefaults);
  const setSettingsTab = useAppStore((s) => s.setSettingsTab);
  const setCurrentHistoryJobId = useAppStore((s) => s.setCurrentHistoryJobId);
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: detail, error } = useQuery({
    queryKey: ["job-detail", jobId],
    queryFn: () => fetchJob(jobId),
    refetchInterval: (q) => {
      const st = q.state.data?.status;
      return st === "running" || st === "pending" ? 2000 : false;
    },
    retry: false,
  });

  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: fetchTemplates });
  const templateOptions = templatesData?.templates ?? [];

  const [productKey, setProductKey] = useState<ProductKey>("");
  const [productPath, setProductPath] = useState("");
  const [productContent, setProductContent] = useState("");
  const [productRendered, setProductRendered] = useState(false);
  const [loadingProduct, setLoadingProduct] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState("");
  const editOriginalRef = useRef<string | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [retryModalOpen, setRetryModalOpen] = useState(false);
  const [retryTpl, setRetryTpl] = useState("");
  const [dirtyChoiceOpen, setDirtyChoiceOpen] = useState(false);
  const dirtyResolveRef = useRef<((c: "save" | "discard" | "cancel") => void) | null>(null);
  const loadProductSeqRef = useRef(0);
  const [labelsEditing, setLabelsEditing] = useState(false);
  const [labelDraft, setLabelDraft] = useState<string[]>([]);

  const terminal =
    detail?.status === "completed" || detail?.status === "failed" || detail?.status === "cancelled";
  const summaryEditedAt = typeof detail?.summary_edited_at === "number" ? detail.summary_edited_at : null;
  const paths = detail?.result_paths || {};
  const hasMore = !!(paths.segments || paths.subtitle);
  const hasProducts = !!(paths.summary || paths.transcript || hasMore);

  // ---- 产物加载 ----
  const loadProduct = useCallback(
    async (key: ProductKey) => {
      if (!detail) return;
      const path = (detail.result_paths || {})[key as string];
      if (!path) return;
      setProductKey(key);
      setProductPath(path);
      setLoadingProduct(true);
      // 竞态守卫：快速切换 tab 时只接受最后一次请求的结果
      const seq = ++loadProductSeqRef.current;
      try {
        const data = await fetchJobFile(detail.job_id, path);
        if (seq !== loadProductSeqRef.current) return;
        setProductContent(data.content || "");
        const isMd = key === "summary" && isMarkdownPath(path);
        setProductRendered(isMd && Boolean(data.content));
      } catch (e) {
        if (seq !== loadProductSeqRef.current) return;
        setProductContent(friendlyError(e, "文件加载失败"));
        setProductRendered(false);
      } finally {
        if (seq === loadProductSeqRef.current) setLoadingProduct(false);
      }
    },
    [detail],
  );

  // 默认展示总结；没有总结回落转写原文
  useEffect(() => {
    if (!detail) return;
    const firstKey: ProductKey = paths.summary ? "summary" : paths.transcript ? "transcript" : "";
    setProductKey(firstKey);
    if (firstKey) {
      void loadProduct(firstKey);
    } else {
      setProductContent("");
      setProductPath("");
    }
    setEditing(false);
    editOriginalRef.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.job_id, detail?.status]);

  // ---- 编辑态 ----
  const dirty = editing && editOriginalRef.current !== null && editDraft !== editOriginalRef.current;

  const askDirty = useCallback(() => {
    return new Promise<"save" | "discard" | "cancel">((resolve) => {
      dirtyResolveRef.current = resolve;
      setDirtyChoiceOpen(true);
    });
  }, []);

  const closeDirty = useCallback(
    (choice: "save" | "discard" | "cancel") => {
      setDirtyChoiceOpen(false);
      dirtyResolveRef.current?.(choice);
      dirtyResolveRef.current = null;
    },
    [],
  );

  const saveSummary = useCallback(async (): Promise<boolean> => {
    if (!detail || !editDraft.trim()) {
      showToast("总结内容不能为空", { kind: "error" });
      return false;
    }
    try {
      const res = await saveJobSummary(detail.job_id, editDraft);
      setProductContent(editDraft);
      queryClient.setQueryData<JobDetail>(["job-detail", detail.job_id], (old?: JobDetail) =>
        old ? { ...old, summary_edited_at: res.summary_edited_at } : old,
      );
      setEditing(false);
      editOriginalRef.current = null;
      const isMd = productKey === "summary" && isMarkdownPath(productPath);
      setProductRendered(isMd && Boolean(editDraft));
      queryClient.invalidateQueries({ queryKey: ["history"] });
      showToast("总结已保存");
      return true;
    } catch (e) {
      showToast(friendlyError(e, "保存失败"), { kind: "error" });
      return false;
    }
  }, [detail, editDraft, productKey, productPath, queryClient, showToast]);

  const exitEdit = useCallback(
    async (opts: { confirmDirty?: boolean } = {}): Promise<boolean> => {
      if (!editing) return true;
      if (opts.confirmDirty && dirty) {
        const choice = await askDirty();
        if (choice === "cancel") return false;
        if (choice === "save") {
          const saved = await saveSummary();
          if (!saved) return false;
        }
      }
      setEditing(false);
      editOriginalRef.current = null;
      return true;
    },
    [editing, dirty, askDirty, saveSummary],
  );

  const enterEdit = useCallback(async () => {
    if (!detail || detail.status !== "completed") return;
    if (productKey !== "summary" || !productContent) return;
    const ok = await exitEdit({ confirmDirty: true });
    if (!ok) return;
    editOriginalRef.current = productContent;
    setEditDraft(productContent);
    setEditing(true);
  }, [detail, productKey, productContent, exitEdit]);

  // ---- 重试（含换模板弹窗） ----
  const openRetryModal = useCallback(() => {
    if (!detail) return;
    setRetryModalOpen(true);
    const wanted = (detail.summary_template || "").trim();
    setRetryTpl(templateOptions.includes(wanted) ? wanted : templateOptions[0] || "");
  }, [detail, templateOptions]);

  const confirmRetry = useCallback(async () => {
    if (!detail) return;
    try {
      await retryJob(detail.job_id, retryTpl || undefined);
      showToast("已重新提交任务");
      setRetryModalOpen(false);
      queryClient.invalidateQueries({ queryKey: ["history"] });
      setView("status");
    } catch (e) {
      showToast(friendlyError(e, "重试失败"), { kind: "error" });
    }
  }, [detail, retryTpl, queryClient, setView, showToast]);

  // ---- 删除 ----
  const onDelete = useCallback(async () => {
    if (!detail) return;
    if (!window.confirm("确定删除该任务？将同时清理其所有产物文件（转写/总结/音频缓存），此操作不可恢复。")) return;
    try {
      await deleteJob(detail.job_id);
      forgetJob(detail.job_id);
      showToast("任务已删除");
      setCurrentHistoryJobId("");
      queryClient.invalidateQueries({ queryKey: ["jobs-count"] });
      window.dispatchEvent(new CustomEvent("vts:refresh-history"));
    } catch (e) {
      showToast(friendlyError(e, "删除失败"), { kind: "error" });
    }
  }, [detail, queryClient, setCurrentHistoryJobId, showToast]);

  // ---- 导出菜单：点菜单外关闭 ----
  useEffect(() => {
    if (!exportOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const wrap = (e.target as HTMLElement).closest(".export-wrap");
      if (!wrap) setExportOpen(false);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [exportOpen]);

  // ---- 导出 ----
  const runExport = useCallback(
    (format: string) => {
      if (!detail || !productPath) return;
      const url = jobExportUrl(detail.job_id, productPath, format);
      const trigger = () => triggerAnchorDownload(url);
      if (isDesktopApp() && desktopApi()?.export_product) {
        fetch(url, { method: "HEAD" })
          .then((res) => {
            const name = res.ok ? parseDownloadName(res.headers.get("content-disposition") || "") : "";
            return desktopApi()!.export_product!(url, name || "export");
          })
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
        // 旧包未带导出桥：回退真实 URL 下载（系统原生保存框）
        trigger();
        showToast("已触发导出：请在系统保存框选择保存位置并确认", { ttl: 5000 });
        return;
      }
      // 浏览器：先轻量 HEAD 取服务端生成的文件名，再触发下载，最后 toast 告知
      fetch(url, { method: "HEAD" })
        .then((res) => {
          let name = "";
          if (res.ok) name = parseDownloadName(res.headers.get("content-disposition") || "");
          trigger();
          showToast(
            name ? `已导出「${name}」，保存在浏览器下载目录（可在下载栏打开）` : "已触发导出：请在浏览器下载栏查看文件",
            { ttl: 6000 },
          );
        })
        .catch(() => {
          trigger();
          showToast("已触发导出：请在浏览器下载栏查看文件", { ttl: 4000 });
        });
    },
    [detail, productPath, showToast],
  );

  // ---- 标签行 ----
  const onSaveLabels = useCallback(async () => {
    if (!detail) return;
    try {
      const res = await setJobLabels(detail.job_id, labelDraft);
      showToast("标签已更新");
      setLabelsEditing(false);
      queryClient.setQueryData<JobDetail>(["job-detail", detail.job_id], (old?: JobDetail) =>
        old ? { ...old, labels: res.labels } : old,
      );
      window.dispatchEvent(new CustomEvent("vts:refresh-history"));
    } catch (e) {
      showToast(friendlyError(e, "保存标签失败"), { kind: "error" });
    }
  }, [detail, labelDraft, queryClient, showToast]);

  if (error) {
    // 任务被删除/不存在：显示占位（父级 HistoryView 的自动选中会切换到有效任务）
    return (
      <div id="historyDetailCard" className="history-detail-card">
        <div className="preview-body product-body">任务不存在或已被删除</div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div id="historyDetailCard" className="history-detail-card">
        <div className="preview-body product-body">加载中…</div>
      </div>
    );
  }

  const events = detail.progress || [];
  const knownEvents = events.filter((e) => e && isKnownEvent(e.event));
  const stages = computeStages(knownEvents, detail.status);
  const hasEvents = knownEvents.length > 0;
  const title = detail.title || jobId;
  const labels = Array.isArray(detail.labels) ? detail.labels : [];

  // 导出菜单项：按当前产物格式决定
  const ext = productPath.split("?")[0].split(".").pop()?.toLowerCase() || "";
  const exportItems: { format: string; label: string }[] = [];
  if (ext === "md" || ext === "markdown") exportItems.push({ format: "md", label: "导出 Markdown" });
  else if (ext === "txt") exportItems.push({ format: "md", label: "导出纯文本" });
  else if (ext === "srt") exportItems.push({ format: "md", label: "导出 SRT" });
  else exportItems.push({ format: "md", label: "导出" });

  const metaBits: React.ReactNode[] = [];
  if (detail.source_type === "local" && detail.source_path) {
    const name = detail.source_path.split(/[\\/]/).pop() || detail.source_path;
    metaBits.push(
      <span className="source-link source-local" title={detail.source_path} key="src">
        {name}
      </span>,
    );
  } else if (detail.source_url) {
    const u = detail.source_url;
    let display = u;
    try {
      const parsed = new URL(u);
      display = (parsed.hostname + parsed.pathname).slice(0, 48);
    } catch {
      /* 非标准 URL 原样展示 */
    }
    metaBits.push(
      /^https?:\/\//i.test(u) ? (
        <a className="source-link" href={u} target="_blank" rel="noopener noreferrer" title={u} key="src">
          {display}
        </a>
      ) : (
        <span className="source-link" title={u} key="src">
          {display}
        </span>
      ),
    );
  }
  metaBits.push(
    <span className="history-time" key="t">
      {formatTime(detail.created_at)}
    </span>,
  );
  const completedEvt = events.find((i) => i.event === "completed" && i.payload && typeof i.payload.elapsed === "number");
  if (completedEvt) metaBits.push(<span key="elapsed">耗时 {(completedEvt.payload!.elapsed as number).toFixed(1)}s</span>);
  if (detail.retry_count > 0) {
    metaBits.push(
      <span className="retry-badge" title={`最近重试：${formatTime(detail.retried_at)}`} key="retry">
        重试 {detail.retry_count} 次
      </span>,
    );
  }

  return (
    <div id="historyDetailCard">
      <div className="history-detail-head">
        <div className="history-detail-title-row">
          <h3 id="historyTitle" title={title}>
            {title}
          </h3>
          <span id="historyStatus" className={`job-status status-${detail.status}`}>
            {STATUS_LABELS[detail.status] || detail.status}
          </span>
          <div className="history-detail-actions">
            {terminal && (
              <button
                id="retryHistoryBtn"
                className={`secondary${terminal ? "" : " hidden"}`}
                onClick={() => void openRetryModal()}
              >
                {detail.status === "completed" ? "重新生成" : "重试"}
              </button>
            )}
            {terminal && (
              <button id="deleteHistoryBtn" className="secondary" onClick={() => void onDelete()}>
                删除
              </button>
            )}
          </div>
        </div>
        <div className="history-detail-meta" id="historyMeta">
          {metaBits}
        </div>
        <div className="job-labels-row" id="jobLabelsRow">
          {!labelsEditing &&
            (labels.length ? (
              labels.map((n) => (
                <span className="tag-pill" key={n}>
                  {n}
                </span>
              ))
            ) : (
              <span className="tag-pill tag-uncat">未分类</span>
            ))}
          {!labelsEditing && (
            <button type="button" className="secondary job-labels-edit" onClick={() => { setLabelDraft(labels); setLabelsEditing(true); }}>
              编辑标签
            </button>
          )}
          {labelsEditing && (
            <>
              <div style={{ flex: "1 1 100%" }}>
                <TagEditor initial={labelDraft} onChange={setLabelDraft} />
              </div>
              <button type="button" className="secondary" onClick={() => void onSaveLabels()}>
                保存
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  setLabelsEditing(false);
                }}
              >
                取消
              </button>
            </>
          )}
        </div>
      </div>

      <div id="jobNotice" className={`notice${noticeVisible(detail, knownEvents) ? "" : " hidden"}`}>
        {detail.status === "running" || detail.status === "pending" ? (
          <>
            <span>任务仍在进行中，产物要等流水线跑完才会出现。</span>
            <button type="button" className="secondary" onClick={() => setView("status")}>
              去任务状态
            </button>
          </>
        ) : detail.status === "completed" &&
          (knownEvents.some((e) => e.event === "summarize_skipped") || !paths.summary) ? (
          <>
            <span>
              本次仅生成转写文本，<strong>未生成总结</strong>——还没有配置 LLM 接入信息（API Key / 接入地址 / 模型名）。
            </span>
            <button type="button" className="secondary" onClick={() => setSettingsTab("tabLlm")}>
              去设置
            </button>
          </>
        ) : (
          <></>
        )}
      </div>

      {detail.status === "failed" && detail.error && (
        <div id="jobError">
          <JobError rawError={detail.error} onAction={(panels) => { setSettingsTab(panels[0] ?? "tabDefaults"); setView("settings"); }} />
        </div>
      )}

      {hasEvents && (
        <details id="processGroup" className="process-group">
          <summary>处理过程</summary>
          <div className="process-body">
            <div id="processStepper">
              <Stepper
                stages={visibleStagesFor(globalDefaults)}
                doneSet={stages.doneSet}
                skippedSet={stages.skippedSet}
                activeStage={stages.activeStage}
                failedStage={stages.failedStage}
              />
            </div>
            <div id="processLog" className="process-log">
              {loggableEvents(events).map((item, i) => (
                <div className="progress-item" key={i}>
                  <span>{eventLabel(item.event)}</span>
                  <span className="progress-detail">{eventDetailSuffix(item)}</span>
                </div>
              ))}
            </div>
          </div>
        </details>
      )}

      {hasProducts && (
        <div id="productToolbar" className="product-toolbar">
          <div className="tabs tabs-product" id="productTabs">
            {paths.summary && (
              <button
                type="button"
                className={`tab product-tab${productKey === "summary" ? " active" : ""}`}
                data-key="summary"
                aria-selected={productKey === "summary"}
                onClick={() => {
                  if (productKey === "summary") return;
                  void (async () => {
                    if (!(await exitEdit({ confirmDirty: true }))) return;
                    void loadProduct("summary");
                  })();
                }}
              >
                总结
              </button>
            )}
            {paths.transcript && (
              <button
                type="button"
                className={`tab product-tab${productKey === "transcript" ? " active" : ""}`}
                data-key="transcript"
                aria-selected={productKey === "transcript"}
                onClick={() => {
                  if (productKey === "transcript") return;
                  void (async () => {
                    if (!(await exitEdit({ confirmDirty: true }))) return;
                    void loadProduct("transcript");
                  })();
                }}
              >
                转写原文
              </button>
            )}
          </div>
          {hasMore && (
            <details id="moreGroup" className="more-group">
              <summary>更多</summary>
              <div className="more-items">
                {paths.segments && (
                  <button
                    type="button"
                    className="result-link more-item"
                    data-key="segments"
                    onClick={() => {
                      void (async () => {
                        if (!(await exitEdit({ confirmDirty: true }))) return;
                        void loadProduct("segments");
                      })();
                    }}
                  >
                    分段
                  </button>
                )}
                {paths.subtitle && (
                  <button
                    type="button"
                    className="result-link more-item"
                    data-key="subtitle"
                    onClick={() => {
                      void (async () => {
                        if (!(await exitEdit({ confirmDirty: true }))) return;
                        void loadProduct("subtitle");
                      })();
                    }}
                  >
                    字幕(SRT)
                  </button>
                )}
              </div>
            </details>
          )}
          <div className="product-actions">
            <span
              id="summaryEditedBadge"
              className={`edited-badge${productKey === "summary" && summaryEditedAt && summaryEditedAt > 0 ? "" : " hidden"}`}
              title={summaryEditedAt && summaryEditedAt > 0 ? `编辑时间：${formatTime(summaryEditedAt)}` : ""}
            >
              {summaryEditedAt && summaryEditedAt > 0 ? `已编辑 · ${formatTime(summaryEditedAt)}` : ""}
            </span>
            <button
              id="toggleProductBtn"
              className={`secondary${productContent && productKey === "summary" && isMarkdownPath(productPath) ? "" : " hidden"}`}
              onClick={() => setProductRendered((r) => !r)}
            >
              {productRendered ? "原文" : "渲染"}
            </button>
            <button
              id="editProductBtn"
              className={`secondary${editing ? " hidden" : ""}`}
              style={
                productKey === "summary" && productContent && detail.status === "completed" && !editing
                  ? undefined
                  : { display: "none" }
              }
              onClick={() => void enterEdit()}
            >
              编辑
            </button>
            <div className="export-wrap">
              <button
                id="exportProductBtn"
                className="secondary"
                disabled={!productContent || editing}
                onClick={(e) => {
                  e.stopPropagation();
                  if (!productContent || editing) return;
                  setExportOpen((v) => !v);
                }}
              >
                导出
              </button>
              {exportOpen && (
                <div id="exportMenu" className="export-menu" role="menu" aria-label="选择导出格式">
                  {exportItems.map((item) => (
                    <button
                      type="button"
                      className="export-menu-item"
                      role="menuitem"
                      data-format={item.format}
                      key={item.format}
                      onClick={() => {
                        setExportOpen(false);
                        runExport(item.format);
                      }}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {editing && (
        <div id="productEditBar" className="product-edit-bar">
          <button id="saveProductBtn" className="primary" onClick={() => void saveSummary()}>
            保存
          </button>
          <button id="cancelProductBtn" className="ghost" onClick={() => void exitEdit({ confirmDirty: true })}>
            取消
          </button>
          <span className="maskhint">编辑会覆盖总结产物；重试 / 重新生成后编辑稿失效</span>
        </div>
      )}
      <textarea
        id="productEditor"
        className={`product-editor${editing ? "" : " hidden"}`}
        spellCheck={false}
        aria-label="编辑总结内容"
        value={editing ? editDraft : ""}
        onChange={(e) => setEditDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape" && editing) void exitEdit({ confirmDirty: true });
        }}
      />

      <div
        className={`preview-body product-body${productKey === "summary" && productRendered && productContent ? " rendered" : ""}`}
        id="productBody"
        data-testid="product-body"
      >
        {loadingProduct ? (
          "加载中…"
        ) : productKey === "summary" && productRendered && productContent ? (
          <span dangerouslySetInnerHTML={{ __html: renderMarkdown(productContent) }} />
        ) : (
          productContent || emptyProductText(detail)
        )}
      </div>

      {/* 未保存修改弹窗 */}
      <Modal open={dirtyChoiceOpen} onClose={() => closeDirty("cancel")} ariaLabel="有未保存的修改">
        <div id="summaryEditModal" className="modal-card-inner">
          <h3>有未保存的修改</h3>
          <p className="modal-hint">总结编辑内容尚未保存，选择如何处理？</p>
          <div className="modal-actions">
            <button id="summaryEditDiscardBtn" className="secondary" onClick={() => closeDirty("discard")}>
              放弃修改
            </button>
            <button id="summaryEditCancelBtn" className="secondary" onClick={() => closeDirty("cancel")}>
              取消
            </button>
            <button id="summaryEditSaveBtn" onClick={() => closeDirty("save")}>
              保存并继续
            </button>
          </div>
        </div>
      </Modal>

      {/* 重试换模板弹窗 */}
      <Modal open={retryModalOpen} onClose={() => setRetryModalOpen(false)} ariaLabel="重试模板选择">
        <div id="retryTplModal" className="modal-card-inner">
          <h3 id="retryTplModalTitle">{detail.status === "completed" ? "重新生成" : "重试"}</h3>
          <p className="modal-hint">选择本次使用的总结模板；任务标签与源信息保持不变。</p>
          <select
            id="retryTplModalSelect"
            autoComplete="off"
            value={retryTpl}
            onChange={(e) => setRetryTpl(e.target.value)}
          >
            {templateOptions.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <div className="modal-actions">
            <button id="retryTplCancelBtn" className="secondary" onClick={() => setRetryModalOpen(false)}>
              取消
            </button>
            <button id="retryTplConfirmBtn" onClick={() => void confirmRetry()}>
              {detail.status === "completed" ? "确认重新生成" : "确认重试"}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

function noticeVisible(detail: JobDetail, knownEvents: { event: string }[]): boolean {
  if (detail.status === "running" || detail.status === "pending") return true;
  if (detail.status === "completed") {
    const summarySkipped = knownEvents.some((e) => e.event === "summarize_skipped");
    const hasSummary = !!(detail.result_paths && detail.result_paths.summary);
    if (summarySkipped || !hasSummary) return true;
  }
  return false;
}

function visibleStagesFor(globalDefaults: { subtitle_preference?: string } | null) {
  const subtitleOff = globalDefaults?.subtitle_preference === "off";
  return STATUS_STAGES.filter((s) => !(s.key === "subtitle" && subtitleOff));
}

function emptyProductText(detail: JobDetail): string {
  if (detail.status === "completed") return "本次任务没有生成任何产物文件。";
  if (detail.status === "running" || detail.status === "pending") return "任务进行中，产物尚未生成。";
  return "";
}

