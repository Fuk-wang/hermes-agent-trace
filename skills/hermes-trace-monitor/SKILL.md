---
name: hermes-trace-monitor
description: "Use when monitoring another Hermes agent's work. Reads htrace traces to prove what an executor agent actually did, and reports stuck, failing, or expensive sessions with call-level evidence."
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [hermes, monitoring, observability, trace, multi-agent]
---

# Hermes trace monitor

You are the **monitor**. Another Hermes profile (the executor) is doing real work.
You never trust its summary: you read `state.db` and htrace, and you report only what
the records show. Every claim you make must carry a `call_id` or a session id.

## Setup

Executor home: `$EXECUTOR_HOME` (e.g. `%LOCALAPPDATA%/hermes/profiles/product`).
Tool repo: the `hermes-agent-trace` checkout that contains `htrace/`.

```bash
# 1) fresh trace, merging live hook events if the executor has them
python -m htrace build \
  --home "$EXECUTOR_HOME" \
  --hooks "$EXECUTOR_HOME/htrace" \
  --source "<hermes-agent source>" \
  --out "$WORK/trace.json" --limit 5

# 2) machine summary
python -m htrace stats --trace "$WORK/trace.json"
```

If `python -m htrace build` fails with "No Hermes state.db found", the executor has never
run a session in that home — say so, do not invent a report.

## Checks, in priority order

Each check reads `trace.json` (or `state.db` directly for timestamps). Report the first
three findings that hold; stop when the executor looks healthy.

1. **Stalled** — newest session's last `ended_at` or last call `started_at` is older than
   `stalled_after` (default 10 min) while the session has no `ended_at`. Capture: session
   id, last call name, gap. Look at `timeline` gaps > 5 min too: a 25-minute `clarify`
   is a waiting-on-human, not a stall — label it accordingly.
2. **Failing** — calls with `status=error` in the newest session, or `pending` calls whose
   `started_at` is older than 2 min (never paired → interrupted or crashed mid-tool).
   Quote the tool name, `call_id`, and the first 200 chars of the result.
3. **Looping** — the same tool name with the same arguments (normalized `args_raw`) ≥ 3
   times in one session. This is the agent retrying a broken approach; show the repeated
   argument and the sequence numbers.
4. **Burning budget** — `tokens.input + tokens.output` or `cost_usd` in the newest session
   above 2× the median of the other sessions in the trace, or call count rising while
   `stats.errors` stays > 0.
5. **Subagent trouble** — `subagents[]` entries with `state` not `completed`, or a
   `completed` child whose parent session has no later activity (the handoff was dropped).
6. **Claim vs record** — if the executor says "done / fixed / deployed", verify: does the
   trace contain the tool call that would have done it (e.g. `patch`/`write_file` to the
   claimed path, `git push`, `gh pr create`)? No such call → the claim is unverified.
   Compare the claimed path with the actual `args.path` values.

## Report format

```
状态：健康 | 需要关注 | 卡住
证据
  · [session 20261027_140730_eee1c0] tool=terminal call_id=call_00_… 耗时 12m03s status=ok
  · [session …] status=error 3 次：browser_exec(2), terminal(1) — 首次 call_00_…
判断
  · <一句话结论，只覆盖上面的证据>
建议
  · <下一步：等 / 重派 / 换路径 / 让它先跑 X 验证>
```

Rules:

- No evidence → write `状态：无法判断` and say what is missing (no state.db / no hook events).
- Never paste secrets, tokens, or full file contents into the report; excerpts are fine.
- Never modify the executor's config, hooks, or database. Read-only: open `state.db` with
  `mode=ro` (htrace already does).
- If the executor is mid-run, prefer "still running, last activity Xs ago" over "stuck".

## What the numbers mean

- `duration_ms` from `state.db` = gap between the assistant message and its tool result
  landing. Includes small write overhead; fine for ranking, not for billing.
- `duration_ms` from hooks = Hermes' own timer; calls sourced from hooks carry
  `"source": "hook"` and render with a `live` tag.
- `pending` is not an error: the call exists, its result does not (yet). Age decides.
- Cost is `estimated_cost_usd` from the session row — model pricing snapshot, not an invoice.

## Keeping the monitor itself cheap

- Build with `--limit 5`. Full history is not needed for a health check.
- `python -m htrace stats` first; only open the viewer (`index.html`) when a human asks
  to look, or when you need per-call args.
- Do not re-build in a loop faster than every 60s; each build reads the whole db file.
