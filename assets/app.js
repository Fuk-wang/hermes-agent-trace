/* htrace viewer — vanilla JS, no build step, no network. */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };

  const state = {
    doc: null,
    mode: 'demo',
    view: 'overview',
    sessionId: null,
    cursor: 0,
    filters: { tool: '', status: '', session: '', minMs: 0, q: '' },
    rows: [],
  };

  /* ---------------- formatting ---------------- */
  const fmtInt = (n) => (n === null || n === undefined || isNaN(n)) ? '—' : Number(n).toLocaleString('en-US');
  const fmtCost = (n) => (n === null || n === undefined) ? '—' : '$' + Number(n).toFixed(4);
  const fmtMs = (ms) => {
    if (ms === null || ms === undefined) return '—';
    if (ms < 1000) return ms + 'ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
    const m = Math.floor(ms / 60000);
    const s = Math.round((ms % 60000) / 1000);
    return m + 'm' + String(s).padStart(2, '0') + 's';
  };
  const fmtPct = (x) => (x === null || x === undefined || isNaN(x)) ? '—' : (x * 100).toFixed(1) + '%';
  const t = (iso) => {
    if (!iso) return '—';
    return iso.replace('T', ' ').slice(5, 19);
  };
  const tLocal = (epochSec) => {
    if (epochSec === null || epochSec === undefined) return '—';
    const dt = new Date(epochSec * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}`;
  };
  const day = (iso) => (iso ? iso.slice(0, 10) : '—');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clip = (s, n) => {
    s = String(s ?? '');
    return s.length > n ? s.slice(0, n) + '…' : s;
  };
  const num = (v) => (typeof v === 'number' ? v : 0);

  /* ---------------- data access ---------------- */
  const sessions = () => (state.doc ? state.doc.sessions : []);
  const sessionById = (id) => sessions().find((s) => s.id === id) || null;
  const allCalls = () => {
    const out = [];
    for (const s of sessions()) {
      for (const c of s.calls || []) out.push({ call: c, session: s });
    }
    return out;
  };
  const toolInfo = (name) => (state.doc && state.doc.tools ? state.doc.tools[name] : null);
  const argsText = (call) => {
    if (call.args !== null && call.args !== undefined) {
      try { return JSON.stringify(call.args, null, 2); } catch (e) { return String(call.args); }
    }
    return call.args_raw || '';
  };
  const argsOneLine = (call) => {
    const raw = call.args_raw || (call.args ? JSON.stringify(call.args) : '');
    return raw.replace(/\s+/g, ' ');
  };

  /* ---------------- chrome ---------------- */
  const VIEWS = {
    overview: ['总览', '所有会话的工具调用活动、错误与成本'],
    sessions: ['会话', '按会话看工具使用构成与耗时'],
    calls: ['工具调用', '每一次调用的参数、结果、耗时与源码位置'],
    timeline: ['时间线', '单个会话里调用发生在什么时候、间隔多久'],
    cost: ['成本', 'token、缓存命中与估算花费'],
    subagents: ['子代理', '委派出去的父子会话链路'],
    source: ['源码索引', '工具名 → Hermes 实现文件与函数'],
    raw: ['原始数据', 'trace 文档本身（可下载）'],
  };

  function setView(view, opts) {
    opts = opts || {};
    if (!VIEWS[view]) view = 'overview';
    state.view = view;
    document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.view === view));
    document.querySelectorAll('.view').forEach((s) => { s.hidden = s.id !== 'view-' + view; });
    const [title, sub] = VIEWS[view];
    $('#view-title').textContent = title;
    $('#view-sub').textContent = sub;
    state.cursor = 0;
    if (opts.push !== false) {
      const hash = view === 'sessions' && state.sessionId ? '#sessions/' + state.sessionId : '#' + view;
      if (location.hash !== hash) {
        try { history.replaceState(null, '', hash); } catch (e) { /* file:// in some browsers */ }
      }
    }
    render();
  }

  function setMode(mode) {
    state.mode = mode;
    const b = $('#mode-badge');
    b.textContent = mode === 'demo' ? 'DEMO · 已脱敏' : 'LOCAL · 你的数据';
    b.classList.toggle('local', mode !== 'demo');
  }

  function toast(msg) {
    const n = $('#toast');
    n.textContent = msg;
    n.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { n.hidden = true; }, 1800);
  }

  /* ---------------- render dispatch ---------------- */
  function render() {
    if (!state.doc) { renderEmpty(); return; }
    const d = state.doc;
    $('#data-badge').textContent = `${d.sessions.length} 会话 · ${fmtInt(d.totals.calls)} 调用 · ${d.schema}`;
    switch (state.view) {
      case 'overview': renderOverview(); break;
      case 'sessions': state.sessionId ? renderSessionDetail(state.sessionId) : renderSessions(); break;
      case 'calls': renderCalls(); break;
      case 'timeline': renderTimeline(); break;
      case 'cost': renderCost(); break;
      case 'subagents': renderSubagents(); break;
      case 'source': renderSource(); break;
      case 'raw': renderRaw(); break;
    }
  }

  function renderEmpty() {
    const box = $('#view-' + state.view);
    box.innerHTML = `
      <div class="empty">
        <p>还没有 trace 数据。</p>
        <p class="tiny">点击左下「打开 trace.json」，或在命令行运行：<br>
        <code>python -m htrace build --out trace.json --source &lt;hermes-agent 源码目录&gt;</code></p>
      </div>`;
  }

  /* ---------------- overview ---------------- */
  function renderOverview() {
    const d = state.doc;
    const tot = d.totals;
    const cacheRatio = tot.input_tokens ? tot.cache_read_tokens / (tot.input_tokens + tot.cache_read_tokens) : null;
    const topTools = Object.entries(d.by_tool || {}).sort((a, b) => b[1].calls - a[1].calls).slice(0, 9);
    const maxCalls = topTools.length ? topTools[0][1].calls : 1;
    const recent = d.sessions.slice(0, 8);
    const src = d.source || {};

    const box = $('#view-overview');
    box.innerHTML = `
      <div class="grid cards">
        <div class="card accent"><div class="label">tool calls</div><div class="value">${fmtInt(tot.calls)}</div><div class="hint">${d.sessions.length} 个会话</div></div>
        <div class="card ${tot.errors ? 'warn' : ''}"><div class="label">errors</div><div class="value">${fmtInt(tot.errors)}</div><div class="hint">${fmtInt(tot.pending)} 条未配对</div></div>
        <div class="card"><div class="label">tool wall time</div><div class="value">${fmtMs(tot.total_ms)}</div><div class="hint">模型思考以外的工具耗时</div></div>
        <div class="card"><div class="label">cost</div><div class="value">${fmtCost(tot.cost_usd)}</div><div class="hint">cache 命中 ${fmtInt(tot.cache_read_tokens)} tok</div></div>
      </div>

      <div class="grid" style="grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);margin-top:16px">
        <div class="panel">
          <div class="panel-head"><h3>调用分布</h3><span class="meta">by tool</span></div>
          <div class="panel-body">
            <div class="bars">
              ${topTools.map(([name, st]) => `
                <div class="bar-row" title="${esc(name)}: ${st.calls} calls, ${fmtMs(st.ms)}">
                  <span class="name">${esc(name)}</span>
                  <span class="bar-track"><span class="bar-fill ${st.errors ? 'err' : ''}" style="width:${(st.calls / maxCalls * 100).toFixed(1)}%"></span></span>
                  <span class="num">${st.calls} · ${fmtMs(st.ms)}</span>
                </div>`).join('') || '<div class="empty">没有工具调用</div>'}
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head"><h3>这次 trace 是怎么来的</h3><span class="meta">${esc(src.mode || '')}</span></div>
          <div class="panel-body">
            <dl class="kv">
              <dt>generated</dt><dd>${esc(d.generated_at || '—')}</dd>
              <dt>homes</dt><dd>${(src.homes || []).map((h) => `${esc(h.profile)} → <span class="mono">${esc(h.state_db)}</span>`).join('<br>') || '—'}</dd>
              <dt>hook 合并</dt><dd>${src.hook_calls_merged ? fmtInt(src.hook_calls_merged) + ' 条实时事件' : '未接入（只用 state.db）'}</dd>
              <dt>源码索引</dt><dd>${Object.keys(d.tools || {}).length} 个工具${src.source_index_root ? ' · <span class="mono">' + esc(src.source_index_root) + '</span>' : ''}</dd>
              <dt>脱敏</dt><dd>${src.redacted ? '已脱敏 ' + JSON.stringify(src.redaction_stats || {}) : '未脱敏'}</dd>
            </dl>
            <div class="note" style="margin-top:12px">
              <b>工具调用 = 可核验事实。</b> 每条记录都带参数、结果、耗时与源码位置；模型的说法与记录不一致时，以记录为准。
            </div>
          </div>
        </div>
      </div>

      <div class="section-title">最近会话</div>
      <div class="panel"><div class="tablewrap"><table>
        <thead><tr><th>会话</th><th>来源</th><th class="num">调用</th><th class="num">token</th><th class="num">cost</th><th>开始</th></tr></thead>
        <tbody>
          ${recent.map((s) => `
            <tr data-session="${esc(s.id)}">
              <td><span class="tool-name">${esc(clip(s.title || s.id, 40))}</span><div class="preview">${esc(s.id)}</div></td>
              <td class="mono dim">${esc(s.source || '')}</td>
              <td class="num">${fmtInt(s.stats.calls)}</td>
              <td class="num">${fmtInt(num(s.tokens.input) + num(s.tokens.output))}</td>
              <td class="num">${fmtCost(s.cost_usd)}</td>
              <td class="mono dim">${esc(t(s.started_at))}</td>
            </tr>`).join('') || '<tr><td colspan="6" class="empty">没有会话</td></tr>'}
        </tbody>
      </table></div></div>
    `;
    box.querySelectorAll('tr[data-session]').forEach((tr) => {
      tr.onclick = () => { state.sessionId = tr.dataset.session; setView('sessions'); };
    });
  }

  /* ---------------- sessions ---------------- */
  function renderSessions() {
    const box = $('#view-sessions');
    const rows = sessions();
    box.innerHTML = `
      <div class="filters">
        <span class="count">${rows.length} 个会话</span>
        <span class="spacer"></span>
        <span class="count">按开始时间倒序</span>
      </div>
      <div class="panel"><div class="tablewrap"><table>
        <thead><tr>
          <th>会话</th><th>模型</th><th class="num">调用</th><th class="num">错误</th>
          <th class="num">工具耗时</th><th class="num">token in/out</th><th class="num">cost</th><th>开始</th>
        </tr></thead>
        <tbody>
          ${rows.map((s) => `
            <tr data-session="${esc(s.id)}">
              <td>
                <span class="tool-name">${esc(clip(s.title || '(untitled)', 46))}</span>
                <div class="preview">${esc(s.id)} · ${esc(s.cwd || '')}</div>
              </td>
              <td class="mono dim">${esc(clip(s.model || '', 22))}</td>
              <td class="num">${fmtInt(s.stats.calls)}</td>
              <td class="num">${s.stats.errors ? `<span class="pill error">${s.stats.errors}</span>` : '<span class="dim">0</span>'}</td>
              <td class="num">${fmtMs(s.stats.total_ms)}</td>
              <td class="num">${fmtInt(s.tokens.input)} / ${fmtInt(s.tokens.output)}</td>
              <td class="num">${fmtCost(s.cost_usd)}</td>
              <td class="mono dim">${esc(t(s.started_at))}</td>
            </tr>`).join('') || '<tr><td colspan="8" class="empty">没有会话</td></tr>'}
        </tbody>
      </table></div></div>
    `;
    state.rows = Array.from(box.querySelectorAll('tr[data-session]'));
    state.rows.forEach((tr, i) => {
      tr.onclick = () => { state.sessionId = tr.dataset.session; setView('sessions'); };
      tr.dataset.idx = i;
    });
  }

  function renderSessionDetail(id) {
    const s = sessionById(id);
    const box = $('#view-sessions');
    if (!s) { state.sessionId = null; renderSessions(); return; }
    const byTool = Object.entries(s.stats.by_tool).sort((a, b) => b[1].calls - a[1].calls);
    const maxCalls = byTool.length ? byTool[0][1].calls : 1;
    const calls = s.calls.slice().reverse();

    box.innerHTML = `
      <div class="hstack" style="margin-bottom:14px">
        <button class="mini-btn" id="back-sessions">← 全部会话</button>
        <span class="mono dim" style="font-size:11.5px">${esc(s.id)}</span>
      </div>
      <div class="grid cards" style="margin-bottom:16px">
        <div class="card accent"><div class="label">tool calls</div><div class="value">${fmtInt(s.stats.calls)}</div><div class="hint">${s.stats.errors} 错误 · ${s.stats.pending} 未配对</div></div>
        <div class="card"><div class="label">工具耗时</div><div class="value">${fmtMs(s.stats.total_ms)}</div><div class="hint">p50 ${fmtMs(s.stats.p50_ms)} · max ${fmtMs(s.stats.max_ms)}</div></div>
        <div class="card"><div class="label">tokens</div><div class="value">${fmtInt(s.tokens.input)}</div><div class="hint">out ${fmtInt(s.tokens.output)} · cache ${fmtInt(s.tokens.cache_read)}</div></div>
        <div class="card"><div class="label">cost</div><div class="value">${fmtCost(s.cost_usd)}</div><div class="hint">${esc(s.model || '')}</div></div>
      </div>

      <div class="grid" style="grid-template-columns:minmax(0,1fr) 300px">
        <div class="panel">
          <div class="panel-head"><h3>调用记录</h3><span class="meta">${calls.length} 条 · 倒序</span></div>
          <div class="tablewrap" style="max-height:520px"><table class="fixed">
            <colgroup><col style="width:40px"><col style="width:84px"><col style="width:118px"><col style="width:68px"><col style="width:70px"><col></colgroup>
            <thead><tr><th>#</th><th>时间</th><th>工具</th><th class="num">耗时</th><th>状态</th><th>参数</th></tr></thead>
            <tbody>
              ${calls.map((c) => `
                <tr data-call="${esc(c.call_id)}" data-seq="${c.seq}">
                  <td class="mono dim">${c.seq}</td>
                  <td class="mono dim nowrap">${esc(t(c.started_at))}</td>
                  <td class="nowrap"><span class="tool-name">${esc(c.name)}</span></td>
                  <td class="num">${fmtMs(c.duration_ms)}</td>
                  <td><span class="pill ${c.status}">${c.status}</span></td>
                  <td><div class="preview">${esc(clip(argsOneLine(c), 80))}</div></td>
                </tr>`).join('') || '<tr><td colspan="6" class="empty">这个会话没有工具调用</td></tr>'}
            </tbody>
          </table></div>
        </div>

        <div>
          <div class="panel" style="margin-bottom:14px">
            <div class="panel-head"><h3>工具构成</h3><span class="meta">${byTool.length}</span></div>
            <div class="panel-body"><div class="bars">
              ${byTool.map(([name, st]) => `
                <div class="bar-row" style="grid-template-columns:86px 1fr 40px">
                  <span class="name">${esc(name)}</span>
                  <span class="bar-track"><span class="bar-fill ${st.errors ? 'err' : ''}" style="width:${(st.calls / maxCalls * 100).toFixed(0)}%"></span></span>
                  <span class="num">${st.calls}</span>
                </div>`).join('') || '<div class="empty">—</div>'}
            </div></div>
          </div>
          <div class="panel">
            <div class="panel-head"><h3>上下文</h3></div>
            <div class="panel-body"><dl class="kv">
              <dt>cwd</dt><dd class="mono" style="font-size:11px;word-break:break-all">${esc(s.cwd || '—')}</dd>
              <dt>branch</dt><dd class="mono" style="font-size:11px">${esc(s.git_branch || '—')}</dd>
              <dt>source</dt><dd>${esc(s.source || '—')}</dd>
              <dt>开始</dt><dd class="mono" style="font-size:11px">${esc(s.started_at || '—')}</dd>
              <dt>结束</dt><dd class="mono" style="font-size:11px">${esc(s.ended_at || '—')}</dd>
            </dl></div>
          </div>
        </div>
      </div>
    `;
    $('#back-sessions').onclick = () => { state.sessionId = null; setView('sessions'); };
    const rowEls = Array.from(box.querySelectorAll('tr[data-call]'));
    state.rows = rowEls;
    rowEls.forEach((tr, i) => {
      tr.dataset.idx = i;
      tr.onclick = () => {
        const call = s.calls.find((c) => c.call_id === tr.dataset.call && String(c.seq) === tr.dataset.seq);
        if (call) openCall(call, s);
      };
    });
  }

  /* ---------------- calls ---------------- */
  function callFiltered() {
    const f = state.filters;
    const q = f.q.trim().toLowerCase();
    let out = allCalls();
    if (f.tool) out = out.filter((x) => x.call.name === f.tool);
    if (f.status) out = out.filter((x) => x.call.status === f.status);
    if (f.session) out = out.filter((x) => x.session.id === f.session);
    if (f.minMs) out = out.filter((x) => (x.call.duration_ms || 0) >= f.minMs);
    if (q) {
      out = out.filter((x) => {
        const hay = [x.call.name, x.call.args_raw, x.call.result, x.session.title, x.session.id]
          .join(' ').toLowerCase();
        return hay.includes(q);
      });
    }
    out.sort((a, b) => (b.call.started_ts || 0) - (a.call.started_ts || 0));
    return out;
  }

  function renderCalls() {
    const box = $('#view-calls');
    const tools = Object.entries(state.doc.by_tool || {}).sort((a, b) => b[1].calls - a[1].calls);
    const rows = callFiltered();
    const limited = rows.slice(0, 600);

    box.innerHTML = `
      <div class="filters">
        <select id="f-tool">
          <option value="">全部工具</option>
          ${tools.map(([n, st]) => `<option value="${esc(n)}" ${state.filters.tool === n ? 'selected' : ''}>${esc(n)} (${st.calls})</option>`).join('')}
        </select>
        <select id="f-status">
          <option value="">全部状态</option>
          ${['ok', 'error', 'pending'].map((s) => `<option value="${s}" ${state.filters.status === s ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
        <select id="f-session">
          <option value="">全部会话</option>
          ${sessions().map((s) => `<option value="${esc(s.id)}" ${state.filters.session === s.id ? 'selected' : ''}>${esc(clip(s.title || s.id, 34))}</option>`).join('')}
        </select>
        <input id="f-min" type="number" min="0" step="100" placeholder="≥ 耗时 ms" value="${state.filters.minMs || ''}">
        <span class="spacer"></span>
        <span class="count">${rows.length} 条${rows.length > limited.length ? ' · 显示前 ' + limited.length : ''}</span>
        <button class="mini-btn" id="f-reset">重置</button>
      </div>
      <div class="panel"><div class="tablewrap" style="max-height:calc(100vh - 210px)"><table class="fixed">
        <colgroup><col style="width:86px"><col style="width:128px"><col style="width:170px"><col style="width:74px"><col style="width:78px"><col><col></colgroup>
        <thead><tr><th>时间</th><th>工具</th><th>会话</th><th class="num">耗时</th><th>状态</th><th>参数</th><th>结果</th></tr></thead>
        <tbody>
          ${limited.map(({ call, session }) => `
            <tr data-call="${esc(call.call_id)}" data-session="${esc(session.id)}" data-seq="${call.seq}">
              <td class="mono dim nowrap">${esc(t(call.started_at))}</td>
              <td class="nowrap"><span class="tool-name">${esc(call.name)}</span>${call.source === 'hook' ? ' <span class="pill hook">live</span>' : ''}</td>
              <td class="mono dim nowrap">${esc(session.title || session.id)}</td>
              <td class="num">${fmtMs(call.duration_ms)}</td>
              <td><span class="pill ${call.status}">${call.status}</span></td>
              <td><div class="preview">${esc(clip(argsOneLine(call), 64))}</div></td>
              <td><div class="preview">${esc(clip((call.result || '').replace(/\s+/g, ' '), 48))}</div></td>
            </tr>`).join('') || '<tr><td colspan="7" class="empty">没有匹配的调用</td></tr>'}
        </tbody>
      </table></div></div>
    `;

    $('#f-tool').onchange = (e) => { state.filters.tool = e.target.value; renderCalls(); };
    $('#f-status').onchange = (e) => { state.filters.status = e.target.value; renderCalls(); };
    $('#f-session').onchange = (e) => { state.filters.session = e.target.value; renderCalls(); };
    $('#f-min').onchange = (e) => { state.filters.minMs = Number(e.target.value) || 0; renderCalls(); };
    $('#f-reset').onclick = () => { state.filters = { tool: '', status: '', session: '', minMs: 0, q: '' }; $('#search').value = ''; renderCalls(); };

    const rowEls = Array.from(box.querySelectorAll('tr[data-call]'));
    state.rows = rowEls;
    rowEls.forEach((tr, i) => {
      tr.dataset.idx = i;
      tr.onclick = () => {
        const s = sessionById(tr.dataset.session);
        const call = s && s.calls.find((c) => c.call_id === tr.dataset.call && String(c.seq) === tr.dataset.seq);
        if (call) openCall(call, s);
      };
    });
    updateSearchCount();
  }

  /* ---------------- timeline ---------------- */
  function renderTimeline() {
    const box = $('#view-timeline');
    if (!state.sessionId || !sessionById(state.sessionId)) {
      state.sessionId = sessions().length ? sessions()[0].id : null;
    }
    const s = sessionById(state.sessionId);
    if (!s) { box.innerHTML = '<div class="empty">没有会话</div>'; return; }
    const calls = s.calls.filter((c) => c.started_ts);
    const t0 = calls.length ? Math.min(...calls.map((c) => c.started_ts)) : 0;
    const t1 = calls.length ? Math.max(...calls.map((c) => (c.ended_ts || c.started_ts))) : 1;
    const span = Math.max(1, t1 - t0);
    const maxDur = Math.max(1, ...calls.map((c) => c.duration_ms || 1));
    const gaps = [];
    const sorted = calls.slice().sort((a, b) => a.started_ts - b.started_ts);
    for (let i = 1; i < sorted.length; i++) {
      const g = sorted[i].started_ts - (sorted[i - 1].ended_ts || sorted[i - 1].started_ts);
      if (g > 0) gaps.push({ at: sorted[i - 1].ended_ts, ms: g * 1000 });
    }
    gaps.sort((a, b) => b.ms - a.ms);
    const longest = sorted.slice().sort((a, b) => (b.duration_ms || 0) - (a.duration_ms || 0)).slice(0, 6);

    const BUCKETS = 60;
    const density = new Array(BUCKETS).fill(0);
    for (const c of sorted) {
      const idx = Math.min(BUCKETS - 1, Math.max(0, Math.floor(((c.started_ts - t0) / span) * BUCKETS)));
      density[idx] += 1;
    }
    const maxBucket = Math.max(1, ...density);

    box.innerHTML = `
      <div class="filters">
        <select id="tl-session">
          ${sessions().map((x) => `<option value="${esc(x.id)}" ${x.id === s.id ? 'selected' : ''}>${esc(clip(x.title || x.id, 46))}</option>`).join('')}
        </select>
        <span class="spacer"></span>
        <span class="count">跨度 ${fmtMs(span * 1000)} · ${calls.length} 次调用</span>
      </div>

      <div class="tl-head"><span>${esc(tLocal(t0))}</span><span>${esc(tLocal(t1))}</span></div>
      <div class="tl" id="tl-canvas">
        ${sorted.map((c) => {
          const left = ((c.started_ts - t0) / span) * 100;
          const h = 6 + Math.round(Math.sqrt((c.duration_ms || 0) / maxDur) * 94);
          return `<div class="tl-bar ${c.status === 'error' ? 'err' : ''}" data-call="${esc(c.call_id)}" data-seq="${c.seq}"
            title="${esc(c.name)} · ${fmtMs(c.duration_ms)} · ${esc(tLocal(c.started_ts))}"
            style="left:${left.toFixed(3)}%;width:3px;height:${h}px"></div>`;
        }).join('')}
      </div>
      <div class="tl-ticks">
        ${[0, 0.25, 0.5, 0.75, 1].map((f) => `<span>${esc(tLocal(t0 + span * f).slice(6))}</span>`).join('')}
      </div>
      <div class="tl-legend"><span><i style="background:var(--cyan)"></i>正常</span><span><i style="background:var(--err)"></i>报错</span><span>柱高 = 耗时（平方根缩放）</span></div>

      <div class="section-title" style="margin-top:18px">调用密度 <span style="text-transform:none;letter-spacing:0">（${BUCKETS} 等分，柱高 = 该时段调用次数）</span></div>
      <div class="tl density">
        ${density.map((n, i) => `<div class="tl-bar dens" data-i="${i}" title="${esc(tLocal(t0 + span * i / BUCKETS).slice(6))} · ${n} 次"
          style="left:${(i / BUCKETS * 100).toFixed(3)}%;width:${(100 / BUCKETS - 0.25).toFixed(3)}%;height:${n ? 8 + Math.round(n / maxBucket * 92) : 2}px;opacity:${n ? 1 : .25}"></div>`).join('')}
      </div>

      <div class="grid" style="grid-template-columns:minmax(0,1fr) minmax(0,1fr);margin-top:18px">
        <div class="panel">
          <div class="panel-head"><h3>最慢的调用</h3><span class="meta">top 6</span></div>
          <div class="tablewrap"><table>
            <thead><tr><th>工具</th><th>时间</th><th class="num">耗时</th></tr></thead>
            <tbody>${longest.map((c) => `<tr data-call="${esc(c.call_id)}" data-seq="${c.seq}"><td><span class="tool-name">${esc(c.name)}</span></td><td class="mono dim">${esc(tLocal(c.started_ts))}</td><td class="num">${fmtMs(c.duration_ms)}</td></tr>`).join('') || '<tr><td colspan="3" class="empty">—</td></tr>'}</tbody>
          </table></div>
        </div>
        <div class="panel">
          <div class="panel-head"><h3>最长空窗</h3><span class="meta">tool idle</span></div>
          <div class="tablewrap"><table>
            <thead><tr><th>开始空窗</th><th class="num">持续</th></tr></thead>
            <tbody>${gaps.slice(0, 6).map((g) => `<tr><td class="mono dim">${esc(tLocal(g.at))}</td><td class="num">${fmtMs(g.ms)}</td></tr>`).join('') || '<tr><td colspan="2" class="empty">没有明显空窗</td></tr>'}</tbody>
          </table></div>
        </div>
      </div>
    `;

    $('#tl-session').onchange = (e) => { state.sessionId = e.target.value; renderTimeline(); };
    box.querySelectorAll('[data-call]').forEach((node) => {
      node.onclick = () => {
        const call = s.calls.find((c) => c.call_id === node.dataset.call && String(c.seq) === node.dataset.seq);
        if (call) openCall(call, s);
      };
    });
  }

  /* ---------------- cost ---------------- */
  function renderCost() {
    const box = $('#view-cost');
    const rows = sessions();
    const tot = state.doc.totals;
    const withApi = rows.some((s) => (s.api_calls || []).length);
    box.innerHTML = `
      <div class="grid cards">
        <div class="card accent"><div class="label">input tokens</div><div class="value">${fmtInt(tot.input_tokens)}</div><div class="hint">整个 trace 的输入</div></div>
        <div class="card"><div class="label">cache read</div><div class="value">${fmtInt(tot.cache_read_tokens)}</div><div class="hint">${fmtPct(tot.cache_read_tokens / Math.max(1, tot.input_tokens + tot.cache_read_tokens))} 命中占比</div></div>
        <div class="card"><div class="label">output tokens</div><div class="value">${fmtInt(tot.output_tokens)}</div><div class="hint">含推理 token 不单独计</div></div>
        <div class="card"><div class="label">estimated cost</div><div class="value">${fmtCost(tot.cost_usd)}</div><div class="hint">来自 state.db 记录</div></div>
      </div>

      <div class="section-title">按会话</div>
      <div class="panel"><div class="tablewrap"><table>
        <thead><tr><th>会话</th><th>模型</th><th class="num">in</th><th class="num">out</th><th class="num">cache r</th><th class="num">cache w</th><th class="num">cost</th><th class="num">$ / 1k calls</th></tr></thead>
        <tbody>
          ${rows.map((s) => {
            const calls = Math.max(1, s.stats.calls);
            return `<tr data-session="${esc(s.id)}">
              <td><span class="tool-name">${esc(clip(s.title || s.id, 38))}</span></td>
              <td class="mono dim">${esc(clip(s.model || '', 20))}</td>
              <td class="num">${fmtInt(s.tokens.input)}</td>
              <td class="num">${fmtInt(s.tokens.output)}</td>
              <td class="num">${fmtInt(s.tokens.cache_read)}</td>
              <td class="num">${fmtInt(s.tokens.cache_write)}</td>
              <td class="num">${fmtCost(s.cost_usd)}</td>
              <td class="num">${s.cost_usd ? '$' + (s.cost_usd / calls * 1000).toFixed(3) : '—'}</td>
            </tr>`;
          }).join('') || '<tr><td colspan="8" class="empty">没有数据</td></tr>'}
        </tbody>
      </table></div></div>

      <div class="note" style="margin-top:14px">
        ${withApi
          ? '<b>含实时采样。</b> 这些会话里有 hook 抓到的 <code>post_api_request</code> 事件，可精确到每次 API 调用的耗时与用量。'
          : '<b>当前只有会话级聚合。</b> state.db 记录每个会话的总量与估算花费；要看到 <code>每次 API 请求</code> 的耗时、缓存命中与包大小，需要装 hook（见仓库 hooks/INSTALL.md），再重建 trace。'}
      </div>

      ${withApi ? `<div class="section-title">API 请求（hook 采样）</div>
        <div class="panel"><div class="tablewrap"><table>
          <thead><tr><th>会话</th><th>时间</th><th>模型</th><th class="num">耗时</th><th class="num">in</th><th class="num">out</th><th>finish</th></tr></thead>
          <tbody>
          ${rows.flatMap((s) => (s.api_calls || []).map((a) => `<tr>
            <td class="mono dim">${esc(clip(s.title || s.id, 24))}</td>
            <td class="mono dim">${esc(t(a.at))}</td>
            <td class="mono dim">${esc(a.model || '')}</td>
            <td class="num">${a.duration_s ? a.duration_s.toFixed(2) + 's' : '—'}</td>
            <td class="num">${fmtInt((a.usage || {}).input_tokens)}</td>
            <td class="num">${fmtInt((a.usage || {}).output_tokens)}</td>
            <td class="mono dim">${esc(a.finish_reason || '')}</td>
          </tr>`)).join('')}
          </tbody>
        </table></div></div>` : ''}
    `;
    box.querySelectorAll('tr[data-session]').forEach((tr) => {
      tr.onclick = () => { state.sessionId = tr.dataset.session; setView('sessions'); };
    });
  }

  /* ---------------- subagents ---------------- */
  function renderSubagents() {
    const box = $('#view-subagents');
    const edges = state.doc.subagents || [];
    const byParent = {};
    for (const e of edges) (byParent[e.parent_session] = byParent[e.parent_session] || []).push(e);
    const ids = new Set(sessions().map((s) => s.id));
    box.innerHTML = `
      <div class="filters"><span class="count">${edges.length} 条委派关系</span><span class="spacer"></span><span class="count">来源：async_delegations + parent_session_id</span></div>
      <div class="panel"><div class="tablewrap"><table>
        <thead><tr><th>父会话</th><th>子会话 / 委派</th><th>目标</th><th>状态</th><th>发起</th><th class="num">耗时</th></tr></thead>
        <tbody>
        ${edges.map((e) => {
          const parentKnown = ids.has(e.parent_session);
          const child = sessionById(e.id);
          const dur = e.dispatched_at && e.completed_at
            ? (new Date(e.completed_at) - new Date(e.dispatched_at))
            : null;
          return `<tr ${child ? `data-session="${esc(child.id)}"` : ''}>
            <td class="mono dim" style="font-size:11px">${esc(clip(e.parent_session, 26))}${parentKnown ? '' : ' <span class="pill pending">不在 trace 内</span>'}</td>
            <td class="mono dim" style="font-size:11px">${esc(clip(e.id, 26))}</td>
            <td><div class="preview" style="max-width:420px">${esc(clip(e.goal || '—', 120))}</div></td>
            <td><span class="pill ${e.state === 'completed' ? 'ok' : e.state === 'failed' ? 'error' : 'pending'}">${esc(e.state)}</span></td>
            <td class="mono dim">${esc(t(e.dispatched_at))}</td>
            <td class="num">${dur === null ? '—' : fmtMs(dur)}</td>
          </tr>`;
        }).join('') || '<tr><td colspan="6" class="empty">没有子代理记录。用 delegate_task 派生子代理后重新生成 trace 即可看到。</td></tr>'}
        </tbody>
      </table></div></div>
      <div class="note" style="margin-top:14px">
        子代理的调用记在它们自己的会话里；这里的父子边用来还原「谁派的、跑了多久、是否失败」。
      </div>
    `;
    box.querySelectorAll('tr[data-session]').forEach((tr) => {
      tr.onclick = () => { state.sessionId = tr.dataset.session; setView('sessions'); };
    });
  }

  /* ---------------- source ---------------- */
  function renderSource() {
    const box = $('#view-source');
    const tools = Object.entries(state.doc.tools || {});
    const counts = {};
    for (const s of sessions()) for (const c of s.calls) counts[c.name] = (counts[c.name] || 0) + 1;
    tools.sort((a, b) => (counts[b[0]] || 0) - (counts[a[0]] || 0) || a[0].localeCompare(b[0]));
    const called = tools.filter(([n]) => counts[n]);
    const never = tools.filter(([n]) => !counts[n]);
    box.innerHTML = `
      <div class="filters">
        <input id="src-filter" placeholder="过滤工具名…" style="padding:6px 8px;border-radius:6px;border:1px solid var(--line-2);background:var(--panel-2);color:var(--ink)">
        <span class="spacer"></span>
        <span class="count">${tools.length} 个工具 · ${called.length} 个被调用过</span>
      </div>
      <div class="panel"><div class="tablewrap" style="max-height:calc(100vh - 200px)"><table>
        <thead><tr><th>工具</th><th>toolset</th><th>实现</th><th>handler</th><th class="num">本次调用</th></tr></thead>
        <tbody id="src-body">
          ${tools.map(([name, info]) => `
            <tr data-tool="${esc(name)}">
              <td><span class="tool-name">${esc(name)}</span></td>
              <td class="mono dim">${esc(info.toolset || '—')}</td>
              <td class="mono dim" style="font-size:11px">${esc(info.file)}:${info.line}</td>
              <td class="mono dim" style="font-size:11px">${esc(info.handler || '')}${info.handler_line ? ' @' + info.handler_line : ''}</td>
              <td class="num">${counts[name] ? counts[name] : '<span class="dim">0</span>'}</td>
            </tr>`).join('')}
        </tbody>
      </table></div></div>
      <div class="note" style="margin-top:14px">
        索引来自对 <code>tools/*.py</code> 里 <code>registry.register(...)</code> 的静态解析，不导入任何代码。点一行可复制 <code>文件:行</code>，用来核对「模型声称调了什么」与「实际实现」是否一致。
      </div>
    `;
    const filter = $('#src-filter');
    filter.oninput = () => {
      const q = filter.value.toLowerCase();
      box.querySelectorAll('#src-body tr').forEach((tr) => {
        tr.style.display = tr.dataset.tool.toLowerCase().includes(q) ? '' : 'none';
      });
    };
    box.querySelectorAll('tr[data-tool]').forEach((tr) => {
      tr.onclick = () => {
        const info = state.doc.tools[tr.dataset.tool];
        if (!info) return;
        const path = `${info.file}:${info.line}`;
        copy(path);
        openSource(tr.dataset.tool, info);
      };
    });
  }

  /* ---------------- raw ---------------- */
  function renderRaw() {
    const box = $('#view-raw');
    const json = JSON.stringify(state.doc, null, 2);
    box.innerHTML = `
      <div class="filters">
        <button class="mini-btn" id="dl">下载 trace.json</button>
        <span class="spacer"></span>
        <span class="count">${(json.length / 1024).toFixed(1)} KiB · schema ${esc(state.doc.schema)}</span>
      </div>
      <pre class="code" style="max-height:calc(100vh - 220px)">${esc(json.slice(0, 240000))}${json.length > 240000 ? '\n… 已截断显示' : ''}</pre>
    `;
    $('#dl').onclick = () => {
      const blob = new Blob([json], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'trace.json';
      a.click();
      URL.revokeObjectURL(a.href);
    };
  }

  /* ---------------- drawer ---------------- */
  function openDrawer(kicker, title, bodyHTML) {
    $('#drawer-kicker').textContent = kicker;
    $('#drawer-title').textContent = title;
    $('#drawer-body').innerHTML = bodyHTML;
    $('#drawer').hidden = false;
    document.querySelector('.app').classList.add('drawer-open');
  }

  function closeDrawer() {
    $('#drawer').hidden = true;
    document.querySelector('.app').classList.remove('drawer-open');
  }

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => toast('已复制：' + text)).catch(() => toast(text));
    } else {
      toast(text);
    }
  }

  function openSource(name, info) {
    openDrawer('source', name, `
      <div class="srcbox">
        <div class="path">${esc(info.file)}:${info.line}</div>
        <div class="meta">toolset ${esc(info.toolset || '—')} · handler ${esc(info.handler || '—')}${info.handler_line ? '（第 ' + info.handler_line + ' 行）' : ''}</div>
      </div>
      <div class="note">这是静态索引结果：<code>registry.register(name=…, handler=…)</code> 的注册行，以及 handler 的 <code>def</code> 行。用它核对模型的说法，而不是相信摘要。</div>
      <dl class="kv">
        <dt>module</dt><dd class="mono">${esc(info.module || '—')}</dd>
        <dt>注册行</dt><dd class="mono">${esc(info.file)}:${info.line}</dd>
        <dt>实现</dt><dd class="mono">${esc(info.file)}:${info.handler_line || '—'}</dd>
      </dl>
      <div class="hstack"><button class="mini-btn" id="copy-src">复制 文件:行</button></div>
    `);
    const b = $('#copy-src');
    if (b) b.onclick = () => copy(`${info.file}:${info.line}`);
  }

  function openCall(call, session) {
    state.selectedCall = call.call_id;
    const info = toolInfo(call.name);
    const argTxt = argsText(call);
    const resTxt = call.result || '';
    openDrawer('tool call', call.name, `
      <dl class="kv">
        <dt>状态</dt><dd><span class="pill ${call.status}">${call.status}</span>${call.source === 'hook' ? ' <span class="pill hook">live</span>' : ''}</dd>
        <dt>耗时</dt><dd class="mono">${fmtMs(call.duration_ms)}</dd>
        <dt>开始</dt><dd class="mono">${esc(call.started_at || '—')}</dd>
        <dt>结束</dt><dd class="mono">${esc(call.ended_at || '—')}</dd>
        <dt>会话</dt><dd class="mono" style="font-size:11px;word-break:break-all">${esc(session.title || session.id)}</dd>
        <dt>turn / seq</dt><dd class="mono">${call.turn ?? '—'} / ${call.seq}</dd>
        <dt>call_id</dt><dd class="mono" style="font-size:11px;word-break:break-all">${esc(call.call_id || '—')}</dd>
        <dt>数据量</dt><dd class="mono">args ${fmtInt(call.args_bytes)} B · result ${fmtInt(call.result_bytes)} B</dd>
      </dl>

      ${info ? `<div class="srcbox">
        <div class="path">${esc(info.file)}:${info.handler_line || info.line}</div>
        <div class="meta">${esc(info.toolset || '')} · handler ${esc(info.handler || '')}</div>
      </div>` : '<div class="note">这个工具没有出现在源码索引里（可能是插件 / MCP / 延迟加载工具）。</div>'}

      <div class="subhead">参数</div>
      <pre class="code">${esc(argTxt || '—')}${call.args_truncated ? '\n… 已按 ' + call.args_bytes + ' B 截断' : ''}</pre>

      <div class="subhead">结果</div>
      <pre class="code">${esc(resTxt || '（没有配对的工具结果：可能是进行中、被中断，或该调用只在 hook 里出现）')}${call.result_truncated ? '\n… 已按 ' + call.result_bytes + ' B 截断' : ''}</pre>

      <div class="hstack">
        <button class="mini-btn" id="copy-call">复制 call_id</button>
        ${info ? `<button class="mini-btn" id="copy-loc">复制 文件:行</button>` : ''}
        <button class="mini-btn" id="goto-session">在会话里看</button>
      </div>
    `);
    const c1 = $('#copy-call'); if (c1) c1.onclick = () => copy(call.call_id || '');
    const c2 = $('#copy-loc'); if (c2) c2.onclick = () => copy(`${info.file}:${info.handler_line || info.line}`);
    const g = $('#goto-session'); if (g) g.onclick = () => { state.sessionId = session.id; closeDrawer(); setView('sessions'); };
  }

  /* ---------------- keyboard ---------------- */
  function moveCursor(delta) {
    if (!state.rows.length) return;
    state.cursor = Math.max(0, Math.min(state.rows.length - 1, state.cursor + delta));
    state.rows.forEach((r) => r.classList.remove('is-sel'));
    const row = state.rows[state.cursor];
    if (row) {
      row.classList.add('is-sel');
      row.scrollIntoView({ block: 'nearest' });
    }
  }

  document.addEventListener('keydown', (e) => {
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
    if (e.key === 'Escape') {
      if (!$('#drawer').hidden) { closeDrawer(); return; }
      if (typing) document.activeElement.blur();
      return;
    }
    if (typing) return;
    if (e.key === '/') { e.preventDefault(); $('#search').focus(); return; }
    if (/^[1-8]$/.test(e.key)) { setView(Object.keys(VIEWS)[Number(e.key) - 1]); return; }
    if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); moveCursor(1); return; }
    if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); moveCursor(-1); return; }
    if (e.key === 'Enter' && state.rows[state.cursor]) { state.rows[state.cursor].click(); return; }
  });

  /* ---------------- search ---------------- */
  function updateSearchCount() {
    const q = state.filters.q.trim();
    $('#search-count').textContent = q ? String(callFiltered().length) : '';
  }

  $('#search').addEventListener('input', (e) => {
    state.filters.q = e.target.value;
    if (state.view !== 'calls') setView('calls');
    else renderCalls();
    updateSearchCount();
  });

  /* ---------------- nav / data ---------------- */
  document.querySelectorAll('.nav-item').forEach((b) => {
    b.onclick = () => {
      if (b.dataset.view === 'sessions') state.sessionId = null;
      setView(b.dataset.view);
    };
  });
  $('#drawer-close').onclick = closeDrawer;

  $('#file-input').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const doc = JSON.parse(reader.result);
        if (!doc.sessions || !doc.schema) throw new Error('不是 htrace 文档（缺 sessions/schema）');
        state.doc = doc;
        state.sessionId = null;
        setMode('local');
        closeDrawer();
        setView('overview');
        toast(`已载入 ${file.name}（${doc.sessions.length} 会话）`);
      } catch (err) {
        toast('读取失败：' + err.message);
      }
    };
    reader.readAsText(file);
  });

  function enterRoute() {
    const route = (location.hash || '#overview').slice(1).split('/');
    if (route[1]) state.sessionId = decodeURIComponent(route[1]);
    setView(route[0] || 'overview', { push: false });
  }

  async function boot() {
    if (window.HTRACE_DEMO) {
      state.doc = window.HTRACE_DEMO;
      setMode('demo');
      enterRoute();
      return;
    }
    try {
      const r = await fetch('/api/trace', { cache: 'no-store' });
      if (r.ok) {
        state.doc = await r.json();
        setMode('local');
        enterRoute();
        return;
      }
    } catch (err) { /* offline file:// — expected */ }
    setMode('local');
    renderEmpty();
  }

  boot();
})();
