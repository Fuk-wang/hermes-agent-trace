# htrace — Hermes Agent 工具调用追踪

[在线演示](https://fuk-wang.github.io/hermes-agent-trace/) · [![tests](https://github.com/Fuk-wang/hermes-agent-trace/actions/workflows/tests.yml/badge.svg)](https://github.com/Fuk-wang/hermes-agent-trace/actions/workflows/tests.yml) · 参考对象：[thevibeworks/cctrace](https://github.com/thevibeworks/cctrace)

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
| 仓库根目录的静态站 | 渲染 `trace.json`：总览 / 会话 / 工具调用 / 上下文 / 时间线 / 成本 / 子代理 / 源码索引 / 原始数据 | 任何浏览器，零 CDN、离线可用 |
| `hooks/` | 可选：把实时事件写进 JSONL，再合并进 trace | Hermes shell hooks |
| `plugins/htrace_context/` | 可选：采样每次请求的六类上下文尺寸（+最后 50 条原文） | Hermes 插件（`pre_api_request` / `post_api_request`） |

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

## 上下文视图（Context）

第四个问题：**这次请求到底往模型里塞了什么**。每条请求画一条六类堆叠条——`sys`（system prompt 去掉
注入块）、`injected`（memory / SOUL / skills 索引 / 运行时环境）、`tools`（工具 JSON schema）、
`user` / `assistant` / `tool`（对话与工具结果）。点「看原文」开抽屉看该段文字。

token 不猜：`pre_api_request` 记每类字符数（CJK 单独计数），`post_api_request` 给真实
`prompt_tokens`，build 时拟合每类 chars/token，并做留一交叉验证；偏差超过 5% 整页标 `estimated`。
参考跑批：16 条请求，样本内中位偏差 0.30% / 最大 0.72%，留一交叉验证中位 0.49% / p90 0.92% /
最大 2.91%。细节见 [docs/CONTEXT.md](docs/CONTEXT.md)。

```bash
# 装采集插件（改 config.yaml 前自动备份；--remove 可撤，采集数据保留）
python -m htrace plugin --install --home "$LOCALAPPDATA/hermes/profiles/monitor"
python -m htrace plugin --status  --home "$LOCALAPPDATA/hermes/profiles/monitor"

# 重建 trace 时把插件数据带进来
python -m htrace build --out trace.json --source "<hermes-agent>" \
  --context "$LOCALAPPDATA/hermes/profiles/monitor/htrace-context" \
  --raw-out demo/context.raw.js
```

## 数据与隐私

- 本地优先：解析、渲染都在本机；静态站不发任何请求（`demo/trace.demo.js` 除外，它是本地文件）。
- 公开 demo 用的是真实会话，但做了完整脱敏：`.env` 里的密钥值、`sk-`/`ghp_` 等 key 形状、
  私钥块、Bearer 头、邮箱、家目录用户名。脱敏统计写在 trace 的 `source.redaction_stats` 里。
- 用户自己拖进网页的 `trace.json` **不会**被自动脱敏——那是原文，分享前请自己处理，
  或用 `python -m htrace build --redact` 重新导出一份。详见 [docs/PRIVACY.md](docs/PRIVACY.md).

- **上下文视图的原文是分开存的**：`trace.json.context` 只有每请求的分段尺寸与摘要，正文走
  `--raw-out` 侧车，只保留最近 50 条请求（插件写盘时按文件数裁剪，build 时再截一次）；单类上限
  6k 字符并标 `truncated`。
- `--raw-context` 限定「只有这些 profile 能贡献原文」，其他 profile 只参与校准、不出文本。
- 记忆（`MEMORY`）与用户档案（`USER PROFILE`）正文默认不发布：段头与 `[x/y chars]` 体量保留，
  正文换成 `（已脱敏 · N 字符…）` 占位；确实要连正文发才加 `--raw-include-personal`。
  公开 demo 用的是这条规则，`context.personal_notes` 会写 `replaced-by-size`。

## 自检记录（2026-11-03）

- 真机采集 16 条请求（monitor + product 两个 profile）；六类系数全部可识别，样本内中位偏差 0.30% /
  最大 0.72%，留一交叉验证中位 0.49% / p90 0.92% / 最大 2.91% → `calibration.ok = true`
- 插件安装按惯例：装 monitor → 跑通 → 再装 product；`--remove` 撤掉后采集数据保留，重装即恢复
- 公开侧车 `demo/context.raw.js` 里 `SNI` 出现 0 次（改前 4 次）；每个带原文的请求脱掉 3,419–5,353 字符
  记忆/档案正文，抽屉里只剩段头 + `（已脱敏 · 2,130 字符…）` 占位
- 测试：`python -m unittest discover -s tests` → 17 tests OK
- 渲染：无头 Chrome 打开 `#context` → 16 行请求、六段堆叠条 6/6；深链 `#context/<key>/<类>` 直开抽屉
- 请求 ↔ 工具调用双向跳转：`link_calls()` 按会话内时间窗配对（`state.db` 没有 `api_request_id`），
  16 条请求里 6 条连到 8 次调用；详情页列出「这一轮调了 N 个工具」，调用抽屉给「上下文 N tok · 第 K 次请求」
- 原文按需展开：侧车单段上限 40k，抽屉首屏 6k + 「展开剩余」，侧车 329 KiB / gzip 86 KiB
- CI：`tests.yml` 跑 ubuntu+windows × py3.10/3.11/3.12 共 6 个 job，首次运行全绿，README 挂徽章

## 目录

```
index.html            静态站入口（也是 Pages 首页）
assets/               app.js / style.css
demo/trace.demo.js    内置演示数据（真实 trace + 完整脱敏）
demo/context.raw.js   上下文原文侧车（最近 50 条请求，记忆正文已脱敏）
htrace/               Python 解析器与 CLI（含 context.py / segments.py / plugin_install.py）
plugins/htrace_context/  采集插件：pre_api_request 记尺寸，post_api_request 记 usage
hooks/                shell hook 与安装器
skills/hermes-trace-monitor/  监控 agent 用的 skill
docs/                 架构、hook 契约、隐私、上下文视图（CONTEXT.md）
```

## 已知边界

- `duration_ms` 在只有 `state.db` 时是「助手消息落库 → 工具结果落库」的间隔，包含少量落库开销；
  接了 hook 后是 Hermes 自己测的真实耗时（`duration_ms`），两者在 UI 里用 `live` 标签区分。
- 并行工具调用的配对靠 `tool_call_id`，缺失时该调用会显示为 `pending`，不会被静默丢弃。
- 子代理的调用记在子会话里；父会话只保留委派边（`async_delegations` / `parent_session_id`）。
- 跨 profile 聚合已支持（`--home` 可重复或自动发现 `profiles/*`），但公网 demo 只放 default。

## License

MIT
