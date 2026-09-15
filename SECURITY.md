# 安全策略（Security Policy）

## 如何报告漏洞

**请不要用公开 Issue 报告安全漏洞。**

- 仓库托管在 GitHub 时：使用 Security → "Report a vulnerability"（私密漏洞报告）；
- 该渠道不可用时：Issue 标题加 `[security]` 前缀，正文避免粘贴任何 API Key、cookie、
  诊断日志片段。

收到报告后会在 7 天内首次回复；修复随下一个补丁版本发布，并在发布说明中致谢
（可要求匿名）。

## 自部署安全要点

- 所有 API Key 经 Fernet 加密落库；密钥文件 `enc_key` 与数据库同目录，请一并备份、
  不要外传；
- 服务默认只监听 `127.0.0.1`；暴露到局域网 / 公网前**必须**设置
  `VIDEO_TO_SUMMARY_TOKEN`（见 `docs/api.md`）；
- 诊断日志导出（`GET /api/v1/logs/export`）已对 Key / Bearer / cookie 做伪名化，
  对外分享前仍建议自行过目；
- 需要登录态的站点 cookies 由用户自行提供，仅存放于本机（数据库或 `VTS_COOKIES_FILE`
  指向的文件），请按敏感凭据对待。
