# web-src 子目录约定

本目录约定是根 `AGENTS.md` 的补充；根文件的铁律在此同样生效。完整的前端栈说明、
目录结构与构建细节见本目录 `README.md`（避免两处重复造成漂移，本文件只列
最容易踩的几条，与 README 冲突时以 README 为准）：

- 技术栈与 Node 版本要求、`pnpm dev / typecheck / build` 用法 → 见 `README.md`「开发」
- 构建产物落回 `src/video_to_summary/web/static/`（不入库）→ `scripts/copy-build.mjs`

## 最容易踩的几条

1. **API 一律走 `src/api/` 唯一出口**：组件禁止散落硬编码 URL；改端点先改
   `api/endpoints.ts` + `api/types.ts`，再同步仓库根 `README.md` 接口表
   （`tests/test_doc_consistency.py` 强制双向一致）。
2. **改完必须 `pnpm build`**：`tsc --noEmit`（strict）+ `vite build` + 复制产物；
   只改源码不构建，运行中的 Web 控制台看不到变化。
3. **UI e2e 依赖稳定 DOM id**（`tests/e2e/`）：改动/删除现有 `id` 属性必须同步 e2e 用例。
4. **未知进度事件一律忽略**（`lib/events.ts::KNOWN_EVENT_NAMES` 之外），
   不得让新事件把页面渲染炸掉。
5. **导出禁 Blob**：一律走带 `Content-Disposition` 的接口地址（WKWebView 下
   Blob 会被内联渲染不落盘）。
