/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // 视觉样式以 style.css（既有设计系统，data-theme 深浅主题）为准；
  // 关闭 preflight 以免 Tailwind 基础重置与既有样式冲突。
  corePlugins: { preflight: false },
  theme: {
    extend: {},
  },
  plugins: [],
};
