// ============ Markdown 渲染 + XSS 净化（npm 版 marked + dompurify） ============
// 替换旧前端的 vendored marked.min.js / purify.min.js。
// 语义与旧 renderMarkdown 完全对齐：
//   - 原始 HTML 一律转义，避免 LLM 输出注入；
//   - 唯一例外：受限的行内着色 span（style 仅允许 color 一种声明，
//     与 summarizers._STYLE_CONTRACT 的输出约定双向对齐）；
//   - 链接只放行 http(s)/mailto；图片转义为文本；
//   - 纵深防御：renderer 转义后再过一层 DOMPurify。

import DOMPurify from "dompurify";
import { marked } from "marked";
import { escapeHtml } from "./format";

const SAFE_COLOR_SPAN = /^<\/?span(?:\s+style\s*=\s*"color:\s*[#(),.\sa-zA-Z0-9]+")?>$/i;

marked.use({
  renderer: {
    html(token: { text?: string } | string): string {
      const raw = String(
        typeof token === "string" ? token : token && token.text !== undefined ? token.text : token,
      ).trim();
      if (SAFE_COLOR_SPAN.test(raw)) return raw;
      return escapeHtml(raw);
    },
    link(
      token: { href?: string | null; title?: string | null; text: string; tokens?: unknown[] },
      ...args: unknown[]
    ): string {
      const href = token.href ?? "";
      const safe = /^(https?:|mailto:)/.test(href) ? href : "#";
      const titleAttr = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      // 链接文本要继续走 inline 解析（含转义）：token.text 是未解析的原始源文本，
      // 直插会让链接内的 markdown 失效、原始 HTML 绕过转义（DOMPurify 兜底前的纵深）
      const parser = (this as { parser?: { parseInline(t: unknown[]): string } }).parser;
      const label =
        parser && Array.isArray(token.tokens)
          ? parser.parseInline(token.tokens)
          : escapeHtml(token.text);
      void args;
      return `<a href="${escapeHtml(safe)}"${titleAttr} target="_blank" rel="noopener noreferrer">${label}</a>`;
    },
    image(): string {
      return "";
    },
  },
});

/** 渲染 Markdown → 净化后的 HTML 字符串。渲染失败回退为转义原文。 */
export function renderMarkdown(md: string): string {
  if (!md) return "";
  try {
    let html = marked.parse(md, { gfm: true, breaks: false }) as string;
    // 纵深防御：即使 renderer 转义有遗漏，再过一层成熟 sanitizer
    html = DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      ADD_ATTR: ["target", "style"],
    });
    return html;
  } catch {
    return escapeHtml(md);
  }
}

export function isMarkdownPath(path: string): boolean {
  return /\.(md|markdown)$/i.test(path.split("?")[0]);
}
