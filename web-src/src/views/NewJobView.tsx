// ============ 新建总结视图 ============
// 创建表单（URL/本地文件 + 模板选择 + 标题 + 标签）、最近来源历史、
// 本地文件辅助（原生选择器 + 目录浏览面板）、重复来源软提示、首跑引导卡。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { browseFs, createJob, fetchJobs, fetchSettings, fetchTemplates, pickFile, type CreateJobPayload } from "../api/endpoints";
import { desktopApi } from "../lib/desktop";
import { RECENT_PATHS_KEY, RECENT_URLS_KEY } from "../lib/constants";
import { rememberJob, rememberLastLabels, rememberRecentSource, storedLastLabels } from "../lib/recent";
import { friendlyError } from "../lib/format";
import { requestNotifyIfNeeded } from "../lib/notify";
import { useAppStore } from "../store/useAppStore";
import { useToast } from "../components/Toast";
import { TagEditor } from "../components/TagEditor";
import { RecentSelect } from "../components/RecentSelect";

type SourceTab = "url" | "local";

// 本会话内已确认过「仍要创建」的 URL：同一 URL 不反复打扰（模块级，跨视图切换保留）
const warnedUrls = new Set<string>();

export function NewJobView() {
  const { showToast } = useToast();
  const setView = useAppStore((s) => s.setView);
  const setSettingsTab = useAppStore((s) => s.setSettingsTab);
  const queryClient = useQueryClient();

  const [sourceTab, setSourceTab] = useState<SourceTab>("url");
  const [url, setUrl] = useState("");
  const [audioPath, setAudioPath] = useState("");
  const [title, setTitle] = useState("");
  const [labels, setLabels] = useState<string[]>(storedLastLabels());
  const [submitting, setSubmitting] = useState(false);
  const [warn, setWarn] = useState<{ count: number } | null>(null);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [fsState, setFsState] = useState<{ loading: boolean; error: string | null }>({ loading: false, error: null });

  // 模板下拉：全部模板（内置声明序在前，首个即「通用」），预选全局默认模板
  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: fetchTemplates });
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const templates = templatesData?.templates ?? [];
  const preferredTemplate = useMemo(() => {
    if (settings?.summary_template && templates.includes(settings.summary_template)) {
      return settings.summary_template;
    }
    return templates[0] ?? "";
  }, [settings, templates]);

  // 是否有历史任务（首跑引导卡显隐）
  const { data: hasAnyJobs } = useQuery({
    queryKey: ["jobs-count"],
    queryFn: async () => {
      const page = await fetchJobs({ limit: 1 });
      return page.total > 0;
    },
  });

  // 配置引导卡：未配置 LLM 时标 muted（可跳过，字幕优先路径零成本）
  const llmConfigured = useAppStore((s) => s.health?.llm_configured === true);

  // B 站字幕登录态提示：URL 命中 B 站且未配置任何 cookies 来源时温和引导（非拦截）
  const [biliHintDismissed, setBiliHintDismissed] = useState(false);
  const showBiliCookieHint =
    sourceTab === "url" &&
    !biliHintDismissed &&
    /\b(bilibili\.com|b23\.tv)\//i.test(url) &&
    !(settings?.cookies_browser ?? "");

  const createMutation = useMutation({
    mutationFn: (payload: CreateJobPayload) => createJob(payload),
    onSuccess: async (data) => {
      rememberRecentSource(RECENT_URLS_KEY, url);
      rememberRecentSource(RECENT_PATHS_KEY, audioPath);
      rememberJob(data.job_id);
      rememberLastLabels(labels);
      queryClient.invalidateQueries({ queryKey: ["jobs-count"] });
      queryClient.invalidateQueries({ queryKey: ["history"] });
      setView("status");
      // 已有任务在跑时明确告知「只是排队」
      try {
        const page = await fetchJobs({ limit: 200 });
        const others = page.jobs.filter(
          (j) => j.job_id !== data.job_id && (j.status === "running" || j.status === "pending"),
        );
        if (others.length) {
          showToast(`已加入队列，当前还有 ${others.length} 个任务在后台运行`, { ttl: 4000 });
        }
      } catch {
        /* ignore */
      }
    },
    onError: (e) => {
      showToast(friendlyError(e, "创建任务失败"), { kind: "error", ttl: 5000 });
    },
  });

  const loadDir = useCallback(
    async (path: string) => {
      setFsState({ loading: true, error: null });
      try {
        const data = await browseFs(path);
        setFsData(data);
        setFsState({ loading: false, error: data.error });
      } catch {
        setFsState({ loading: false, error: "无法访问目录" });
      }
    },
    [],
  );

  const [fsData, setFsData] = useState<Awaited<ReturnType<typeof browseFs>> | null>(null);

  const onBrowseClick = useCallback(async () => {
    // 桌面形态：pywebview 原生桥（前端直接调用，拿到绝对路径）
    const api = desktopApi();
    if (api?.pick_file) {
      try {
        const picked = await api.pick_file();
        if (picked) setAudioPath(String(picked));
      } catch {
        /* ignore */
      }
      return;
    }
    // Web 形态：优先后端原生系统弹框（POST /api/v1/fs/pick）→ 失败回退目录面板
    try {
      const data = await pickFile();
      if (data.path) {
        setAudioPath(data.path);
        return;
      }
      if (!data.error) return; // 用户取消对话框，不展开面板
    } catch {
      /* 网络异常回退面板 */
    }
    // 回退目录浏览面板：从输入框当前值（或其所在目录）起步，否则回到用户主目录
    setBrowserOpen(true);
    const val = audioPath.trim() || "";
    const sep = val.includes("\\") ? "\\" : "/";
    const start = val.split(/[\\/]/).slice(0, -1).join(sep);
    void loadDir(start);
  }, [audioPath, loadDir]);


  // 模板下拉当前值（受控）。为避免「重建下拉丢失选择」，模板选项加载后不重排用户选择。
  const [templateValue, setTemplateValue] = useState<string>("");
  useEffect(() => {
    if (templates.length && !templates.includes(templateValue)) {
      setTemplateValue(preferredTemplate);
    }
  }, [templates, preferredTemplate, templateValue]);

  const countDuplicateSource = useCallback(async (q: string): Promise<number> => {
    const norm = (q || "").trim().replace(/\/+$/, "");
    if (!norm) return 0;
    try {
      const page = await fetchJobs({ q: norm, limit: 1 });
      return page.total || 0;
    } catch {
      return 0;
    }
  }, []);

  const doSubmit = useCallback(
    (urlVal: string, pathVal: string) => {
      const payload: CreateJobPayload = {
        source_type: sourceTab,
        url: urlVal,
        audio_path: pathVal,
        title: title.trim(),
        labels,
        summary_template: templateValue || "",
      };
      setWarn(null);
      setSubmitting(true);
      requestNotifyIfNeeded();
      createMutation.mutate(payload, {
        onSettled: () => setSubmitting(false),
      });
    },
    [sourceTab, url, audioPath, title, labels, templateValue, createMutation],
  );

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const urlVal = url.trim();
      const pathVal = audioPath.trim();
      if (sourceTab === "url" && !urlVal) {
        showToast("请填写视频 URL", { kind: "error" });
        return;
      }
      if (sourceTab === "local" && !pathVal) {
        showToast("请填写音频/视频路径", { kind: "error" });
        return;
      }

      // A5 重复来源软提示：命中历史同源 URL 时警示但不阻断
      if (sourceTab === "url" && !warnedUrls.has(urlVal)) {
        const dupCount = await countDuplicateSource(urlVal);
        if (dupCount > 0) {
          setWarn({ count: dupCount });
          return; // 等用户在警告里选择
        }
      }

      void doSubmit(urlVal, pathVal);
    },
    [sourceTab, url, audioPath, labels, title, templateValue, doSubmit],
  );

  const allReady = llmConfigured;
  const jobEmptyHidden = allReady && (hasAnyJobs ?? false);

  return (
    <section id="view-new" className="view">
      <header className="view-head">
        <h2>新建总结</h2>
        <p>粘贴视频链接或选择本地文件；总结模板可按任务选择，其余配置来自全局默认。</p>
      </header>

      <section className="panel new-panel">
        <div className="tabs tabs-source">
          <button type="button" className={`tab${sourceTab === "url" ? " active" : ""}`} data-panel="tabUrl" onClick={() => setSourceTab("url")}>
            URL
          </button>
          <button type="button" className={`tab${sourceTab === "local" ? " active" : ""}`} data-panel="tabLocal" onClick={() => setSourceTab("local")}>
            本地文件
          </button>
        </div>

        <form id="jobForm" onSubmit={(e) => void onSubmit(e)}>
          <div className={`tab-content${sourceTab === "url" ? "" : " hidden"}`} id="tabUrl">
            <label className="field">
              视频 URL
              <input id="url" autoComplete="off" placeholder="https://www.bilibili.com/video/..." value={url} onChange={(e) => setUrl(e.target.value)} />
            </label>
            <div className="local-tools">
              <RecentSelect
                id="recentUrlSelect"
                placeholder="最近 URL…"
                storageKey={RECENT_URLS_KEY}
                onPick={(v) => setUrl(v)}
              />
            </div>
            {showBiliCookieHint && (
              <div id="biliCookieHint" className="submit-warnings">
                <div className="warn-item">
                  <span>
                    B 站 AI/CC 字幕通常需要登录才能获取。可在 设置 → 任务默认 里开启浏览器 Cookies；无字幕视频仍可直接下载转写。
                  </span>
                  <span>
                    <button
                      type="button"
                      className="secondary warn-view"
                      onClick={() => {
                        setSettingsTab("tabDefaults");
                        setView("settings");
                      }}
                    >
                      去设置
                    </button>
                    <button type="button" className="secondary warn-proceed" onClick={() => setBiliHintDismissed(true)}>
                      知道了
                    </button>
                  </span>
                </div>
              </div>
            )}
          </div>

          <div className={`tab-content${sourceTab === "local" ? "" : " hidden"}`} id="tabLocal">
            <label className="field">
              音频/视频路径
              <input id="audioPath" autoComplete="off" placeholder="/Users/fan/Desktop/video.mp4" value={audioPath} onChange={(e) => setAudioPath(e.target.value)} />
            </label>
            <div className="local-tools">
              <button type="button" id="browseFileBtn" className="secondary" onClick={() => void onBrowseClick()}>
                浏览…
              </button>
              <RecentSelect
                id="recentPathSelect"
                placeholder="最近路径…"
                storageKey={RECENT_PATHS_KEY}
                onPick={(v) => setAudioPath(v)}
              />
            </div>
            {browserOpen && (
              <div id="webFileBrowser" className="file-browser">
                {fsState.loading && <div className="file-browser-status">加载中…</div>}
                {!fsState.loading && fsState.error && <div className="file-browser-status">{fsState.error}</div>}
                {!fsState.loading && !fsState.error && fsData && (
                  <>
                    <div className="file-browser-head">
                      <button
                        type="button"
                        className="secondary fb-back"
                        disabled={!fsData.parent}
                        onClick={() => fsData.parent && void loadDir(fsData.parent)}
                      >
                        ⬆ 上一级
                      </button>
                      <span className="fb-path mono" title={fsData.path}>
                        {fsData.path.length > 48 ? `…${fsData.path.slice(-47)}` : fsData.path}
                      </span>
                    </div>
                    <div className="fb-list">
                      {fsData.dirs.length === 0 && fsData.files.length === 0 && (
                        <div className="file-browser-status">此目录没有音频/视频文件</div>
                      )}
                      {fsData.dirs.map((d) => (
                        <div
                          key={d.path}
                          className="fb-row fb-dir"
                          data-path={d.path}
                          title={d.path}
                          role="button"
                          tabIndex={0}
                          onClick={() => void loadDir(d.path)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") void loadDir(d.path);
                          }}
                        >
                          📁 {d.name}
                        </div>
                      ))}
                      {fsData.files.map((f) => (
                        <div
                          key={f.path}
                          className="fb-row fb-file"
                          data-path={f.path}
                          title={f.path}
                          role="button"
                          tabIndex={0}
                          onClick={() => {
                            setAudioPath(f.path);
                            setBrowserOpen(false);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              setAudioPath(f.path);
                              setBrowserOpen(false);
                            }
                          }}
                        >
                          🎬 {f.name}
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
            <label className="field">
              标题
              <input id="title" autoComplete="off" placeholder="可选标题" value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
          </div>

          <label className="field">
            总结模板
            <select id="jobTemplate" autoComplete="off" value={templateValue} onChange={(e) => setTemplateValue(e.target.value)}>
              {templates.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>

          <div className="field">
            <span className="field-label-text">标签（可选）</span>
            <div id="jobLabelsEditor">
              <TagEditor initial={labels} onChange={setLabels} />
            </div>
          </div>

          <p className="form-hint">总结模板可按任务单独选择（默认跟随「设置 → 任务默认」）；其余转写/总结配置统一在全局默认中管理。</p>

          {warn && (
            <div id="submitWarnings" className="submit-warnings">
              <div className="warn-item">
                <span>历史任务中已有 {warn.count} 条同源来源，重复创建会生成重复笔记。</span>
                <span>
                  <button
                    type="button"
                    className="secondary warn-view"
                    onClick={() => {
                      setWarn(null);
                      setView("history");
                    }}
                  >
                    查看历史
                  </button>
                  <button
                    type="button"
                    className="secondary warn-proceed"
                    onClick={() => {
                      warnedUrls.add(url.trim());
                      setWarn(null);
                      void doSubmit(url.trim(), audioPath.trim());
                    }}
                  >
                    仍要创建任务
                  </button>
                </span>
              </div>
            </div>
          )}

          <div className="actions">
            <button type="submit" id="submitBtn" disabled={submitting}>
              {submitting ? "创建中…" : "创建任务"}
            </button>
          </div>
        </form>
      </section>

      <section className="panel onboard-panel">
        <div id="jobEmpty" className="empty-state" style={jobEmptyHidden ? { display: "none" } : undefined}>
          <div className="empty-hero">
            <div className="empty-ico">🎬</div>
            <div className="empty-title">从一条视频链接开始</div>
            <p className="empty-hint">完成下面的准备步骤，就能生成第一个总结。</p>
          </div>
          <ul id="onboardingChecklist" className="onboarding">
            <li className={llmConfigured ? "done" : "muted"}>
              <span className="tick">{llmConfigured ? "✓" : "1"}</span>
              <span className="ob-text">
                {llmConfigured
                  ? "已配置 LLM 接入（BYOK）"
                  : "配置 LLM 接入：API Key / 接入地址 / 模型名（可跳过——视频自带字幕也能出转写）"}
              </span>
              {!llmConfigured && (
                <button type="button" className="secondary" onClick={() => { setSettingsTab("tabLlm"); setView("settings"); }}>
                  去设置
                </button>
              )}
            </li>
            <li className="muted">
              <span className="tick">2</span>
              <span className="ob-text">在上方粘贴视频链接，创建第一个任务</span>
            </li>
          </ul>
        </div>
      </section>
    </section>
  );
}
