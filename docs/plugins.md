# 插件与扩展（plugins）

核心只定义挂载点，第三方插件通过标准 entry point 挂载，**核心代码不引用任何具体插件
包名**（硬耦合写法会被 `scripts/banned_words_lint.py` 禁词门禁拦下）。本构建不含任何
插件——0 插件即预期状态，产品按完整 BYOK 形态运行。入口说明见 `README.md`「进阶」。

## 挂载点

插件在其发行版的 `pyproject.toml` 里声明 entry point（group 固定为 `vts.plugins`）：

```toml
[project.entry-points."vts.plugins"]
my-plugin = "my_plugin:plugin"
```

插件实现 `register(hooks)`，可注册三类挂载点：

| 挂载点 | 作用 |
|---|---|
| `RouteProvider` | 注册额外 HTTP 路由（挂在 `/api/v1` 之外的自有前缀下） |
| `JobSideEffect` | 任务完成后的副作用（如推送通知、外部归档） |
| `SettingsProvider` | 提供额外设置项（前端设置页据此渲染） |

并可调用 `hooks.declare_capabilities({...})` 声明可选能力。

## 能力协商（`/api/v1/capabilities`）

`GET /api/v1/capabilities` 返回各插件 `declare_capabilities` 声明能力的**并集**；
本构建不含任何插件，响应为 `{}`。安装了插件的部署中，前端据此渲染插件提供的可选功能。

## fail-closed 判据

插件发现是 **fail-closed**（宁可拒绝启动，绝不带病降级）的：

- 查到 0 个 entry point = 本构建的预期状态，正常启动；
- 查到条目但加载失败（导入错误、`register` 异常等）→ **抛致命错误拒绝启动**，
  绝不静默跳过降级运行（实现见 `web/hooks.py`）。

---

与 `web/hooks.py` / `web/capabilities.py` 的实现保持同步：改插件语义时先改代码，
再同步本文与 `AGENTS.md` 铁律 3。
