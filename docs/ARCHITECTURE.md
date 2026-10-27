# htrace 架构

## 一条数据从哪来

```
Hermes 运行时
├── state.db (SQLite)                 每个会话持久化消息、工具调用、token、成本
│     sessions / messages / async_delegations
└── shell hooks (可选)                pre_tool_call / post_tool_call / post_api_request
      └── hooks/htrace_hook.py  →  $HERMES_HOME/htrace/events-*.jsonl
                                        ↓
htrace (Python, 无第三方依赖)
├── htrace/state_db.py      读 db（只读 URI），pair tool_calls ↔ tool 结果，算耗时/状态/统计
├── htrace/hooks_jsonl.py   读 JSONL，兼容两种 payload 形状，fold 成 calls/api
├── htrace/source_index.py  静态扫 tools/*.py 的 registry.register(...) → 工具源码索引
├── htrace/redact.py        密钥/家目录/邮箱脱敏（只给公开 demo 用）
└── htrace/build.py         合成 trace 文档 (schema htrace/1)
                                        ↓
trace.json  →  index.html + assets/app.js（纯前端渲染，无网络请求）
```

## trace 文档结构（schema `htrace/1`）

```jsonc
{
  "schema": "htrace/1",
  "generated_at": "2026-10-27T15:20:00+08:00",
  "source": {
    "mode": "state_db | merged",
    "homes": [{"profile": "default", "home": "...", "state_db": "..."}],
    "hook_files": ["..."], "hook_calls_merged": 3,
    "redacted": true, "redaction_stats": {"username": 533, "home_path": 780},
    "source_index_root": "..."
  },
  "totals": {"sessions": 2, "calls": 131, "errors": 4, "pending": 1,
             "total_ms": 3865357, "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cost_usd": 0.1194},
  "by_tool": {"terminal": {"calls": 79, "errors": 1, "ms": 2810000, "bytes": 12345, "sessions": 2}},
  "sessions": [{
    "id": "20261027_140730_eee1c0", "title": "...", "profile": "default",
    "model": "deepseek-flash", "started_at": "...", "cwd": "...", "git_branch": "...",
    "tokens": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 0, "reasoning": 0},
    "cost_usd": 0.05, "tool_names": ["..."],
    "calls": [{
      "call_id": "call_...", "name": "read_file", "turn": 3, "seq": 7,
      "started_at": "2026-10-27T14:12:02", "ended_at": "2026-10-27T14:12:05",
      "duration_ms": 2351,
      "args": {"path": "..."}, "args_raw": "{...}", "args_bytes": 95, "args_truncated": false,
      "status": "ok | error | pending", "result": "...", "result_bytes": 4096,
      "result_truncated": true, "source": "db | hook"
    }],
    "timeline": [{"kind": "user|assistant|tool", "at": "...", "text": "...", "chars": 120}],
    "stats": {"calls": 7, "errors": 0, "pending": 0, "total_ms": 4000,
              "p50_ms": 210, "max_ms": 2351, "by_tool": {...}},
    "api_calls": [{"model": "...", "duration_s": 1.9, "usage": {...}}]   // 仅 hook
  }],
  "tools": {"read_file": {"file": "tools/file_tools.py", "line": 1346,
                          "handler": "_handle_read_file", "handler_line": 1263,
                          "toolset": "file", "module": "tools.file_tools"}},
  "subagents": [{"kind": "delegation | child_session", "id": "...",
                 "parent_session": "...", "state": "completed",
                 "goal": "...", "dispatched_at": "...", "completed_at": "..."}]
}
```

## 配对与耗时的口径

- 助手消息的 `tool_calls` 是 JSON 数组，元素含 `id` / `call_id` / `function.name` /
  `function.arguments`。hermes 历史上出现过三种形状，`_call_entries()` 全部兼容。
- 工具结果消息按 `tool_call_id` 回填到调用上；找不到结果的调用保留为 `pending`，
  绝不丢弃——中断、未落库、只在 hook 里出现的情况都要看得见。
- `status=error` 只在工具结果**自己声明失败**时设置（`error` / `success:false` /
  `exit_code != 0` / `ok:false`），不从文本里猜。
- 耗时口径见 README「已知边界」。接了 hook 后 `duration_ms` 来自 Hermes 的计时，
  前端用 `live` 标签标出来。

## 源码索引

静态解析，不导入、不执行：

1. 找 `registry.register(` 调用；
2. 从后 2KB 窗口里取 `name=` / `toolset=` / `handler=`；
3. 在同文件的 `def <handler>(` 里取实现行号；
4. 补一遍 schema 字面量里的 `"name": "<tool>"`，覆盖循环注册（如 browser 系列）。

结果里 `handler: "lambda"` 的条目表示注册时传的是匿名函数，只有注册行可用。

## 前端

`assets/app.js` 是单文件渲染器：视图切换、过滤、搜索、抽屉、键盘导航都在里面；
数据只来自 `window.HTRACE_DEMO`（内置 demo）或用户选择的本地文件，或 `GET /api/trace`
（`htrace serve` 提供，静态部署时不存在）。没有构建步骤，没有外部请求。

## 为什么不用 MITM

cctrace 抓 TLS 是为了看「发出去的请求原文」。Hermes 这边 `state.db` 已经保存了
工具调用与结果，`request_dump_*.json` 在报错时保存完整请求体，所以不需要证书、不需要改
`base_url`、不需要接管流量。要看每次 API 请求的耗时与用量，用 `post_api_request` hook 即可。
