"""Context composition: per-request segment sizes + calibrated token estimates.

Data comes from the ``htrace-context`` plugin (``pre_api_request`` for the six
category sizes, ``post_api_request`` for the provider's real token usage). Nothing
here talks to a provider: the token counts are a least-squares fit of
``chars -> tokens`` per category against the real ``prompt_tokens`` of the requests
that were captured, and every request keeps its own deviation so the viewer can
label the numbers honestly.

    x_ic * beta_c   sums to estimated tokens,  beta_c = 1 / (chars per token)

Fit: coordinate descent with a weak ridge toward a 4.0 chars/token prior. The prior
only settles categories whose columns do not vary across the sample (a single
profile sends the same system prompt every time, so its coefficient is not
identifiable from that profile alone); those are reported as ``identifiable: false``
and the UI must not pretend otherwise.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import redact, segments

CATEGORIES = list(segments.CATEGORIES)
LABELS = dict(segments.LABELS)
PRIOR_CPT = 4.0          # chars per token before any data
RAW_KEEP_DEFAULT = 50
RAW_SEG_CAP = 6000       # chars per category per request in the sidecar
RAW_TOTAL_CAP = 3_000_000


# --------------------------------------------------------------------------- loading
def _expand(paths: Optional[Iterable[str]]) -> List[Path]:
    out: List[Path] = []
    for raw in paths or []:
        p = Path(raw).expanduser()
        if p.is_dir():
            out.extend(sorted(p.glob("context-*.jsonl")))
            for sub in sorted(p.glob("*/context-*.jsonl")):
                out.append(sub)
        elif p.exists():
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def read_events(paths: Optional[Iterable[str]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for path in _expand(paths):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("kind") in ("ctx_req", "ctx_resp"):
                    obj["_file"] = str(path)
                    events.append(obj)
        except OSError:
            continue
    return events


def _req_key(ev: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(ev.get("session_id") or ""),
        str(ev.get("api_request_id") or ""),
        str(ev.get("api_call_count") or ""),
    )


def _truth_tokens(usage: Dict[str, Any]) -> Optional[int]:
    """Real prompt size: prompt_tokens when the provider sends it, else input + cache."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    if isinstance(prompt, (int, float)) and prompt > 0:
        return int(prompt)
    inp = usage.get("input_tokens")
    cache = usage.get("cache_read_tokens") or 0
    if isinstance(inp, (int, float)) and inp > 0:
        return int(inp + (cache if isinstance(cache, (int, float)) else 0))
    return None


def pair(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Join each measured request with the usage its response reported."""
    reqs: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str, str]] = []
    for ev in events:
        key = _req_key(ev)
        if ev.get("kind") == "ctx_req":
            if key not in reqs:
                order.append(key)
            rec = reqs.setdefault(key, {"chars": {}, "raw_ref": None, "sections": [], "counts": {}})
            rec.update({
                "key": key,
                "session_id": ev.get("session_id") or "",
                "api_request_id": ev.get("api_request_id") or "",
                "turn_id": ev.get("turn_id") or "",
                "api_call_count": ev.get("api_call_count"),
                "ts": ev.get("ts"),
                "model": ev.get("model") or "",
                "provider": ev.get("provider") or "",
                "chars": {c: int((ev.get("chars") or {}).get(c) or 0) for c in CATEGORIES},
                "cjk": {c: int((ev.get("cjk") or {}).get(c) or 0) for c in CATEGORIES},
                "sections": ev.get("sections") or [],
                "counts": ev.get("counts") or {},
                "tools_present": bool(ev.get("tools_present")),
                "truncated": bool(ev.get("payload_truncated")),
                "raw_ref": ev.get("raw_ref"),
                "file": ev.get("_file"),
            })
    for ev in events:
        if ev.get("kind") != "ctx_resp":
            continue
        key = _req_key(ev)
        rec = reqs.get(key)
        if rec is None:
            # pre-hook line missing (plugin enabled mid-request): keep the usage only.
            continue
        truth = _truth_tokens(ev.get("usage") or {})
        if truth:
            rec["truth"] = truth
        rec["usage"] = ev.get("usage") or {}
        rec["finish_reason"] = ev.get("finish_reason")
        rec["api_duration"] = ev.get("api_duration")
    rows = [reqs[k] for k in order if reqs[k].get("chars")]
    rows.sort(key=lambda r: (r.get("ts") or 0))
    return rows


# --------------------------------------------------------------------------- fit
def _fittable(row: Dict[str, Any]) -> bool:
    return bool(row.get("truth")) and not row.get("truncated") and row.get("tools_present") and \
        sum(row["chars"].values()) > 0


def _column_identified(values: List[float]) -> bool:
    vals = [v for v in values]
    if not vals:
        return False
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return False
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return math.sqrt(var) / mean > 0.05


PRIOR_CJK_CPT = 1.35


def _design(rows: List[Dict[str, Any]]) -> List[List[float]]:
    """Columns: non-CJK chars per category (6) + CJK chars total (1).

    CJK runs at roughtly one token per character, ASCII prose/code at one per ~4, so
    a single chars/token per category mixes two populations and misfires on exactly
    the tool results people look at (a Chinese HTML dump sits in `tool`). The CJK
    column is shared: the tokenizer does not care which category the characters
    arrived in.
    """
    out: List[List[float]] = []
    for row in rows:
        chars = row.get("chars") or {}
        cjk = row.get("cjk") or {}
        cjk_total = sum(int(cjk.get(c) or 0) for c in CATEGORIES)
        out.append([float(int(chars.get(c) or 0) - int(cjk.get(c) or 0)) for c in CATEGORIES]
                   + [float(cjk_total)])
    return out


def _priors(prior: float) -> List[float]:
    return [1.0 / prior] * len(CATEGORIES) + [1.0 / PRIOR_CJK_CPT]


def _fit_once(x: List[List[float]], t: List[float], prior: float, iters: int) -> List[float]:
    """Coordinate descent on the ridge-regularised least-squares problem."""
    n, k = len(x), len(t[0]) if False else len(_priors(prior))
    scale = sum(sum(v * v for v in row) for row in x) / max(1, n)
    lam = max(1e-9, 1e-3 * scale)
    prior_vec = _priors(prior)
    beta = list(prior_vec)
    for _ in range(iters):
        for j in range(k):
            num = lam * prior_vec[j]
            den = lam
            for i in range(n):
                resid = t[i] - sum(x[i][m] * beta[m] for m in range(k) if m != j)
                num += x[i][j] * resid
                den += x[i][j] * x[i][j]
            beta[j] = max(0.0, num / den)
    return beta


def _loo_deviation(x: List[List[float]], t: List[float], prior: float, iters: int) -> List[float]:
    """Leave-one-out error: fit without a request, predict it. In-sample error flatters a 7-parameter fit."""
    out: List[float] = []
    n = len(x)
    if n < 10:
        return out
    for i in range(n):
        xs = [x[j] for j in range(n) if j != i]
        ts = [t[j] for j in range(n) if j != i]
        beta = _fit_once(xs, ts, prior, max(80, iters // 3))
        pred = sum(x[i][m] * beta[m] for m in range(len(beta)))
        if t[i] > 0:
            out.append(abs(pred - t[i]) / t[i])
    return out


def fit(rows: List[Dict[str, Any]], *, prior: float = PRIOR_CPT, iters: int = 300) -> Dict[str, Any]:
    """Least-squares chars→tokens coefficients, CJK split out, weak ridge prior."""
    sample = [r for r in rows if _fittable(r)]
    cal: Dict[str, Any] = {
        "method": ("least squares: tokens ~ sum(non_cjk_chars_c / cpt_c) + cjk_chars / cpt_cjk, "
                   "ridge prior %.1f cpt / %.2f cjk-cpt" % (prior, PRIOR_CJK_CPT)),
        "n": len(sample),
        "categories": {},
        "deviation": {},
        "ok": False,
    }
    if len(sample) < 4:
        cal["note"] = "需要至少 4 条带真实 usage 的请求才能拟合；当前不足，全部按 estimated 显示"
        cal["categories"] = {c: {"chars_per_token": prior, "identifiable": False} for c in CATEGORIES}
        cal["cjk_chars_per_token"] = PRIOR_CJK_CPT
        return cal

    x = _design(sample)
    t = [float(r["truth"]) for r in sample]
    beta = _fit_once(x, t, prior, iters)
    est = [sum(x[i][m] * beta[m] for m in range(len(beta))) for i in range(len(x))]
    devs = [abs(est[i] - t[i]) / t[i] for i in range(len(t)) if t[i]]
    devs_sorted = sorted(devs)
    median = devs_sorted[len(devs_sorted) // 2] if devs_sorted else 0.0
    p90 = devs_sorted[min(len(devs_sorted) - 1, int(0.9 * (len(devs_sorted) - 1)))] if devs_sorted else 0.0
    worst = max(devs) if devs else 0.0

    for j, cat in enumerate(CATEGORIES):
        cpt = 1.0 / beta[j] if beta[j] > 0 else None
        cal["categories"][cat] = {
            "chars_per_token": round(cpt, 3) if cpt else None,
            "tokens_per_kchar": round(beta[j] * 1000, 2),
            "identifiable": _column_identified([x[i][j] for i in range(len(x))]),
        }
    cal["cjk_chars_per_token"] = round(1.0 / beta[-1], 3) if beta[-1] > 0 else None
    cal["cjk_identifiable"] = _column_identified([x[i][-1] for i in range(len(x))])
    cal["_beta"] = [round(b, 8) for b in beta]          # stripped before the document ships
    cal["deviation"] = {"median": round(median, 4), "p90": round(p90, 4), "max": round(worst, 4),
                        "n": len(devs)}
    loo = sorted(_loo_deviation(x, t, prior, iters))
    if loo:
        cal["cv"] = {
            "method": "leave-one-out",
            "n": len(loo),
            "median": round(loo[len(loo) // 2], 4),
            "p90": round(loo[min(len(loo) - 1, int(0.9 * (len(loo) - 1)))], 4),
            "max": round(loo[-1], 4),
        }
    cv_worst = (cal.get("cv") or {}).get("max") or 0.0
    worst_any = max(worst, cv_worst)
    cal["ok"] = worst_any <= 0.05
    if not cal["ok"]:
        cal["note"] = ("偏差 %.1f%%（留一交叉验证 %.1f%%）超过 5%%，估算值在 UI 上标 estimated"
                       % (worst * 100, cv_worst * 100))
    prior_only = [c for c in CATEGORIES if not cal["categories"][c]["identifiable"]]
    if prior_only:
        cal["prior_only"] = prior_only
        cal["note"] = ((cal.get("note") or "") +
                       "；这些类别在样本内没有变化，系数主要来自先验：%s" % ", ".join(prior_only))
    return cal


def _params_from_calibration(cal: Dict[str, Any]) -> List[float]:
    params = []
    for cat in CATEGORIES:
        cpt = (cal.get("categories", {}).get(cat) or {}).get("chars_per_token") or PRIOR_CPT
        params.append(1.0 / float(cpt))
    cpt_cjk = cal.get("cjk_chars_per_token") or PRIOR_CJK_CPT
    params.append(1.0 / float(cpt_cjk))
    return params


def _segment_tokens(chars: Dict[str, int], cjk: Dict[str, int], params: List[float]) -> Dict[str, int]:
    out = {}
    for i, cat in enumerate(CATEGORIES):
        total = int(chars.get(cat) or 0)
        cj = int(cjk.get(cat) or 0)
        out[cat] = int(round((total - cj) * params[i] + cj * params[-1]))
    return out


# --------------------------------------------------------------------------- raw sidecar
def _raw_segments(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Split one stored request into the text the drawer shows per category."""
    sys_text = ((raw.get("system_prompt") or {}).get("text") or "")
    out: Dict[str, Dict[str, Any]] = {}

    def put(cat: str, text: str) -> None:
        prev = out.get(cat, {"text": "", "chars": 0, "truncated": False})
        merged = (prev["text"] + ("\n\n" if prev["text"] else "") + text) if text else prev["text"]
        out[cat] = {"text": merged, "chars": len(merged),
                    "truncated": bool(prev["truncated"]) or len(merged) > RAW_SEG_CAP}

    sys_parts: Dict[str, List[str]] = {"sys": [], "injected": []}
    for sec in segments.system_sections(sys_text):
        sys_parts[sec["k"]].append(sys_text[sec["start"]:sec["end"]])
    put("sys", "\n".join(sys_parts["sys"]).strip())
    put("injected", "\n".join(sys_parts["injected"]).strip())

    if raw.get("tools"):
        put("tools", "\n".join(t.get("text") or "" for t in raw["tools"]))
    by_role: Dict[str, List[str]] = {"user": [], "assistant": [], "tool": []}
    for msg in raw.get("messages") or []:
        role = msg.get("role")
        if role in by_role:
            label = f"[{role}{(' ' + msg['tool_name']) if msg.get('tool_name') else ''}]"
            by_role[role].append(label + "\n" + (msg.get("text") or ""))
    for role, chunks in by_role.items():
        if chunks:
            put(role, "\n\n".join(chunks))
    for cat, item in out.items():
        if len(item["text"]) > RAW_SEG_CAP:
            item["text"] = item["text"][:RAW_SEG_CAP]
            item["truncated"] = True
    return out


def _read_raw(row: Dict[str, Any], raw_keep: int) -> Optional[Dict[str, Any]]:
    ref = row.get("raw_ref")
    base = row.get("file")
    if not ref or not base:
        return None
    path = Path(base).parent / ref
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _raw_segments(raw)


# --------------------------------------------------------------------------- build
def _raw_scope(raw_paths: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Roots allowed to contribute raw text (None = every --context source)."""
    if not raw_paths:
        return None
    roots: List[str] = []
    for raw in raw_paths:
        p = Path(raw).expanduser()
        roots.append(str(p.resolve()))
    return roots


def _in_raw_scope(row: Dict[str, Any], roots: Optional[List[str]]) -> bool:
    if roots is None:
        return True
    f = str(row.get("file") or "")
    try:
        resolved = str(Path(f).resolve())
    except OSError:
        resolved = f
    return any(resolved.startswith(r) for r in roots)


def build(
    paths: Optional[Iterable[str]],
    *,
    raw_paths: Optional[Iterable[str]] = None,
    raw_keep: int = RAW_KEEP_DEFAULT,
    apply_redaction: bool = False,
    env_values: Iterable[str] = (),
    home_names: Iterable[str] = (),
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (context section for trace.json, raw sidecar document)."""
    events = read_events(paths)
    rows = pair(events)
    cal = fit(rows)
    params = _params_from_calibration(cal)

    # Raw text is kept for the most recent N requests only — everything older is
    # represented by sizes + summary, which is the whole point of the split.
    roots = _raw_scope(raw_paths)
    pool = [r for r in rows if _in_raw_scope(r, roots)]
    with_truth = [r for r in pool if r.get("truth")]
    newest = sorted(with_truth or pool, key=lambda r: r.get("ts") or 0, reverse=True)[:raw_keep]
    keep_keys = {tuple(r["key"]) for r in newest}

    raw_doc: Dict[str, Any] = {"schema": "htrace/context-raw/1", "keep": raw_keep, "requests": {}}
    budget = RAW_TOTAL_CAP
    requests_out: List[Dict[str, Any]] = []
    for row in rows:
        toks = _segment_tokens(row["chars"], row.get("cjk") or {}, params)
        est = sum(toks.values())
        truth = row.get("truth")
        key_str = "|".join(row["key"])
        has_raw = False
        if tuple(row["key"]) in keep_keys:
            segs = _read_raw(row, raw_keep)
            if segs and budget > 0:
                trimmed: Dict[str, Any] = {}
                for cat, item in segs.items():
                    text = item["text"]
                    if len(text) > budget:
                        text = text[:budget]
                        budget = 0
                    else:
                        budget -= len(text)
                    trimmed[cat] = {"text": text, "chars": item["chars"], "truncated": item["truncated"]}
                raw_doc["requests"][key_str] = {"ts": row.get("ts"), "segments": trimmed,
                                                "model": row.get("model") or ""}
                has_raw = True
        requests_out.append({
            "k": key_str,
            "session": row.get("session_id") or "",
            "turn": row.get("turn_id") or "",
            "n": row.get("api_call_count"),
            "ts": row.get("ts"),
            "model": row.get("model") or "",
            "chars": row["chars"],
            "cjk": row.get("cjk") or {},
            "tokens": toks,
            "est": est,
            "truth": truth,
            "dev": (round(abs(est - truth) / truth, 4) if truth else None),
            "sections": [{"heading": s.get("heading"), "k": s.get("k"), "chars": s.get("chars")}
                         for s in row.get("sections") or []][:40],
            "counts": row.get("counts") or {},
            "tools_present": row.get("tools_present"),
            "truncated": row.get("truncated"),
            "has_raw": has_raw,
        })

    cal.pop("_beta", None)
    doc = {
        "schema": "htrace/context/1",
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "categories": [{"key": c, "label": LABELS[c]} for c in CATEGORIES],
        "calibration": cal,
        "totals": {
            "requests": len(requests_out),
            "with_truth": sum(1 for r in requests_out if r["truth"]),
            "raw_kept": len(raw_doc["requests"]),
            "chars": sum(sum(r["chars"].values()) for r in requests_out),
            "est_tokens": sum(r["est"] or 0 for r in requests_out),
            "true_tokens": sum(r["truth"] or 0 for r in requests_out),
            "sessions": len({r["session"] for r in requests_out if r["session"]}),
        },
        "files": [str(p) for p in _expand(paths)],
        "raw_scope": roots if roots is not None else "all",
        "requests": requests_out,
    }
    if apply_redaction:
        doc, stats = redact.redact_document(doc, env_values, home_names)
        doc.setdefault("source", {})["redaction_stats"] = stats
        raw_doc, stats2 = redact.redact_document(raw_doc, env_values, home_names)
        merged = dict(stats)
        for k, v in (stats2 or {}).items():
            merged[k] = merged.get(k, 0) + v
        doc["source"]["redaction_stats"] = merged
    return doc, raw_doc


def write_raw(raw_doc: Dict[str, Any], out_path: Path, *, as_demo_js: bool = True) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(raw_doc, ensure_ascii=False, separators=(",", ":"))
    if as_demo_js:
        out_path.write_text("window.HTRACE_CTX_RAW = " + payload + ";\n", encoding="utf-8")
    else:
        out_path.write_text(payload, encoding="utf-8")
    return out_path
