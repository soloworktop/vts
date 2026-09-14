# web-src —— VTS Web 控制台前端（React + TypeScript）

本目录是 Web 控制台前端的**源码**：只想部署使用请回仓库根 `README.md`；改前端请先读完
本文件。

前端功能对等迁移自旧的原生 JS 前端（`web/static/` 旧版），**不做视觉重设计**：信息架构
（四视图 + 设置子 tab）、文案、交互语义与旧版保持一致。视觉样式以既有设计系统
`web-src/src/styles/style.css` 为准——主题变量与全部组件样式集中于此（`html[data-theme]`
深浅双主题）；Tailwind 只提供少量工具类（经 `web-src/src/index.css` 的 `@tailwind` 指令
引入，且已在 `tailwind.config.js` 关闭 preflight，避免基础重置与既有样式冲突）。

## 技术栈

React 18 · TypeScript（strict）· Vite 5 · Tailwind CSS 3 · TanStack Query 5 · zustand 5
（依赖 pin 在 `package.json`，锁文件 `pnpm-lock.yaml` 随仓库提交）

## 目录

```text
src/
├─ api/        ★ API 层唯一出口：client.ts（base=/api/v1 + fetch 封装）、
│              endpoints.ts（全部端点函数，组件禁止散落硬编码 URL）、
│              types.ts（手写等价类型，对照 web/app.py 响应字面量）
├─ lib/        纯逻辑：events.ts（进度事件消费，未知事件忽略）、markdown.ts（marked+DOMPurify）、
│              errors.ts（失败分类）、constants.ts（状态/事件等展示常量）、format.ts、
│              recent.ts（最近来源 localStorage）、theme.ts、desktop.ts（pywebview 桥）、
│              notify.ts
├─ store/      zustand：视图 / capabilities / 健康 / 历史窗口 / 筛选态
├─ components/ Toast / TagEditor / Stepper / Modal / JobError / RecentSelect
└─ views/      NewJobView / StatusView / HistoryView(+HistoryDetail) / SettingsView
```

## 开发

前置：**Node 22+** 与 `pnpm`（仓库以 pnpm 11.8.0 开发，pnpm 11 要求 Node 22+，
Node 20 下 `pnpm install` 实测报 `ERR_UNKNOWN_BUILTIN_MODULE`）；后端服务在
`127.0.0.1:8080` 运行（`bash scripts/web.sh`，未构建前端时不影响 API）。

```bash
pnpm install          # 安装依赖（锁文件已在仓库）
pnpm dev              # Vite dev server → http://localhost:5173/static/
                      # /api、/guide、/static 代理到 http://127.0.0.1:8080
pnpm typecheck        # tsc --noEmit
pnpm build            # tsc --noEmit && vite build && 复制产物到 src/video_to_summary/web/static/
```

> 生产态：`pnpm build` 的产物落回 `web/static/`（构建产物**不入库**，由
> `scripts/copy-build.mjs` 复制并保留 `user-guide.html` / `README.md`；其中
> `user-guide.html` 即后端 `/guide` 使用手册页面，源文件在
> `src/video_to_summary/web/static/`、不在本目录）。
> 未构建时 FastAPI 首页返回「前端未构建」提示页（`web/app.py::_frontend_not_built_html`）。

## 关键约定

- **API 一律 `/api/v1`**：改端点先改 `api/endpoints.ts` + `api/types.ts`，再同步
  `README.md` 接口表（`tests/test_doc_consistency.py` 强制双向一致）。
- **能力驱动渲染**：`GET /api/v1/capabilities` 返回插件声明的可选能力集合（本构建为
  空对象）；前端据此渲染插件提供的可选功能，未声明的能力完全不渲染（不是 disabled）。
- **未知进度事件忽略**：`lib/events.ts::KNOWN_EVENT_NAMES` 之外的事件名一律忽略，
  不得让新事件把页面渲染炸掉。
- **导出禁 Blob**：一律走带 `Content-Disposition` 的接口地址（`jobExportUrl` / `logsExportUrl` /
  `historyExportUrl`），WKWebView 下 Blob 会被内联渲染不落盘。
- **E2E**：`bash scripts/e2e.sh`（会先构建前端）。UI e2e 依赖稳定 DOM id（与旧版一致），
  改 id 时必须同步 `tests/e2e/` 用例。
