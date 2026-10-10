// One in-memory desktop session; never persist credentials or place them in URLs.
export interface ExportResult { saved: boolean; cancelled: boolean; path?: string }
export interface DesktopSession { origin: string; token: string; version: string }
export interface PyWebviewApi {
  get_session: () => Promise<DesktopSession>;
  pick_file: (kind?: "media" | "cookies") => Promise<string | null>;
  export_product: (url: string, name: string) => Promise<ExportResult>;
  reveal_in_finder: (path: string) => Promise<boolean>;
  open_job_output: (jobId: string, productKey?: string) => Promise<boolean>;
  open_external: (url: string) => Promise<boolean>;
  copy_text: (text: string) => Promise<boolean>;
  request_exit: () => Promise<boolean>;
}
type NativeApi = { [K in keyof PyWebviewApi]: (...args: unknown[]) => ReturnType<PyWebviewApi[K]> };
declare global { interface Window { pywebview?: { api?: NativeApi } } }
let session: DesktopSession | undefined;
export function desktopApi(): PyWebviewApi | undefined {
  const native = window.pywebview?.api;
  if (!native || !session) return undefined;
  const proof = session.token;
  return {
    get_session: () => Promise.resolve(session!),
    pick_file: (kind = "media") => native.pick_file(kind, proof),
    export_product: (url, name) => native.export_product(url, name, proof),
    reveal_in_finder: (path) => native.reveal_in_finder(path, proof),
    open_job_output: (id, key) => native.open_job_output(id, key ?? null, proof),
    open_external: (url) => native.open_external(url, proof),
    copy_text: (text) => native.copy_text(text, proof),
    request_exit: () => native.request_exit(proof),
  };
}
export function isDesktopApp() { return Boolean(session); }
export function sessionHeaders(): Record<string, string> {
  return session ? { Authorization: `Bearer ${session.token}` } : {};
}
export async function initializeDesktop(): Promise<void> {
  if (!document.querySelector('meta[name="vts-desktop"]')) return;
  if (!window.pywebview?.api?.get_session) {
    await new Promise<void>((resolve, reject) => {
      const ready = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(() => { window.removeEventListener("pywebviewready", ready); reject(new Error("桌面桥初始化超时，请重启 VTS")); }, 30_000);
      window.addEventListener("pywebviewready", ready, { once: true });
    });
  }
  const marker = document.querySelector<HTMLMetaElement>('meta[name="vts-desktop"]')!;
  session = await window.pywebview!.api!.get_session(marker.content);
  marker.content = "ready";
  if (session.origin !== location.origin || !session.token) throw new Error("桌面会话无效");
  document.addEventListener("click", (event) => {
    const anchor = (event.target as HTMLElement).closest("a[href]") as HTMLAnchorElement | null;
    if (anchor && /^https?:/.test(anchor.href) && new URL(anchor.href).origin !== location.origin) {
      event.preventDefault();
      void desktopApi()!.open_external(anchor.href);
    }
  }, true);
}
const exitGuards = new Set<() => Promise<boolean>>();
export function registerExitGuard(guard: () => Promise<boolean>): () => void {
  exitGuards.add(guard);
  return () => { exitGuards.delete(guard); };
}
export async function allowDesktopExit() {
  for (const guard of exitGuards) if (!(await guard())) return false;
  return true;
}
export async function copyText(text: string): Promise<boolean> {
  if (isDesktopApp()) return desktopApi()!.copy_text(text);
  await navigator.clipboard.writeText(text);
  return true;
}
export function triggerAnchorDownload(url: string): void {
  const a = document.createElement("a"); a.href = url; a.download = "";
  document.body.appendChild(a); a.click(); a.remove();
}
