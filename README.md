# htrace — Hermes Agent 工具调用追踪

[在线演示](https://fuk-wang.github.io/hermes-agent-trace/) · 参考对象：[thevibeworks/cctrace](https://github.com/thevibeworks/cctrace)

htrace 把 Hermes Agent 的一次或多次会话，还原成可点开、可核验的调用记录：**每一次工具调用**
的名字、参数、结果、开始时间、耗时、token 开销，以及**真正执行它的源码位置**。

它回答三个问题：

1. 这个 agent 到底做了什么？——按会话还原调用序列，参数与结果原文都在。
2. 什么时候调的、等了多久？——时间线 + 调用密度 + 最长空窗，卡住一眼能看出来。
3. 是模型说的，还是真的发生了？——工具名静态索引到 `tools/xxx.py:行号`，点条目对源码。

## 两个部分

| 部分 | 作用 | 依赖 |
| --- | --- | --- |
| `htrace/`（Python，纯标准库） | 读 `state.db`、读 hook JSONL、扫源码生成工具索引，产出 `trace.json` | Python 3.10+ |
| 仓库根目录的静态站 | 渲染 `trace.json`：总览 / 会话 / 工具调用 / 时间线 / 成本 / 子代理 / 源码索引 / 原始数据 | 任何浏览器，零 CDN、离线可用 |
| `hooks/` | 可选：把实时事件写进 JSONL，再合并进 trace | Hermes shell hooks |

## 快速开始（离线）

```bash
cd hermes-agent-trace

# 1) 生成 trace（默认自动找 $HERMES_HOME 和 profiles/*）
python -m htrace build --out trace.json --source "C:/Users/<you>/AppData/Local/hermes/hermes-agent"

# 2) 打开 index.html，点左下「打开 trace.json」
#    或者起个本地服务（同时预留了 /api/trace 后端接口）
python -m htrace serve --dir . --port 8765
```

`--source` 指向 Hermes 源码目录（`hermes-agent/`），用来生成「工具名 → 文件:行」的索引。
不传也能用，只是源码索引页为空。

常用参数：

```bash
python -m htrace build --limit 10            # 只看最近 10 个会话
python -m htrace build --home <profile home> # 指定某个 profile
python -m htrace build --hooks <dir>         # 合并 hook 实时事件
python -m htrace build --redact              # 导出前脱敏（密钥 / 家目录 / 邮箱）
python -m htrace stats --trace trace.json    # 命令行摘要
python -m htrace homes                       # 列出可发现的 Hermes home
```

## 实时采集（可选，默认关闭）

```bash
python hooks/install.py             # 只做 self-test + 打印 YAML，不改任何配置
python hooks/install.py --write <某个 profile 的 config.yaml>   # 显式写入，自动备份
python hooks/install.py --remove <同一个文件>                    # 撤掉 htrace 的条目
```

采集到 `$HERMES_HOME/htrace/events-YYYYMMDD.jsonl`，然后：

```bash
python -m htrace build --hooks "$HERMES_HOME/htrace" --out trace.json
```

事件、payload 两种真实形状、首次授权（`--accept-hooks`）见 [docs/HOOKS.md](docs/HOOKS.md)。

## 溯源是怎么做的

Hermes 的每个工具在导入时调用 `registry.register(name=..., handler=...)`。htrace 只对
`tools/*.py` 做静态解析，得到：

```json
{"read_file": {"file": "tools/file_tools.py", "line": 1346,
               "handler": "_handle_read_file", "handler_line": 1263, "toolset": "file"}}
```

不导入任何 Hermes 代码，不启动 agent，也不联网。点开任意一次调用，抽屉里给出实现位置，
用来核对模型的自述。

## 数据与隐私

- 本地优先：解析、渲染都在本机；静态站不发任何请求（`demo/trace.demo.js` 除外，它是本地文件）。
- 公开 demo 用的是真实会话，但做了完整脱敏：`.env` 里的密钥值、`sk-`/`ghp_` 等 key 形状、
  私钥块、Bearer 头、邮箱、家目录用户名。脱敏统计写在 trace 的 `source.redaction_stats` 里。
- 用户自己拖进网页的 `trace.json` **不会**被自动脱敏——那是原文，分享前请自己处理，
  或用 `python -m htrace build --redact` 重新导出一份。详见 [docs/PRIVACY.md](docs/PRIVACY.md).

## 目录

```
index.html            静态站入口（也是 Pages 首页）
assets/               app.js / style.css
demo/trace.demo.js    内置演示数据（真实 trace + 完整脱敏）
htrace/               Python 解析器与 CLI
hooks/                shell hook 与安装器
skills/hermes-trace-monitor/  监控 agent 用的 skill
docs/                 架构、hook 契约、隐私
```

## 已知边界

- `duration_ms` 在只有 `state.db` 时是「助手消息落库 → 工具结果落库」的间隔，包含少量落库开销；
  接了 hook 后是 Hermes 自己测的真实耗时（`duration_ms`），两者在 UI 里用 `live` 标签区分。
- 并行工具调用的配对靠 `tool_call_id`，缺失时该调用会显示为 `pending`，不会被静默丢弃。
- 子代理的调用记在子会话里；父会话只保留委派边（`async_delegations` / `parent_session_id`）。
- 跨 profile 聚合已支持（`--home` 可重复或自动发现 `profiles/*`），但公网 demo 只放 default。

## License

MIT
