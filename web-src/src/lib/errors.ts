// 失败错误分类：把原始异常翻译成「人话标题 + 建议 + 去处理入口」。
// 与旧 app.js ERROR_PATTERNS 对齐；BYOK 下 Key/接入类失败引导统一指向「设置」。

export interface ErrorAction {
  panels: string[];
  label: string;
}

export interface ErrorPattern {
  re: RegExp;
  title: string;
  hint: string;
  action?: ErrorAction;
}

export const ERROR_PATTERNS: ErrorPattern[] = [
  {
    re: /(nodename nor servname|failed to resolve|connection refused|connection reset|timed out|network is unreachable|temporary failure in name resolution|getaddrinfo)/i,
    title: "无法访问该链接",
    hint: "请检查网络连接，或确认链接可公开访问后重试。",
  },
  {
    re: /(sign in to confirm|members?-only|premium only|login required|需要登录|大会员|付费|仅限会员)/i,
    title: "该内容需要登录 / 会员权限",
    hint: "该站点需要登录态：请在「设置 → 任务默认」配置浏览器 cookies 后重试（本版不内置扫码登录）。",
    action: { panels: ["tabDefaults"], label: "去配置 cookies" },
  },
  {
    re: /unsupported url/i,
    title: "暂不支持该链接",
    hint: "请粘贴视频页面地址（支持 B 站、YouTube 等 yt-dlp 兼容站点），而非分享短链或网页嵌入口。",
  },
  {
    re: /(40[34]|not found|private video|video unavailable|has been removed|removed by the uploader)/i,
    title: "视频不存在或不可访问",
    hint: "链接可能已失效、被设为私享或存在地区限制。",
  },
  {
    re: /(ffmpeg|ffprobe)/i,
    title: "缺少 ffmpeg",
    hint: "本机未安装 ffmpeg，无法下载音频。请运行 bash scripts/fetch_ffmpeg.sh 后重试。",
  },
  {
    re: /(insufficient[_ ]?quota|exceeded your current quota|quota exceeded|arrearage|欠费|余额不足|账户额度不足)/i,
    title: "LLM 账户额度不足",
    hint: "你配置的 LLM 账户额度不足或已欠费：请到服务商后台充值，或在「设置 → LLM 配置」里改用其它 OpenAI 兼容端点。",
  },
  {
    re: /(429|rate[ _-]?limit|too many requests|请求过于频繁|请求太快|限流)/i,
    title: "请求过于频繁",
    hint: "触发上游限流：请稍后重试，或避免短时间内提交大量任务。",
  },
  {
    re: /(401|unauthorized|invalid[_ ]api[ _]?key|incorrect api key|invalid_request_error.*api key|authentication)/i,
    title: "LLM Key 校验失败",
    hint: "请在「设置 → LLM 配置」里检查 API Key、接入地址（base_url）与模型名是否填写正确并已保存。",
    action: { panels: ["tabLlm"], label: "去设置" },
  },
];

export function classifyError(raw: unknown): ErrorPattern | null {
  const text = String(raw ?? "");
  for (const p of ERROR_PATTERNS) {
    try {
      if (p.re.test(text)) return p;
    } catch {
      /* 忽略正则异常，继续下一条 */
    }
  }
  return null;
}
