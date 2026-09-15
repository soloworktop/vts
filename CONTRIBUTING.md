# 贡献指南

感谢你愿意为 VTS 出力。这一页很短：了解项目定位、开发约定与提交流程，然后动手就好。

## 这个项目是什么

VTS 是一个**功能完整的开源产品**：视频 URL → 字幕/转写 → LLM 摘要 → Markdown 笔记。
定位是**自部署、单用户、BYOK**（用户自备任意 OpenAI 兼容端点）。

本仓提供的功能是**完整的、可长期使用的**，没有隐藏门控、没有限期降级、没有水印。

## 范围宣言

**BYOK、字幕优先、单用户自部署**，是理解这个项目的三把钥匙，功能取舍都从这里出发：

- **Markdown 是唯一一等产物**——需要 PDF 等格式，用 pandoc 等工具在本地转换即可；
- **转写走 OpenAI 兼容接口**（字幕优先，没有字幕才调用）——本地模型文件托管（含下载、
  GPU 依赖）不在路线图内；有自己的推理网关的话，把它暴露成兼容端点即可直接使用；
- **登录态由用户自己解决**——`--cookies` 与浏览器 cookies 已经支持，不做站点登录集成；
- **单用户工具**——不做账号与权限体系，多人使用请各自部署。

任何形式的授权门控与用量上报机制都与项目原则相悖，
不在计划之内；拿不准某个想法是否合适，先开 issue 聊聊，对齐方向再动手最省时间。

## 开发约定

仓库的约定与铁律集中在根目录 `AGENTS.md`——API 契约（`/api/v1` canonical、接口表
双向同步）、密钥处理（Fernet 加密、接口只回掩码）、插件挂载点（只经 `entry_points`，
不做硬耦合）、常量与默认值的归属等，动手前请读一遍；多项约定有防漂移测试强制
（如 `tests/test_doc_consistency.py`）。

## 文档维护约定

多份文档并存，靠三条约定防止同一事实在两处漂移：

1. **两语言 README 章节树同构**：`README.md` 与 `README.en.md` 的二级节一一对应，
   增删二级节必须两版同步（例外需在两侧注明原因）；
2. **跨文件事实单一权威 + 互链**：接口表见 `docs/api.md`、环境变量语义见
   `docs/configuration.md`、插件机制见 `docs/plugins.md`；「明确不做的事」以两语言
   README 为权威，B 站等站点风控解法以 `docker/README.md`「网络与风控」为权威——
   其余位置只放一句话指引 + 链接，**禁止全量复述**；
3. **`docker/README.md` 内同一语义最多出现一次**：表格单元格只放一句话 + 指向小节
   的链接，正文展开。

## 欢迎的贡献

- **Bug 修复**，尤其是 yt-dlp / 字幕提取 / cookies 这些真实踩坑点
- **新增总结模板**（`summarizers/openai.py` 的 `SUMMARY_TEMPLATES`，附一份真实产物示例）
- **新的 OpenAI 兼容端点适配**（若需要特殊参数，走 LLM 槽位配置字段扩展而非硬编码供应商分支）
- **文档、示例、翻译**（README 英文版尤其欢迎）
- **测试**：离线用例覆盖率、边界用例
- **性能与稳定性**：长视频、大并发、异常恢复

## 开发流程

```bash
git clone <this-repo> && cd vts
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q                     # 必须全绿（默认全离线）
```

- 端到端：`pip install -e ".[e2e]" && playwright install chromium && bash scripts/e2e.sh`
- 真实边界端到端（会用掉 .env 里 Key 的真实调用量，默认跳过）：在 `.env` 配好 Key 与 `VTS_LIVE_TEST_URL` 后 `bash scripts/e2e.sh live`
- 需要联网的用例默认跳过，用 `VTS_NETWORK_TESTS=1` 开启
- 提交前请确认 `git status` 里没有 `.env` / `data/` / `output*/` / `*.db`

### 提交与 PR

- 一个小提交只做一件事；提交信息用中文，格式 `type: 说明`
  （`feat` / `fix` / `refactor` / `docs` / `test` / `chore`）
- PR 描述里写清：**改了什么、为什么、怎么验证**；UI/契约变更加前后对比
- 改了 HTTP 路由 / 进度事件时，记得同步 `docs/api.md` 的接口表；改了内置模板时同步
  `README.md` 的模板清单——有防漂移测试会强制这一点
- 新增依赖请说明必要性；本项目的依赖面刻意保持很小

## 报告问题

- **Bug**：附复现步骤 + `GET /api/v1/logs/export` 导出的脱敏诊断日志
  （含环境、最近任务、运行日志，已自动脱敏）
- **功能建议**：先开 issue 描述使用场景与收益，对齐方向后再动手

## 许可

贡献即表示你同意以 [MIT](LICENSE) 许可发布你的贡献。
