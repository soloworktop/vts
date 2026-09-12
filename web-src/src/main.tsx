import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ToastProvider } from "./components/Toast";
import "./index.css";
import "./styles/style.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <App />
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);

// 应用初始化完成标记：e2e（goto_console）以此等待，消除「按钮已渲染但 handler 未绑定」的竞态
window.__APP_READY__ = true;

declare global {
  interface Window {
    __APP_READY__?: boolean;
  }
}
