# web/static —— React 前端构建产物（不入库）

本目录是 **`web-src/`（React 18 + Vite + TypeScript + Tailwind + TanStack Query）的构建产物**，
由 `cd web-src && pnpm build` 生成（`web-src/scripts/copy-build.mjs` 复制到本目录）。
**构建产物不提交进仓库**（见根 `.gitignore`）；源码检出后需先构建，否则首页显示「前端未构建」提示页。

- 入口：`index.html`（React 挂载点）+ `assets/`（Vite 打包 JS/CSS）
- 保留文件：`user-guide.html`（`/guide` 使用手册，随包分发，构建脚本不会覆盖它）
- 由 FastAPI 以 `/static` 挂载，走 `_RevalidateStaticFiles`（`Cache-Control: no-cache` + ETag/304）
- 后端接口一律走 **`/api/v1`**（canonical；过渡别名 `/api/*` 已随 M1 删除）

## 关键设计（M1 验收点）

- **API 层集中**：所有请求在 `web-src/src/api/` 一处（base path 常量 `/api/v1`），
  组件不散落硬编码 URL；类型为手写等价类型（对照 `web/app.py` 响应字面量，见 `api/types.ts` 头注释）
- **能力驱动渲染**：启动拉 `GET /api/v1/capabilities`——插件声明集合（核心不预置、
  本构建为空对象 `{}`），前端据此渲染插件声明的能力相关 UI；无声明时界面即完整产品形态
- **SSE/事件消费**：进度事件按事件名消费，未知事件忽略而非报错（向前兼容，见 `lib/events.ts`）
- **Markdown + XSS 净化**：npm 版 `marked` + `dompurify`（`lib/markdown.ts`）
- **导出**：真实 attachment（`Content-Disposition`）下载，**禁 Blob**（WKWebView 下被内联渲染）
- **桌面壳兼容**：pywebview 桥（`pick_file` / `export_product` / `reveal_in_finder`）原样保留，
  现有 pywebview 壳零改动即可加载本前端

开发/构建/运行方式见 **`web-src/README.md`**。
