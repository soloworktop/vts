// ============ 设置视图：任务默认 / LLM 配置（BYOK）/ 总结模板 / 标签管理 ============
import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createLabel,
  deleteLabel,
  deleteTemplate,
  fetchLabels,
  fetchLlmConfig,
  fetchSettings,
  fetchTemplate,
  fetchTemplates,
  importLlmFromEnv,
  logsExportUrl,
  mergeLabels,
  renameLabel,
  saveTemplate,
  testCookies,
  updateLlmConfig,
  updateSettings,
} from "../api/endpoints";
import type { LlmConfig, LlmSlotKey, Settings } from "../api/types";
import { friendlyError } from "../lib/format";
import { desktopApi, isDesktopApp, triggerAnchorDownload } from "../lib/desktop";
import { useAppStore } from "../store/useAppStore";
import { useToast } from "../components/Toast";

const TABS = [
  { id: "tabDefaults", label: "任务默认" },
  { id: "tabLlm", label: "LLM 配置" },
  { id: "tabTemplate", label: "总结模板" },
  { id: "tabLabels", label: "标签管理" },
];

export function SettingsView() {
  const settingsTab = useAppStore((s) => s.settingsTab);
  const setSettingsTab = useAppStore((s) => s.setSettingsTab);
  const { showToast } = useToast();

  return (
    <section id="view-settings" className="view">
      <header className="view-head view-head-actions">
        <div className="view-head-text">
          <h2>设置</h2>
          <p>LLM 接入（BYOK）、任务默认配置、总结模板与标签管理。</p>
        </div>
        <div className="view-head-tools">
          <button
            id="exportLogsBtn"
            type="button"
            className="secondary"
            title="导出脱敏诊断日志，供技术人员分析"
            onClick={() => {
              const url = logsExportUrl();
              if (isDesktopApp() && desktopApi()?.export_product) {
                desktopApi()!
                  .export_product!(url, "video-to-summary-logs.txt")
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
                    showToast(`已导出到「${r.path}」`, { ttl: 9000 });
                  })
                  .catch(() => showToast("导出失败：请重试", { kind: "error" }));
              } else {
                // 真实附件下载（禁 Blob）：HEAD 取文件名 → 触发 → toast
                fetch(url)
                  .then((res) => {
                    const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || "video-to-summary-logs.txt";
                    triggerAnchorDownload(url);
                    showToast(`已导出「${name}」，保存在浏览器下载目录（可在下载栏打开）`, { ttl: 6000 });
                  })
                  .catch(() => {
                    triggerAnchorDownload(url);
                    showToast("已触发导出：请在浏览器下载栏查看文件", { ttl: 4000 });
                  });
              }
            }}
          >
            导出日志
          </button>
          <span className="export-hint">遇到问题？导出日志发给技术人员（已自动脱敏）</span>
        </div>
      </header>

      <section className="panel settings-panel">
        <div className="tabs tabs-sub">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`tab${settingsTab === t.id ? " active" : ""}`}
              data-panel={t.id}
              aria-selected={settingsTab === t.id}
              onClick={() => setSettingsTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {settingsTab === "tabDefaults" && <DefaultsTab />}
        {settingsTab === "tabLlm" && <LlmTab />}
        {settingsTab === "tabTemplate" && <TemplateTab />}
        {settingsTab === "tabLabels" && <LabelsTab />}
      </section>
    </section>
  );
}

// ---------------- 任务默认 ----------------
function DefaultsTab() {
  const queryClient = useQueryClient();
  const setGlobalDefaults = useAppStore((s) => s.setGlobalDefaults);

  const { data: settings, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const s = await fetchSettings();
      return s;
    },
  });

  const [form, setForm] = useState<Partial<Settings>>({});
  useEffect(() => {
    if (settings) {
      setForm(settings);
      setGlobalDefaults(settings);
    }
  }, [settings, setGlobalDefaults]);

  const [status, setStatus] = useState("");
  const [cookieStatus, setCookieStatus] = useState("");

  const saveMutation = useMutation({
    mutationFn: (payload: Partial<Settings>) => updateSettings(payload),
    onSuccess: (s) => {
      setGlobalDefaults(s);
      setStatus("已保存");
      queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e) => {
      setStatus(friendlyError(e, "保存失败"));
    },
  });

  const onSave = () => {
    setStatus("保存中…");
    const payload: Partial<Settings> = {
      // 以下键不再由 UI 管理（走内置默认），省略键，后端只更新提交的键
      subtitle_preference: form.subtitle_preference ?? "auto",
      cookies_browser: form.cookies_browser ?? "",
      proxy: (form.proxy || "").trim(),
    };
    saveMutation.mutate(payload);
  };

  const onTestCookies = async () => {
    const browser = form.cookies_browser || "";
    if (!browser) {
      setCookieStatus("请先选择浏览器");
      return;
    }
    setCookieStatus("读取中…（若弹出钥匙串授权请点「允许」）");
    try {
      const data = await testCookies(browser);
      if (data.ok === false) {
        setCookieStatus(`读取失败：${data.error || "未知错误"}`);
        return;
      }
      const yt = (data.domains || []).find(([d]) => d === "youtube.com");
      const bili = (data.domains || []).find(([d]) => d === "bilibili.com");
      const bits: string[] = [`已读取 ${data.total} 条 cookies`];
      if (yt) {
        bits.push(`YouTube ${yt[1]} 条${data.youtube_login_hint ? "（含登录态 ✓）" : "（未见登录态）"}`);
      }
      // bilibili_login_hint 不依赖 domains 列表（后者只回 top-8，可能被截断）
      if (data.bilibili_login_hint) {
        bits.push(bili ? `bilibili.com ${bili[1]} 条（B 站登录态 ✓）` : "B 站登录态 ✓");
      } else if (bili) {
        bits.push(`bilibili.com ${bili[1]} 条（未见 B 站登录）`);
      }
      if (!yt && !bili && !data.bilibili_login_hint) {
        bits.push("未发现 youtube.com / bilibili.com（可能未在该浏览器登录）");
      }
      setCookieStatus(bits.join("，"));
    } catch (e) {
      setCookieStatus(friendlyError(e, "读取失败"));
    }
  };

  return (
    <div className="tab-content" id="tabDefaults">
      <p className="defaults-intro">
        本版为 BYOK：转写与总结使用你自己配置的 OpenAI 兼容端点（见「LLM 配置」）。总结模板可在创建任务时按次选择；以下为连接与行为偏好。
      </p>

      <section className="settings-group">
        <div className="settings-group-head">
          <h3>📝 字幕策略</h3>
          <p>视频自带字幕时跳过「下载音频 + 转写」，更快更省。</p>
        </div>
        <div className="settings-group-body">
          <label>
            视频自带字幕优先
            <select
              id="defaultSubtitlePreference"
              value={form.subtitle_preference ?? "auto"}
              disabled={isLoading}
              onChange={(e) => setForm((f) => ({ ...f, subtitle_preference: e.target.value }))}
            >
              <option value="auto">有字幕就用（人工优先，没有则转写）</option>
              <option value="manual_only">仅用人工字幕（没有则转写）</option>
              <option value="off">关闭（始终下载+转写）</option>
            </select>
          </label>
          <details className="hint-more">
            <summary>详细说明</summary>
            <p>
              开启后，视频自带字幕会直接用作转写文本，跳过「下载音频 + 语音转写」，更快更省。需要登录才能取字幕的站点，可在「网络与访问」里配置浏览器 cookies。
            </p>
          </details>
        </div>
      </section>

      <details className="settings-group settings-group-collapsible">
        <summary className="settings-group-head">
          <h3>🌐 网络与访问</h3>
          <p>浏览器 cookies、网络代理等高级配置，普通使用无需调整。</p>
        </summary>
        <div className="settings-group-body">
          <label>
            浏览器 cookies（B 站字幕 / YouTube 等需登录内容）
            <select
              id="defaultCookiesBrowser"
              value={form.cookies_browser ?? ""}
              disabled={isLoading}
              onChange={(e) => setForm((f) => ({ ...f, cookies_browser: e.target.value }))}
            >
              <option value="">不使用</option>
              {["chrome", "edge", "brave", "chromium", "vivaldi", "opera", "whale", "firefox", "safari"].map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </label>
          <div className="defaults-actions">
            <button id="cookiesTestBtn" type="button" className="secondary" onClick={() => void onTestCookies()}>
              测试读取
            </button>
            <span className="defaults-status" id="cookiesTestStatus">
              {cookieStatus}
            </span>
          </div>
          <p className="hint-brief">读取所选浏览器的登录 cookies，解锁 B 站 AI/CC 字幕、YouTube 自动字幕与会员内容。</p>
          <details className="hint-more">
            <summary>详细说明</summary>
            <p>
              直接读取所选浏览器的登录 cookies（需在该浏览器登录过 B 站 / YouTube 等目标站点）。B 站 AI/CC 字幕通常需要登录才可获取。
              Chrome 系首次读取会弹 macOS 钥匙串授权，点「始终允许」；Safari 需给本 App 完全磁盘访问权限；Docker 部署无法读取宿主机浏览器，请改用环境变量 VTS_COOKIES_FILE。
            </p>
          </details>
          <label>
            网络代理（YouTube 等需代理的站点）
            <input
              id="defaultProxy"
              placeholder="http://127.0.0.1:7897（留空 = 自动/直连）"
              value={form.proxy ?? ""}
              onChange={(e) => setForm((f) => ({ ...f, proxy: e.target.value }))}
            />
          </label>
          <p className="hint-brief">留空自动/直连；填写后 yt-dlp 强制走此代理，B 站等直连站点会变慢。</p>
          <details className="hint-more">
            <summary>详细说明</summary>
            <p>
              留空时跟随系统代理与环境变量；显式填写后 yt-dlp 强制走此代理，不受系统代理开关影响。B 站等直连站点会明显变慢，请按需开关。
            </p>
          </details>
        </div>
      </details>

      <div className="defaults-actions defaults-footer">
        <button id="saveDefaultsBtn" type="button" className="secondary" onClick={onSave}>
          保存默认配置
        </button>
        <span className="defaults-status" id="defaultsStatus">
          {status}
        </span>
      </div>
    </div>
  );
}

// ---------------- LLM 配置（BYOK）：推理模型 + 语音识别模型 ----------------
type SlotForm = { base_url: string; model: string; api_key: string };

const LLM_SLOTS: {
  id: LlmSlotKey;
  label: string;
  hint: string;
  modelPlaceholder: string;
}[] = [
  {
    id: "summary",
    label: "🧠 推理模型",
    hint: "生成结构化笔记；开启文本润色时也使用该模型。任意 OpenAI 兼容端点。",
    modelPlaceholder: "如 deepseek-chat / gpt-4o-mini",
  },
  {
    id: "asr",
    label: "🎙️ 语音识别模型",
    hint: "视频没有自带字幕时，转写音频使用该模型。",
    modelPlaceholder: "如 whisper-1",
  },
];

function LlmTab() {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["llm-config"], queryFn: fetchLlmConfig });

  const [forms, setForms] = useState<Record<LlmSlotKey, SlotForm> | null>(null);
  useEffect(() => {
    if (data && !forms) {
      setForms({
        summary: { base_url: data.summary.base_url, model: data.summary.model, api_key: "" },
        asr: { base_url: data.asr.base_url, model: data.asr.model, api_key: "" },
      });
    }
  }, [data, forms]);

  const refresh = (next?: LlmConfig) => {
    if (next) {
      setForms({
        summary: { base_url: next.summary.base_url, model: next.summary.model, api_key: "" },
        asr: { base_url: next.asr.base_url, model: next.asr.model, api_key: "" },
      });
    }
    queryClient.invalidateQueries({ queryKey: ["llm-config"] });
    queryClient.invalidateQueries({ queryKey: ["health"] });
  };

  const onSave = async () => {
    if (!forms) return;
    try {
      const next = await updateLlmConfig({
        summary: { ...forms.summary, api_key: forms.summary.api_key.trim() },
        asr: { ...forms.asr, api_key: forms.asr.api_key.trim() },
      });
      refresh(next);
      showToast("LLM 配置已保存");
    } catch (e) {
      showToast(friendlyError(e, "保存失败"), { kind: "error" });
    }
  };

  const onImportEnv = async () => {
    try {
      const next = await importLlmFromEnv();
      refresh(next);
      showToast("已从 .env 导入配置", { ttl: 4000 });
    } catch (e) {
      showToast(friendlyError(e, "导入失败"), { kind: "error" });
    }
  };

  const setSlot = (id: LlmSlotKey, patch: Partial<SlotForm>) => {
    setForms((f) => (f ? { ...f, [id]: { ...f[id], ...patch } } : f));
  };

  return (
    <div className="tab-content" id="tabLlm">
      <p className="defaults-intro">
        BYOK：自备任意 OpenAI 兼容端点。两个模型槽位——推理模型负责总结与润色，语音识别模型在视频没有字幕时转写音频；
        Key 以 Fernet 加密落库，界面只显示掩码。
      </p>

      <div className="llm-toolbar">
        <button id="llmImportEnvBtn" type="button" className="secondary" onClick={() => void onImportEnv()}>
          从 .env 导入
        </button>
      </div>

      {LLM_SLOTS.map((slot) => {
        const info = data?.[slot.id];
        return (
          <section className="settings-group" key={slot.id}>
            <div className="settings-group-head">
              <h3>{slot.label}</h3>
              <p>{slot.hint}</p>
            </div>
            <div className="settings-group-body">
              <label>
                接入地址 base_url
                <input
                  id={`llm-${slot.id}-baseUrl`}
                  autoComplete="off"
                  placeholder="https://api.openai.com/v1"
                  disabled={!forms}
                  value={forms?.[slot.id].base_url ?? ""}
                  onChange={(e) => setSlot(slot.id, { base_url: e.target.value })}
                />
              </label>
              <label>
                模型名
                <input
                  id={`llm-${slot.id}-model`}
                  autoComplete="off"
                  placeholder={slot.modelPlaceholder}
                  disabled={!forms}
                  value={forms?.[slot.id].model ?? ""}
                  onChange={(e) => setSlot(slot.id, { model: e.target.value })}
                />
              </label>
              <label>
                API Key（留空 = 保留原有）
                <input
                  id={`llm-${slot.id}-apiKey`}
                  autoComplete="off"
                  type="password"
                  placeholder={info?.api_key ? `当前：${info.api_key}` : "sk-..."}
                  disabled={!forms}
                  value={forms?.[slot.id].api_key ?? ""}
                  onChange={(e) => setSlot(slot.id, { api_key: e.target.value })}
                />
              </label>
              {info && (
                <p className="hint-brief">
                  {info.configured ? "已配置 ✓" : "未配置"}
                  {info.configured ? ` · Key ${info.api_key}` : "——未配置时总结阶段自动跳过，仍产出转写文本"}
                </p>
              )}
            </div>
          </section>
        );
      })}

      <div className="defaults-actions defaults-footer">
        <button id="llmSaveBtn" type="button" className="secondary" disabled={!forms} onClick={() => void onSave()}>
          保存 LLM 配置
        </button>
        <span className="defaults-status">保存后新提交的任务立即使用新配置；进行中的任务不受影响。</span>
      </div>
    </div>
  );
}

// ---------------- 总结模板 ----------------
function TemplateTab() {
  const queryClient = useQueryClient();
  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: fetchTemplates });
  const templates = templatesData?.templates ?? [];
  const builtins = new Set(templatesData?.builtins ?? []);

  const [selected, setSelected] = useState("");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [status, setStatus] = useState("");
  const [locked, setLocked] = useState(false);

  const isBuiltin = builtins.has(selected);

  useEffect(() => {
    if (!selected && templates.length) {
      setSelected(templates[0] || "");
    }
  }, [templates, selected]);

  const loadTemplate = useCallback(async (name: string) => {
    setStatus("加载中…");
    try {
      const data = await fetchTemplate(name);
      setTitle(data.name);
      setPrompt(data.template.prompt || "");
      setLocked(builtins.has(name));
      setStatus(builtins.has(name) ? "内置模板只读：可直接选用；想定制请点「＋ 新建模板」另存" : "已加载");
    } catch (e) {
      setStatus(friendlyError(e, "模板加载失败"));
    }
  }, [builtins]);

  const onSelect = (name: string) => {
    setSelected(name);
    void loadTemplate(name);
  };

  const onSave = async () => {
    const name = title.trim();
    if (!name) {
      setStatus("请先填写模板名称");
      return;
    }
    if (!prompt.trim()) {
      setStatus("提示词不能为空");
      return;
    }
    setStatus("保存中…");
    try {
      await saveTemplate(name, prompt);
      setStatus(`已保存：${name}`);
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      setSelected(name);
    } catch (e) {
      setStatus(friendlyError(e, "保存失败"));
    }
  };

  const onDelete = async () => {
    if (!selected) {
      setStatus("请先在「已有模板」中选择要删除的模板");
      return;
    }
    if (!window.confirm(`确定删除自定义模板「${selected}」？此操作不可恢复。`)) return;
    setStatus("删除中…");
    try {
      await deleteTemplate(selected);
      setStatus(`已删除：${selected}`);
      setTitle("");
      setPrompt("");
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      const next = templates.filter((t) => t !== selected);
      if (next.length) setSelected(next[0]);
    } catch (e) {
      setStatus(friendlyError(e, "删除失败"));
    }
  };

  return (
    <div className="tab-content" id="tabTemplate">
      <div className="template-toolbar">
        <label>
          已有模板
          <select id="templateName" value={selected} onChange={(e) => onSelect(e.target.value)}>
            {templates.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <button id="loadTemplateBtn" className="secondary" onClick={() => selected && void loadTemplate(selected)}>
          加载
        </button>
        <button id="deleteTemplateBtn" className="secondary" disabled={isBuiltin || !selected} onClick={() => void onDelete()}>
          删除
        </button>
        <button
          id="createTemplateBtn"
          className="secondary"
          onClick={() => {
            setLocked(false);
            setTitle("");
            setPrompt("");
            setStatus("填写模板名称与提示词后点击「保存模板」，将创建新模板。");
          }}
        >
          ＋ 新建模板
        </button>
      </div>
      <div className="template-editor">
        <label>
          模板名称
          <input
            id="templateTitle"
            placeholder="保存新模板时填写，例如：投资备忘录"
            autoComplete="off"
            disabled={locked}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          提示词（一段话，描述总结什么、怎么组织）
          <textarea
            id="templatePrompt"
            rows={7}
            disabled={locked}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </label>
        <p className="form-hint">
          排版美化（emoji 小标题、加粗、引用块）由系统统一保证，无需写进提示词。保存时按上方名称新建或覆盖。
        </p>
      </div>
      <div className="template-actions">
        <button id="saveTemplateBtn" className="secondary" disabled={locked} onClick={() => void onSave()}>
          保存模板
        </button>
      </div>
      <div className="template-status" id="templateStatus">
        {status}
      </div>
    </div>
  );
}

// ---------------- 标签管理 ----------------
function LabelsTab() {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const { data, refetch } = useQuery({ queryKey: ["labels"], queryFn: fetchLabels });
  const [newName, setNewName] = useState("");
  const [hint, setHint] = useState("");
  const [editingRow, setEditingRow] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [mergingRow, setMergingRow] = useState<number | null>(null);
  const [mergeTarget, setMergeTarget] = useState("");

  const labels = data?.labels ?? [];
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["labels"] });
    queryClient.invalidateQueries({ queryKey: ["history"] });
  };

  const onCreate = async () => {
    const name = newName.trim();
    if (!name) {
      setHint("标签名不能为空");
      return;
    }
    try {
      await createLabel(name);
      setNewName("");
      setHint("");
      showToast("已新建标签");
      refetch();
    } catch (e) {
      setHint(friendlyError(e, "新建失败"));
    }
  };

  const doRename = async (id: number) => {
    const v = renameValue.trim();
    if (!v) {
      setEditingRow(null);
      return;
    }
    try {
      await renameLabel(id, v);
      showToast("已重命名");
      setEditingRow(null);
      refresh();
    } catch (e) {
      showToast(friendlyError(e, "重命名失败"), { kind: "error" });
    }
  };

  const doMerge = async (sourceId: number) => {
    const target = parseInt(mergeTarget, 10);
    if (!Number.isFinite(target)) return;
    try {
      await mergeLabels(sourceId, target);
      showToast("已合并");
      setMergingRow(null);
      refresh();
    } catch (e) {
      showToast(friendlyError(e, "合并失败"), { kind: "error" });
    }
  };

  const doDelete = async (l: { id: number; name: string; count: number }) => {
    if (!window.confirm(`确定删除标签「${l.name}」？将从 ${l.count} 个任务移除。`)) return;
    try {
      await deleteLabel(l.id);
      showToast("已删除");
      refresh();
    } catch (e) {
      showToast(friendlyError(e, "删除失败"), { kind: "error" });
    }
  };

  return (
    <div className="tab-content" id="tabLabels">
      <div className="label-mgmt-layout">
        <div className="label-mgmt-create">
          <input
            id="newLabelName"
            className="label-mgmt-inline-input"
            autoComplete="off"
            placeholder="新标签名"
            maxLength={24}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void onCreate();
            }}
          />
          <button id="createLabelBtn" type="button" className="secondary" onClick={() => void onCreate()}>
            新建标签
          </button>
          <span id="newLabelHint" className="tag-hint">
            {hint}
          </span>
        </div>
        <p className="tag-hint">
          给任务贴 / 改标签：在「我的任务」点开任务，详情卡点「编辑标签」；创建任务时也可直接填写，此处只管理标签本身（重命名 / 合并 / 删除）。
        </p>
        <div id="labelMgmtList">
          {labels.length === 0 && (
            <p className="tag-hint" id="labelMgmtEmpty">
              还没有标签。创建任务时输入的标签会自动出现在这里。
            </p>
          )}
          {labels.map((l) => (
            <div className="label-mgmt-row" key={l.id}>
              <span className="label-mgmt-name">{l.name}</span>
              <span className="label-mgmt-count">{l.count} 个任务</span>
              <div className="label-mgmt-actions">
                {editingRow !== l.id && (
                  <button type="button" className="secondary" onClick={() => { setEditingRow(l.id); setRenameValue(l.name); }}>
                    重命名
                  </button>
                )}
                {editingRow === l.id && (
                  <span className="label-mgmt-inline">
                    <input
                      className="label-mgmt-inline-input"
                      autoComplete="off"
                      maxLength={24}
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void doRename(l.id);
                      }}
                    />
                    <button type="button" className="secondary" onClick={() => void doRename(l.id)}>
                      确认
                    </button>
                    <button type="button" className="secondary" onClick={() => setEditingRow(null)}>
                      取消
                    </button>
                  </span>
                )}
                {mergingRow !== l.id && (
                  <button type="button" className="secondary" onClick={() => { setMergingRow(l.id); setMergeTarget(String(labels.find((x) => x.id !== l.id)?.id ?? "")); }}>
                    合并到…
                  </button>
                )}
                {mergingRow === l.id && (
                  <span className="label-mgmt-inline">
                    <select
                      className="label-mgmt-inline-input"
                      value={mergeTarget}
                      onChange={(e) => setMergeTarget(e.target.value)}
                    >
                      {labels
                        .filter((x) => x.id !== l.id)
                        .map((x) => (
                          <option key={x.id} value={x.id}>
                            {x.name}
                          </option>
                        ))}
                    </select>
                    <button type="button" className="secondary" onClick={() => void doMerge(l.id)}>
                      确认
                    </button>
                    <button type="button" className="secondary" onClick={() => setMergingRow(null)}>
                      取消
                    </button>
                  </span>
                )}
                <button type="button" className="secondary" onClick={() => void doDelete(l)}>
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
