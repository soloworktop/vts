// 主题：auto → 深 → 浅循环，localStorage 记忆（对照旧 app.js 主题控制器）
import { THEME_KEY } from "./constants";

export type ThemeMode = "auto" | "dark" | "light";

export const THEME_MODES: ThemeMode[] = ["auto", "dark", "light"];
export const THEME_ICONS: Record<ThemeMode, string> = { auto: "🌗", dark: "🌙", light: "☀️" };
export const THEME_NAMES: Record<ThemeMode, string> = { auto: "跟随系统", dark: "深色", light: "浅色" };

export function storedThemeMode(): ThemeMode {
  try {
    const m = localStorage.getItem(THEME_KEY);
    return (THEME_MODES as string[]).includes(m || "") ? (m as ThemeMode) : "auto";
  } catch {
    return "auto";
  }
}

function mqDark(): MediaQueryList {
  return window.matchMedia("(prefers-color-scheme: dark)");
}

export function applyTheme(mode: ThemeMode): void {
  document.documentElement.dataset.theme = mode === "auto" ? (mqDark().matches ? "dark" : "light") : mode;
}

export function cycleTheme(): ThemeMode {
  const next = THEME_MODES[(THEME_MODES.indexOf(storedThemeMode()) + 1) % THEME_MODES.length];
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* ignore */
  }
  applyTheme(next);
  return next;
}

export function initTheme(): () => void {
  const mode = storedThemeMode();
  applyTheme(mode);
  const onChange = () => applyTheme(storedThemeMode());
  mqDark().addEventListener?.("change", onChange);
  return () => mqDark().removeEventListener?.("change", onChange);
}
