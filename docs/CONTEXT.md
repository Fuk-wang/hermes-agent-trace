# Context view — what actually goes into each request

The Context tab answers a different question from the rest of htrace. Tool-call views say *what the
agent did*; this one says **what the provider was given** on request N: six categories, their sizes,
and — for the most recent requests — the text itself.

## Data path

```
Hermes process
  pre_api_request  ──►  ctx_req   (six category sizes + CJK counts + system-prompt sections)
  post_api_request ──►  ctx_resp  (usage: prompt_tokens, output_tokens, cache_read)
        │
        └─►  <HERMES_HOME>/htrace-context/context-YYYYMMDD.jsonl   (sizes only)
             <HERMES_HOME>/htrace-context/raw/ctx-*.json            (last 50 requests, pruned)
                       │
                       ▼
  python -m htrace build --context <store> --raw-out demo/context.raw.js
                       │
                       ▼
        trace.json  → context.requests[]   (sizes, sections, tokens, deviation, has_raw)
        context.raw.js → window.HTRACE_CTX_RAW (per-category text, capped, last 50)
```

Why a plugin and not a shell hook: the shell hook payload for `post_api_request` carries counts and
usage only. `pre_api_request` is a *plugin* hook and receives `system_prompt`, `request_messages`,
`tool_count` and `request.body.tools` — but only plugins can subscribe to it, and a plugin loads only
when it is named in `plugins.enabled`. `python -m htrace plugin --install --home <profile>` does the
copy **and** the enable, after backing `config.yaml` up.

## The six categories

Sizes are characters of what the provider receives, nothing re-tokenised.

| key | what it is | how it is measured |
| --- | --- | --- |
| `sys` | system prompt minus injected blocks | markdown headings that don't name an injected block |
| `injected` | memory, SOUL, skills index, runtime environment | headings matching `memor|skill|environment|soul|persona|agents.md|claude.md|instructions|workspace|project|sandbox|user profile` |
| `tools` | tool JSON schemas as sent | `json.dumps(request.body.tools)` |
| `user` | user messages | `content` + `tool_calls` JSON |
| `assistant` | assistant messages | same |
| `tool` | tool results | same, `tool` role |

Two traps this handles, each one observed in a real capture:

1. **OpenAI-style requests put the system prompt in `messages[0]`** while the hook's
   `system_prompt` is `None`. Counting both double-counts the whole prompt (45,780 instead of
   22,890 chars on the first capture). The plugin recovers it from `messages[0]` and the segmenter
   skips role `system` when it already measured the prompt.
2. **Headings only match with `re.M`.** Without it the whole prompt falls into one `sys` block and
   `injected` reads 0 — a silent five-way split pretending to be six.

## Token calibration

`tokens ≈ Σ_c (non_cjk_chars_c / cpt_c) + cjk_chars / cpt_cjk`

* Objective: least squares against the provider's own `prompt_tokens` (fallback: `input + cache_read`
  — DeepSeek reports `input_tokens` **excluding** cached tokens, so using it alone would underestimate
  the prompt by an order of magnitude).
* Solver: coordinate descent with a weak ridge toward 4.0 chars/token (1.35 for CJK). The ridge settles
  the direction of columns that do not vary in the sample; those are reported as
  `identifiable: false` and the UI says so instead of pretending the split is measured.
* **CJK is a shared column.** One chars/token per category cannot cover both a Chinese HTML dump and an
  ASCII JSON blob; without the CJK split, a single `read_file` of a Chinese page left a 12% residual on
  that request (leave-one-out), which is exactly the kind of request people open this tab for.
* **Leave-one-out** is the number that decides `ok`: in-sample error flatters a 7-parameter fit. Both are
  in `context.calibration`. The page shows `ESTIMATED` whenever either exceeds 5%.

Reference run (16 requests, monitor + product profiles, 2026-11-03):

| | median | p90 | max |
| --- | --- | --- | --- |
| in-sample | 0.30% | 0.68% | 0.72% |
| leave-one-out | 0.49% | 0.92% | 2.91% |

Coefficients from that run: sys 3.99 · injected 3.31 · tools 4.38 · user 5.00 · assistant 3.89 ·
tool 1.32 · CJK 1.66 chars/token. Calibration is only as good as its sample: sessions whose
static part never changes (one profile, one toolset) cannot identify `sys` vs `injected`, and the
build says so in `prior_only`.

## Volume and privacy

* `trace.json` holds per-request sizes, section headings, counts and tokens — **no text**.
* Raw text lives in the sidecar (`context.raw.js` / `.json`), only for the newest `--raw-keep` (default
  50) requests; each category is capped at 6,000 chars and the file at 3M chars, with `truncated` set.
* The plugin prunes its own `raw/` to 50 files at write time, so a long-running profile does not
  accumulate prompt text on disk.
* `--raw-context <store>` restricts which stores may contribute raw text while still calibrating on
  all of them. The public demo uses it: monitor's prompt is published, product's (which carries
  memory notes) only contributes sizes.
* `--redact` runs the same scrubbing over the context document and the sidecar.
