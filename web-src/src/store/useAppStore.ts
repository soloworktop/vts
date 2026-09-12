// ============ 全局 UI 状态（zustand） ============
// 服务端数据走 TanStack Query（组件内 useQuery）；这里只放跨组件共享的 UI 态：
// 当前视图、健康信息、历史列表窗口、筛选态、主题等。
import { create } from "zustand";
import type { Health, JobListItem, Settings } from "../api/types";

export type ViewName = "new" | "status" | "history" | "settings";

interface AppState {
  health: Health | null;
  setHealth: (h: Health | null) => void;

  view: ViewName;
  setView: (v: ViewName) => void;

  // 设置视图当前激活的 tab（跨组件跳转「去设置」用）
  settingsTab: string;
  setSettingsTab: (tab: string) => void;

  // 历史页左右布局：当前选中任务 + 详情快照
  currentHistoryJobId: string;
  setCurrentHistoryJobId: (id: string) => void;

  // 历史列表窗口（已加载全量，滚动加载追加）
  historyEntries: JobListItem[];
  historyTotal: number;
  setHistoryEntries: (entries: JobListItem[], total: number) => void;
  appendHistoryEntries: (entries: JobListItem[], total: number) => void;
  resetHistoryWindow: () => void;

  historySearchTerm: string;
  setHistorySearchTerm: (term: string) => void;

  // 标签筛选：null=全部 / "__uncat__"=未分类 / 名字=具体标签
  activeLabelFilter: string | null;
  setActiveLabelFilter: (v: string | null) => void;

  // 全局任务默认（subtitle 阶段显隐等判断）
  globalDefaults: Settings | null;
  setGlobalDefaults: (s: Settings | null) => void;

  // 上次看到的状态快照（终态迁移检测）
  seenStatuses: Map<string, string>;
  replaceSeenStatuses: (next: Map<string, string>) => void;

  // 进行中任务数（侧栏运行徽标）
  runningCount: number;
  setRunningCount: (n: number) => void;

}

export const useAppStore = create<AppState>((set) => ({
  health: null,
  setHealth: (h) => set({ health: h }),

  view: "new",
  setView: (v) => set({ view: v }),

  settingsTab: "tabDefaults",
  setSettingsTab: (tab) => set({ settingsTab: tab }),

  currentHistoryJobId: "",
  setCurrentHistoryJobId: (id) => set({ currentHistoryJobId: id }),

  historyEntries: [],
  historyTotal: 0,
  setHistoryEntries: (entries, total) => set({ historyEntries: entries, historyTotal: total }),
  appendHistoryEntries: (entries, total) =>
    set((s) => {
      const known = new Set(s.historyEntries.map((j) => j.job_id));
      const merged = [...s.historyEntries];
      for (const j of entries) {
        if (!known.has(j.job_id)) merged.push(j);
      }
      return { historyEntries: merged, historyTotal: total };
    }),
  resetHistoryWindow: () => set({ historyEntries: [], historyTotal: 0 }),

  historySearchTerm: "",
  setHistorySearchTerm: (term) => set({ historySearchTerm: term }),

  activeLabelFilter: null,
  setActiveLabelFilter: (v) => set({ activeLabelFilter: v }),

  globalDefaults: null,
  setGlobalDefaults: (s) => set({ globalDefaults: s }),

  seenStatuses: new Map<string, string>(),
  replaceSeenStatuses: (next) => set({ seenStatuses: next }),

  runningCount: 0,
  setRunningCount: (n) => set({ runningCount: n }),

}));
