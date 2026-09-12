import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base 必须是 /static/：FastAPI 以 /static 挂载前端构建产物（_RevalidateStaticFiles），
// index.html 与 assets 的引用路径都要带该前缀；dev 模式 Vite 也在 /static/ 下提供页面。
// API 一律经 /api（Vite dev 代理到 FastAPI 的 8080 端口），生产同源直连。
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/guide": "http://127.0.0.1:8080",
      "/static": "http://127.0.0.1:8080",
    },
  },
});
