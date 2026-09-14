# HTTP API 参考（api）

canonical 前缀为 **`/api/v1`**；过渡别名 `/api/*` 已随前端切换（M1）删除，所有客户端
一律走 `/api/v1`。本表与 `src/video_to_summary/web/app.py` 的真实路由由
`tests/test_doc_consistency.py` 强制双向一致（锚点聚合读 `README.md` 与本文件）；
接口增删改后先改代码，再同步本表。OpenAPI 路径集合由 `tests/test_openapi_contract.py`
快照锁定（`tests/openapi_paths.json`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 服务状态、版本、LLM 是否已配置、DB schema 提示 |
| GET | `/api/v1/capabilities` | 能力集合（见上） |
| POST | `/api/v1/jobs` | 创建任务 |
| GET | `/api/v1/jobs` | 列表（`label` / `unlabeled` / `limit` / `offset` / `q` 全文检索） |
| GET | `/api/v1/jobs/export` | 导出历史数据 zip |
| POST | `/api/v1/jobs/import` | 导入历史数据 zip |
| GET | `/api/v1/jobs/{job_id}` | 任务详情（含完整进度事件链） |
| GET | `/api/v1/jobs/{job_id}/progress` | 进度事件 |
| POST | `/api/v1/jobs/{job_id}/cancel` | 取消 |
| POST | `/api/v1/jobs/{job_id}/retry` | 重试（可换模板） |
| DELETE | `/api/v1/jobs/{job_id}` | 删除（仅终态） |
| GET | `/api/v1/jobs/{job_id}/result` | 产物路径 |
| GET | `/api/v1/jobs/{job_id}/file` | 读取单个产物内容 |
| GET, HEAD | `/api/v1/jobs/{job_id}/export` | 导出单个产物（`format=md`，真实 attachment） |
| PUT | `/api/v1/jobs/{job_id}/summary` | 保存编辑后的总结（覆盖写回产物） |
| PUT | `/api/v1/jobs/{job_id}/labels` | 设置任务标签 |
| GET | `/api/v1/settings` | 任务默认配置 |
| PUT | `/api/v1/settings` | 修改任务默认配置 |
| GET | `/api/v1/templates` | 模板列表（含内置只读名单） |
| GET | `/api/v1/templates/{template_name}` | 单个模板 |
| PUT | `/api/v1/templates/{template_name}` | 保存自定义模板 |
| DELETE | `/api/v1/templates/{template_name}` | 删除自定义模板（内置只读） |
| GET | `/api/v1/llm` | LLM 配置（推理模型 / 语音识别模型两槽位，Key 掩码） |
| PUT | `/api/v1/llm` | 更新 LLM 槽位配置（summary / asr） |
| POST | `/api/v1/llm/import` | 从 `.env` 导入 LLM 配置 |
| GET | `/api/v1/labels` | 标签列表 |
| POST | `/api/v1/labels` | 新建标签 |
| PUT | `/api/v1/labels/{label_id}` | 重命名标签 |
| DELETE | `/api/v1/labels/{label_id}` | 删除标签 |
| POST | `/api/v1/labels/merge` | 合并标签 |
| POST | `/api/v1/cookies/test` | 预检浏览器 cookies 可读性（含 YouTube / B 站登录态提示） |
| GET | `/api/v1/fs/browse` | 浏览本机目录（本地文件源） |
| POST | `/api/v1/fs/pick` | 系统原生文件选择框（macOS / Windows） |
| GET | `/api/v1/logs/export` | 导出脱敏诊断日志 |

设置 `VIDEO_TO_SUMMARY_TOKEN` 后，所有 `/api/v1` 请求需带 `Authorization: Bearer <TOKEN>`
或 `X-Auth-Token`。暴露到局域网/公网前**必须**配置。
