# 实时采集：Hermes shell hooks

## 事件与字段

Hermes 的 shell hook 每条事件把**一个 JSON 对象**写进脚本 stdin。htrace 订阅：

| 事件 | 拿到什么 | 用途 |
| --- | --- | --- |
| `pre_tool_call` | `tool_name`、`tool_input`、`session_id`、`tool_call_id` | 起始时刻 |
| `post_tool_call` | 同上 + `result`、`duration_ms` | 真实耗时、结果、成败 |
| `post_api_request` | `model`、`provider`、`api_duration`、`usage`、`finish_reason` | 每次请求的耗时与 token |
| `on_session_end` | `completed` / `failed` / `model` | 会话收尾 |
| `subagent_stop` | `parent_session_id`、`child_status`、`child_summary`、`duration_ms` | 子代理链路 |

两种 payload 形状都会出现，`htrace/hooks_jsonl.py` 两种都认：

```jsonc
// 1) hermes hooks test 文档里的扁平形状
{"hook_event_name":"post_tool_call","tool_name":"terminal","args":{...},
 "result":"...","duration_ms":42,"session_id":"s","tool_call_id":"c"}

// 2) 实测的运行形状：业务字段在 extra 里，参数叫 tool_input
{"hook_event_name":"post_tool_call","tool_name":"terminal",
 "tool_input":{"command":"echo hello"},"session_id":"s","cwd":"...","profile":"custom",
 "extra":{"task_id":"t","tool_call_id":"c","result":"...","duration_ms":42},
 "_htrace_ts":1793085040.11}
```

## 安装

```bash
python hooks/install.py            # self-test + 打印 YAML，不动任何配置
python hooks/install.py --snippet  # 只打印 YAML
```

把打印出的块加进**你想采集的那个 profile** 的 `config.yaml`（profile 隔离，互不影响）：

```yaml
hooks:
  post_tool_call:
    - command: '"<python>" "<...>/hooks/htrace_hook.py"'
      timeout: 5
```

或者显式让安装器写：

```bash
python hooks/install.py --write "<profile home>/config.yaml"
python hooks/install.py --remove "<profile home>/config.yaml"   # 撤掉，只删 htrace 条目
```

`--write` 会先备份成 `config.yaml.htrace.bak`，只替换含 `htrace_hook.py` 的条目，
其他 hook 原样保留。**默认什么都不改**，开关完全在你手里。

首次使用需要授权（Hermes 的 shell-hook 白名单）：

```bash
hermes --accept-hooks        # 交互式跑一次
# 或在该 config 里：hooks_auto_accept: true
hermes hooks list            # 应显示 ✓ allowed
hermes hooks doctor          # 检查可执行、JSON 合法、合成运行耗时
```

## 采集与合成

事件落在 `$HERMES_HOME/htrace/events-YYYYMMDD.jsonl`（`HTRACE_DIR` 可覆盖）。

```bash
python -m htrace build --hooks "$HERMES_HOME/htrace" --out trace.json
```

合并规则：

- 同一 `tool_call_id` 已在 `state.db` 里配对的，不重复添加；只补 `state.db` 没有的调用
  （实时事件比落库更早、或会话正在进行中时）。
- 只有 hook、没有 `state.db`（新 profile / 容器里）也能出 trace：会话表里会出现
  `(hook-only session)`。
- 回填的调用在前端带 `live` 标签，和 db 来源区分开。

## 验证过的事实

在本机用独立 `HERMES_HOME` 跑过真实链路（不是只跑单元测试）：

```
$ HERMES_HOME=<tmp> hermes hooks test post_tool_call
  → hook exit=0 elapsed=0.234s
  → <tmp>/htrace/events-20261027.jsonl 写入 1 行
  → python -m htrace build --hooks <tmp>/htrace
    sessions=1 tool_calls=1  (terminal, 42ms)
```

合并路径同样验过：给一个真实会话 id 注入 1 条 `post_tool_call`，`--hooks` 后
`hook_calls_merged=1`，会话里多出一条 `source=hook` 的调用，其余 100 条 db 调用不受影响。
