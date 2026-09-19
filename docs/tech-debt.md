# 技术债与安全增强登记

本文件记录稳定性 / 安全审计中确认的已知限制与后续增强方向。这些项是**有意不在
当前版本实现**的（避免为理论风险引入复杂度），在此登记供后续规划。

## 1. 产物指纹 / manifest（pipeline 缓存）

- 现状：缓存策略为「**文件存在即命中缓存**」（`output/<job_id>/` 下
  `.txt` / `.segments.json` / `.srt` / `.polished.txt`，见 `pipeline.py`）；
  retry 通过清空整个任务输出目录保证从头重建。**summary 无缓存语义**——每次运行都
  重新生成并覆盖写盘，因此模板/配置切换后不会误用旧 summary。
- 已知限制：缓存命中不含输入指纹（ASR 模型 / 音频内容 / 分片参数）。同一源改 ASR
  配置后 resume 会复用旧转写；单任务目录 + retry 显式清缓存的当前产品形态下可接受。
- 方向：为每个任务引入 `manifest.json`（记录各阶段输入配置快照与产物指纹），缓存
  命中前先校验 manifest；或在产物文件名中携带配置 hash。单用户千级任务量内暂无必要。

## 2. 私网 URL 拒绝（可选 SSRF 加固，未实现）

- 现状：URL 源仅允许 http/https scheme（`sources/url._validate_url`，拒绝 `file://`
  等本地协议），但**不拒绝私网 / loopback / link-local 目标**。
- 不做的理由：单用户自部署 BYOK 工具，提交任务者即服务器所有者；局域网媒体服务器
  （Jellyfin / EMBY 直链）是合法场景；围绕 yt-dlp 做 DNS 解析 + 重定向链感知的校验
  成本高且覆盖不全（重定向后的目标无法可靠拦截），只会造成虚假安全感。
- 风险边界：默认只监听本机 → 无跨用户风险面。暴露到局域网 / 公网后，持有
  `VIDEO_TO_SUMMARY_TOKEN` 的访问者可让服务器向内网地址发起请求 → 文档已明确
  「暴露前必须设置 Token 并限制网络可达范围」（README「安全」/ SECURITY.md /
  docker/README.md）。
- 方向：可选环境变量 `VTS_ALLOW_PRIVATE_URLS=false`（默认保持现行为）——建任务时
  解析 hostname，命中 RFC1918 / loopback / 169.254.0.0/16 时拒绝。注意这仍拦不住
  重定向后的私网跳转，真正的边界是网络隔离（Docker network policy / 防火墙）。

## 3. 诊断日志脱敏的覆盖上限

- 现状：`log_export.sanitize_text` 覆盖 OpenAI 风格 Key、Fernet 密文、Bearer、
  cookie 行、查询参数 token/签名、`api_key/secret/password` 赋值、非 Bearer 认证头
  （Basic / X-Api-Key）、`access/refresh/id_token`、URL userinfo（`user:pass@host`）、
  主目录伪名化。URL userinfo 打码同时收敛了 httpx/openai SDK 的 INFO 访问日志
  回显带凭据 base_url 的泄漏链（2026-09 审计确认的真实路径）。
- 上限：日志内容是自由文本，未知凭据形态（自定义认证头、第三方网关的私有签名参数）
  仍可能漏网。诊断日志导出（`/api/v1/logs/export`）前请自行确认可对外交付。

## 4. 编辑稿无版本历史

- 现状：`summary_edited_at` 只有单一时间戳（前端展示「已编辑」徽标）；retry 清空
  输出目录，用户编辑稿随之失效（「重新生成」的产品语义），不可恢复。
- 方向：如需保留，可在 retry 前把编辑稿另存 `.edited.md` 副本或引入版本历史；
  属产品级增强，当前刻意不做。

## 5. resume 防重复执行依赖单实例锁

- 现状：`resume_pending_jobs()` 把 RUNNING/PENDING 任务重新入队；「旧进程已消亡」
  的前提由数据目录运行锁保证（`web/tasks.acquire_scheduler_lock`，第二个进程启动即
  明确报错）。任务行内的取消意图（`cancel_requested` 事件）在 resume 时被尊重——
  已请求停止的任务落 cancelled 终态而非复活。
- 若未来引入多进程调度，需要 DB-backed job lease 重新设计 resume 语义；单进程形态
  是当前的有意取舍（见 AGENTS.md「Web 单进程」）。

## 6. upload 孤儿清扫的窗口

- 现状：上传与建任务在同请求内完成；进程在「落盘后、建任务前」崩溃会留下孤儿目录，
  由启动时 `sweep_orphan_uploads()` 按 DB payload 引用集清扫（引用判定失败时放弃，
  绝不误删；用户本地文件永不删除）。运行期不做后台 GC——单用户形态下无必要。
