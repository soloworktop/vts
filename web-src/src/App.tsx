import { LEGACY_IMPORT_ACK_KEY } from "./lib/constants";
// ============ 应用外壳：侧栏导航 + 视图切换 + 全局引导 ============
import { useCallback, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchHealth, fetchJobs } from "./api/endpoints";
import { notifyTerminal, requestNotifyIfNeeded } from "./lib/notify";
import { THEME_ICONS, THEME_NAMES, cycleTheme, initTheme, storedThemeMode, type ThemeMode } from "./lib/theme";
import { useAppStore, type ViewName } from "./store/useAppStore";
import { useToast } from "./components/Toast";
import { NewJobView } from "./views/NewJobView";
import { StatusView } from "./views/StatusView";
import { HistoryView } from "./views/HistoryView";
import { SettingsView } from "./views/SettingsView";

const VIEWS: { key: ViewName; icon: string; label: string }[] = [
  { key: "new", icon: "🎬", label: "新建总结" },
  { key: "status", icon: "📡", label: "任务状态" },
  { key: "history", icon: "🗂️", label: "历史任务" },
  { key: "settings", icon: "⚙️", label: "设置" },
];

function parseHistoryHash(): string | null {
  const m = window.location.hash.match(/^#history\/([0-9a-fA-F]+)/);
  return m ? m[1] : null;
}

export function App() {
  const view = useAppStore((s) => s.view);
  const setView = useAppStore((s) => s.setView);
  const setHealth = useAppStore((s) => s.setHealth);
  const health = useAppStore((s) => s.health);
  const runningCount = useAppStore((s) => s.runningCount);
  const setRunningCount = useAppStore((s) => s.setRunningCount);
  const setCurrentHistoryJobId = useAppStore((s) => s.setCurrentHistoryJobId);
  const setSettingsTab = useAppStore((s) => s.setSettingsTab);
  const { showToast } = useToast();

  const [themeMode, setThemeMode] = useState<ThemeMode>(storedThemeMode());
  const [guideOpen, setGuideOpen] = useState(false);
  // 使用手册弹层：Esc 关闭（该弹层未走 Modal 组件，需单独处理）
  useEffect(() => {
    if (!guideOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setGuideOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [guideOpen]);

  // ---- 启动：健康 ----
  const { data: healthData } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 60_000,
  });
  useEffect(() => {
    if (healthData) setHealth(healthData);
  }, [healthData, setHealth]);

  // ---- 主题 ----
  useEffect(() => initTheme(), []);

  const onThemeToggle = useCallback(() => {
    const next = cycleTheme();
    setThemeMode(next);
  }, []);

  // ---- 历史深链路由：#history/<id> ----
  useEffect(() => {
    const initial = parseHistoryHash();
    if (initial) {
      setCurrentHistoryJobId(initial);
      setView("history");
    }
  }, [setCurrentHistoryJobId, setView]);

  useEffect(() => {
    const onHash = () => {
      const id = parseHistoryHash();
      if (id) {
        setCurrentHistoryJobId(id);
        setView("history");
      }
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [setCurrentHistoryJobId, setView]);

  // ---- 活跃任务看门狗：有 running/pending 时每 5s 刷新历史 + 更新运行徽标 ----
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const tick = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const res = await fetchJobs({ limit: 200 });
        const active = res.jobs.filter((j) => j.status === "running" || j.status === "pending");
        setRunningCount(active.length);
        if (active.length > 0) {
          window.dispatchEvent(new CustomEvent("vts:refresh-history"));
        }
      } catch {
        /* 网络抖动忽略，下个周期重试 */
      }
    };
    timer = setInterval(tick, 5000);
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [setRunningCount]);

  // ---- legacy JSON 一次性迁移提示 ----
  useEffect(() => {
    const info = health?.config_import;
    if (!info || typeof info !== "object") return;
    const entries = Object.values(info).filter((e) => e && typeof e === "object");
    const count = entries.reduce((sum, e) => sum + (Number((e as { count?: number }).count) || 0), 0);
    if (!count) return;
    const latestAt = Math.max(0, ...entries.map((e) => Number((e as { at?: number }).at) || 0));
    const ackKey = LEGACY_IMPORT_ACK_KEY;
    if (String(latestAt) && localStorage.getItem(ackKey) === String(latestAt)) return;
    if (latestAt) localStorage.setItem(ackKey, String(latestAt));
    showToast(`已从旧版配置文件导入 ${count} 条配置（API Key 已加密保存）`, { ttl: 8000 });
  }, [health, showToast]);

  // ---- 完成迁移检测（toast + 系统通知）由 StatusView 轮询负责 ----

  const goSettings = (tab: string) => {
    setSettingsTab(tab);
    setView("settings");
  };

  const healthOk = health?.status === "ok";
  const llmConfigured = health?.llm_configured === true;
  const dbNewer = health?.db_newer_version;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-logo">▶</div>
          <div className="brand-text">
            <b>VTS</b>
            <span>视频 → 结构化 Markdown 笔记</span>
          </div>
        </div>

        <nav className="nav" id="sideNav">
          {VIEWS.map((v) => (
            <button
              key={v.key}
              className={`nav-item${view === v.key ? " active" : ""}`}
              data-view={v.key}
              aria-current={view === v.key ? "page" : "false"}
              onClick={() => setView(v.key)}
            >
              <i className="nav-ico">{v.icon}</i>
              <span>{v.label}</span>
              {v.key === "status" && (
                <em
                  id="runningPill"
                  className={`nav-badge${runningCount ? "" : " hidden"}`}
                  title={runningCount ? `${runningCount} 个任务进行中` : ""}
                >
                  {String(runningCount || "")}
                </em>
              )}
            </button>
          ))}
        </nav>

        <div className="side-foot">
          <button
            className={`foot-item foot-status ${healthOk ? (llmConfigured ? "foot-status-ok" : "foot-status-warn") : "foot-status-err"}`}
            id="envStatus"
            type="button"
            title={healthOk ? (llmConfigured ? "点击进入设置查看 / 修改 LLM 接入信息" : "点击进入设置配置 LLM；未配置时仍可凭视频自带字幕生成转写文本") : ""}
            onClick={() => goSettings("tabLlm")}
          >
            <span className="foot-status-dot"></span>
            <span className="foot-status-text">
              {!healthOk ? "服务不可用" : llmConfigured ? "服务正常 · LLM 已配置" : "服务正常 · LLM 未配置（BYOK）"}
            </span>
          </button>
          <button className="foot-item" id="guideBtn" type="button" title="打开使用手册" onClick={() => setGuideOpen(true)}>
            <span className="foot-ico">📖</span>
            <span>使用指引</span>
          </button>
          <button
            className="foot-item foot-theme"
            id="themeToggle"
            type="button"
            title={`主题：${THEME_NAMES[themeMode]}（点击切换）`}
            onClick={onThemeToggle}
          >
            <span className="foot-ico" id="themeIcon">{THEME_ICONS[themeMode]}</span>
            <span>主题：</span>
            <span className="theme-val" id="themeLabel">{THEME_NAMES[themeMode]}</span>
          </button>
          {dbNewer && (
            <div id="dbNewerBanner" className="sidebar-warn">
              ⚠️ 本地数据由更新版本创建（schema v{dbNewer}），请升级 App 后再继续使用
            </div>
          )}
          <div className="foot-ver">
            <span className="foot-sep" aria-hidden="true"></span>
            <span className="side-ver mono" id="appVersion" title="应用版本号（发布构建 = git tag；开发态 = git describe）">
              {health?.version ? `v${String(health.version).replace(/^v/, "")}` : ""}
            </span>
          </div>
        </div>
      </aside>

      <main className="views">
        {view === "new" && <NewJobView />}
        {view === "status" && <StatusView />}
        {view === "history" && <HistoryView />}
        {view === "settings" && <SettingsView />}
      </main>

      {/* 使用手册弹层 */}
      {guideOpen && (
        <div className="modal-overlay" id="guideOverlay" role="dialog" aria-modal="true" aria-label="用户使用手册" onMouseDown={(e) => { if (e.target === e.currentTarget) setGuideOpen(false); }}>
          <div className="guide-card">
            <div className="guide-head">
              <b>📖 使用手册</b>
              <button id="guideCloseBtn" className="secondary" type="button" onClick={() => setGuideOpen(false)}>
                关闭
              </button>
            </div>
            <iframe id="guideFrame" title="用户使用手册" src={`/guide?theme=${encodeURIComponent(storedThemeMode())}`} />
          </div>
        </div>
      )}
    </div>
  );
}

// 暴露给 e2e 的初始化完成标记（goto_console 依赖）
export { requestNotifyIfNeeded, notifyTerminal };
