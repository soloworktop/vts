// 桌面壳（pywebview）桥接：同一套前端被浏览器与桌面壳共同加载。
// 导出/文件选择在桌面形态下必须走桥（WKWebView 下 Blob 被内联渲染、下载委托结果
// JS 拿不到）；浏览器形态回退真实 attachment URL 下载。

export interface PyWebviewApi {
  pick_file?: () => Promise<string | null>;
  export_product?: (url: string, name: string) => Promise<ExportResult>;
  reveal_in_finder?: (path: string) => boolean;
}

export interface ExportResult {
  saved: boolean;
  cancelled: boolean;
  path?: string;
}

declare global {
  interface Window {
    pywebview?: { api?: PyWebviewApi };
  }
}

export function isDesktopApp(): boolean {
  return !!(window.pywebview && window.pywebview.api);
}

export function desktopApi(): PyWebviewApi | undefined {
  return window.pywebview?.api;
}

/** 触发一次真实下载（新建 <a> 点击，href 直接指向带 Content-Disposition 的接口地址）。 */
export function triggerAnchorDownload(url: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
