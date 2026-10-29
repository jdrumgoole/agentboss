/* agentboss dashboard.

   Two data flows: an SSE stream carries the overview (tiles, charts) and pushes
   a refresh of the session table whenever the numbers actually change; the table
   itself is fetched separately so filters and sorting stay server-side. */

const ALLOW_CONTROL = document.documentElement.dataset.allowControl === 'true';
const SERIES_SLOTS = 8;

const state = {
  overview: null,
  sessions: [],
  selected: null,
  sort: 'last_activity',
  order: 'desc',
  filters: { q: '', status: 'live', project: '', model: '', agent_type: '' },
  modelColors: new Map(),
  filtersLoaded: false,
  chart: null,
};

/* ---------- formatting ---------- */

const nf = new Intl.NumberFormat();

function compact(n) {
  if (n === null || n === undefined) return '—';
  if (n < 1000) return nf.format(n);
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + 'k';
  if (n < 1e9) return (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + 'M';
  return (n / 1e9).toFixed(1) + 'B';
}

function bytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function ago(iso) {
  if (!iso) return '—';
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 0) return 'just now';
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

function shortModel(name) {
  return name.replace(/^claude-/, '').replace(/-\d{8}$/, '');
}

function basename(path) {
  const parts = String(path || '').split('/').filter(Boolean);
  return parts[parts.length - 1] || path || 'unknown';
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'style') node.setAttribute('style', value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* ---------- colour: assigned per model name, never per rank ---------- */

// Slots are handed out alphabetically on first sight and then never reassigned,
// so the palette is deterministic on load and a model that appears later cannot
// repaint the ones already on screen. Past the eighth slot, models fold into a
// neutral "other" rather than inventing a ninth hue.
function assignModelColors(names) {
  for (const name of [...names].sort()) {
    if (state.modelColors.has(name)) continue;
    const slot = state.modelColors.size;
    state.modelColors.set(
      name,
      slot < SERIES_SLOTS ? `var(--series-${slot + 1})` : 'var(--border-strong)',
    );
  }
}

function modelColor(name) {
  if (!state.modelColors.has(name)) assignModelColors([name]);
  return state.modelColors.get(name);
}

/* ---------- tiles ---------- */

function renderTiles(o) {
  const tiles = [
    {
      label: 'Live sessions',
      value: o.live_sessions,
      sub: `${o.busy_sessions} busy · ${o.idle_sessions} idle`,
    },
    {
      label: 'Sessions on disk',
      value: o.total_sessions,
      sub: `${o.projects.length} project${o.projects.length === 1 ? '' : 's'}`,
    },
    {
      label: 'Agents',
      value: o.total_agents,
      sub: `${o.subagents} nested · max depth ${o.max_spawn_depth}`,
    },
    {
      label: 'Models in use',
      value: o.models.length,
      sub: o.models.length ? shortModel(o.models[0].model) + ' leads' : 'none recorded',
    },
    {
      label: 'Tokens billed',
      value: compact(o.tokens.total),
      sub: `${compact(o.tokens.output)} out · ${compact(o.tokens.cache_read)} cached`,
    },
    {
      label: 'Tool calls',
      value: compact(o.total_tool_calls),
      sub: `${compact(o.total_messages)} messages`,
    },
  ];

  const host = document.getElementById('tiles');
  host.replaceChildren(
    ...tiles.map((tile) =>
      el('div', { class: 'tile' },
        el('div', { class: 'label', text: tile.label }),
        el('div', { class: 'value', text: typeof tile.value === 'number' ? nf.format(tile.value) : tile.value }),
        el('div', { class: 'sub', text: tile.sub }),
      ),
    ),
  );
}

/* ---------- horizontal bar breakdowns ---------- */

function renderBars(host, rows, { colorOf, formatValue, emptyText }) {
  const node = document.getElementById(host);
  if (!rows.length) {
    node.replaceChildren(el('p', { class: 'empty', text: emptyText }));
    return;
  }
  const max = Math.max(...rows.map((r) => r.value)) || 1;
  node.replaceChildren(
    ...rows.map((row) =>
      el('div', { class: 'bar-row' },
        el('div', { class: 'name', title: row.label, text: row.label }),
        el('div', { class: 'amount', text: formatValue(row) }),
        el('div', { class: 'bar-track' },
          el('div', {
            class: 'bar-fill',
            style: `width:${Math.max(1.5, (row.value / max) * 100)}%;background:${colorOf(row)}`,
          }),
        ),
      ),
    ),
  );
}

function renderModelBars(o) {
  const rows = o.models.map((m) => ({
    key: m.model,
    label: shortModel(m.model),
    value: m.tokens.total,
    messages: m.messages,
  }));
  renderBars('model-bars', rows, {
    colorOf: (row) => modelColor(row.key),
    formatValue: (row) => `${compact(row.value)} tok · ${compact(row.messages)} turns`,
    emptyText: 'No assistant turns recorded yet.',
  });
}

function renderAgentBars(o) {
  const rows = Object.entries(o.agent_types).map(([type, count]) => ({
    key: type,
    label: type,
    value: count,
  }));
  renderBars('agent-bars', rows, {
    colorOf: () => 'var(--series-3)',
    formatValue: (row) => `${nf.format(row.value)}`,
    emptyText: 'No subagents spawned yet.',
  });
}

/* ---------- quota ---------- */

const usd = (n) => `$${n >= 100 ? Math.round(n).toLocaleString() : n.toFixed(2)}`;

function quotaMeter(label, w) {
  const frac = w.used_fraction;
  const level = frac == null ? '' : frac >= 0.9 ? 'critical' : frac >= 0.7 ? 'warning' : 'good';
  const resets = w.active ? `resets ${new Date(w.end).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}` : 'no active block';
  const amount = w.estimated_cap
    ? `${Math.round(frac * 100)}% · ${usd(w.cost)} of ~${usd(w.estimated_cap)} API-equiv`
    : `${usd(w.cost)} API-equiv · no limit hit recorded yet`;
  return el('div', { class: 'bar-row' },
    el('div', { class: 'name', text: label }),
    el('div', { class: 'amount', text: amount }),
    el('div', { class: 'bar-track' },
      el('div', { class: `bar-fill quota-${level}`, style: `width:${Math.min(100, Math.max(1.5, (frac ?? 0) * 100))}%` })),
    el('div', { class: 'sub', text: `${resets} · ${compact(w.tokens)} tok · on pace for ${usd(w.projected_cost)}` }),
  );
}

function renderQuota(q) {
  document.getElementById('quota-meters').replaceChildren(
    quotaMeter('5-hour block', q.five_hour), quotaMeter('7-day window', q.seven_day));

  const max = Math.max(...q.daily.map((d) => d.cost)) || 1;
  const hitDays = new Set(q.limit_hits.map((h) => h.at.slice(0, 10)));
  document.getElementById('quota-daily').replaceChildren(...q.daily.map((d) => {
    const day = d.start.slice(0, 10);
    const models = Object.entries(d.models).map(([m, c]) => `${shortModel(m)} ${usd(c)}`).join(', ');
    return el('div', {
      class: `quota-day${hitDays.has(day) ? ' hit' : ''}`,
      title: `${day}: ${usd(d.cost)}${models ? ` (${models})` : ''}${hitDays.has(day) ? ' · limit hit' : ''}`,
      style: `height:${Math.max(2, (d.cost / max) * 100)}%`,
    });
  }));

  const hits = q.limit_hits.slice(-3).reverse().map((h) =>
    `${h.limit_type.replace('_', '-')} limit on ${new Date(h.at).toLocaleDateString()} at ${h.spend_at_hit != null ? usd(h.spend_at_hit) : '?'}`);
  document.getElementById('quota-hits').textContent = hits.length
    ? `Daily spend, last ${q.daily.length} days (red = limit hit). Recent hits: ${hits.join(' · ')}`
    : `Daily spend, last ${q.daily.length} days. No limit hits recorded, so no cap estimate yet.`;
}

async function refreshQuota() {
  const response = await fetch('/api/quota');
  if (response.ok) renderQuota(await response.json());
}

/* ---------- recommendations ---------- */

const RECS_SHOWN = 6;

function renderRecs(recs) {
  const host = document.getElementById('recs');
  if (!recs.length) {
    host.replaceChildren(el('p', { class: 'empty', text: 'Nothing to flag: no deep contexts, heavy sessions or oversized tool output right now.' }));
    return;
  }
  const card = (r) => el('div', { class: `rec rec-${r.severity}` },
    el('div', { class: 'rec-head' },
      el('span', { class: 'rec-sev', text: r.severity }),
      el('span', { class: 'rec-title', text: r.title }),
      r.estimated_savings != null
        ? el('span', { class: 'rec-save', text: `save ~${usd(r.estimated_savings)} ${r.savings_basis || ''}` })
        : null,
    ),
    el('p', { class: 'rec-detail', text: r.detail }),
    r.session_id ? el('a', {
      class: 'rec-link', href: `#/session/${r.session_id}`, text: 'open session',
      onclick: (event) => { event.preventDefault(); openDrawer(r.session_id); },
    }) : null,
  );
  const shown = recs.slice(0, RECS_SHOWN).map(card);
  const rest = recs.slice(RECS_SHOWN);
  if (rest.length) {
    const more = el('details', { class: 'rec-more' },
      el('summary', { text: `${rest.length} more` }), ...rest.map(card));
    shown.push(more);
  }
  host.replaceChildren(...shown);
}

async function refreshRecs() {
  const response = await fetch('/api/recommendations');
  if (response.ok) renderRecs(await response.json());
}

/* ---------- activity chart ---------- */

function barPath(x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h);
  return `M${x} ${y + h} L${x} ${y + radius} Q${x} ${y} ${x + radius} ${y} ` +
         `L${x + w - radius} ${y} Q${x + w} ${y} ${x + w} ${y + radius} L${x + w} ${y + h} Z`;
}

// The chart is rebuilt only when its shape changes (a new hour bucket, a resize).
// While the shape holds, the bars are re-pointed in place: at a one-second refresh
// a full rebuild would tear out the element under the cursor on every tick and
// the tooltip would never survive long enough to read.
function renderActivity(o) {
  const host = document.getElementById('activity-chart');
  const buckets = o.activity || [];
  if (!buckets.length) {
    state.chart = null;
    host.replaceChildren(el('p', { class: 'empty', text: 'No activity in this window.' }));
    return;
  }

  const width = Math.max(host.clientWidth || 420, 260);
  const starts = buckets.map((b) => b.start).join('|');
  if (state.chart && state.chart.starts === starts && state.chart.width === width) {
    updateActivity(buckets);
    return;
  }
  buildActivity(host, buckets, width, starts);
}

const CHART = { height: 132, pad: { top: 14, right: 6, bottom: 20, left: 34 } };

function plotBox(width) {
  const { height, pad } = CHART;
  return { w: width - pad.left - pad.right, h: height - pad.top - pad.bottom };
}

function buildActivity(host, buckets, width, starts) {
  const NS = 'http://www.w3.org/2000/svg';
  const { height, pad } = CHART;
  const plot = plotBox(width);
  const step = plot.w / buckets.length;
  const barW = Math.max(2, step - 2); // 2px surface gap between adjacent bars

  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('role', 'img');

  const add = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.append(node);
    return node;
  };

  const axisLabels = [];
  for (const frac of [0, 0.5, 1]) {
    const y = pad.top + plot.h * frac;
    add('line', { class: 'gridline', x1: pad.left, x2: width - pad.right, y1: y, y2: y });
    axisLabels.push(add('text', {
      class: 'axis-label', x: pad.left - 6, y: y + 3.5, 'text-anchor': 'end',
    }));
  }

  const marks = buckets.map((_, i) => add('path', {
    class: 'mark',
    'data-x': pad.left + i * step + (step - barW) / 2,
    'data-w': barW,
  }));

  buckets.forEach((_, i) => {
    // The hit target spans the full column height so thin bars stay hoverable.
    const hit = add('rect', {
      class: 'hit', x: pad.left + i * step, y: pad.top, width: step, height: plot.h,
    });
    hit.addEventListener('mousemove', (event) => {
      marks[i].classList.add('on');
      showTooltip(event, state.chart.buckets[i]);
    });
    hit.addEventListener('mouseleave', () => {
      marks[i].classList.remove('on');
      hideTooltip();
    });
  });

  const every = Math.max(1, Math.round(buckets.length / 6));
  buckets.forEach((bucket, i) => {
    if (i % every !== 0 && i !== buckets.length - 1) return;
    const when = new Date(bucket.start);
    const label = add('text', {
      class: 'axis-label',
      x: pad.left + i * step + step / 2,
      y: height - 6,
      'text-anchor': i === buckets.length - 1 ? 'end' : 'middle',
    });
    label.textContent = `${String(when.getHours()).padStart(2, '0')}:00`;
  });

  host.replaceChildren(svg);
  state.chart = { svg, marks, axisLabels, starts, width, buckets: [] };
  updateActivity(buckets);
}

function updateActivity(buckets) {
  const chart = state.chart;
  const plot = plotBox(chart.width);
  const max = Math.max(...buckets.map((b) => b.messages), 1);

  chart.buckets = buckets;
  chart.svg.setAttribute('aria-label', `Assistant turns per hour; peak ${max}`);
  chart.axisLabels.forEach((label, i) => {
    label.textContent = compact(Math.round(max * (1 - [0, 0.5, 1][i])));
  });

  chart.marks.forEach((mark, i) => {
    const h = (buckets[i].messages / max) * plot.h;
    if (h <= 0) {
      mark.removeAttribute('d');
      return;
    }
    const x = Number(mark.dataset.x);
    const w = Number(mark.dataset.w);
    mark.setAttribute('d', barPath(x, CHART.pad.top + plot.h - h, w, h, 4));
  });
}

function showTooltip(event, bucket) {
  const tip = document.getElementById('tooltip');
  const when = new Date(bucket.start);
  tip.replaceChildren(
    el('div', { class: 't-title', text: when.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }),
    el('div', { text: `${nf.format(bucket.messages)} assistant turn${bucket.messages === 1 ? '' : 's'}` }),
    el('div', { text: `${compact(bucket.tokens)} tokens` }),
  );
  tip.hidden = false;
  tip.style.left = `${event.pageX}px`;
  tip.style.top = `${event.pageY - 12}px`;
}

function hideTooltip() {
  document.getElementById('tooltip').hidden = true;
}

/* ---------- sessions table ---------- */

function statusCell(session) {
  // The CLI's own word ("shell", "compacting", ...) is more informative than
  // "busy" when it differs, so show it and keep the coarse status as the title.
  const word = session.activity_state && session.activity_state !== session.status
    ? session.activity_state
    : session.status;
  return el('span', {
    class: `status ${session.status}`,
    title: `${session.status}${session.pid ? ` · pid ${session.pid}` : ''}`,
  },
    el('span', { class: 'glyph', 'aria-hidden': 'true' }),
    el('span', { text: word }),
  );
}

function modelChips(session) {
  const used = new Map();
  for (const m of session.models) used.set(m.model, (used.get(m.model) || 0) + m.tokens.total);
  for (const agent of session.agents || []) {
    for (const m of agent.models) used.set(m.model, (used.get(m.model) || 0) + m.tokens.total);
  }
  const entries = [...used.entries()].sort((a, b) => b[1] - a[1]);
  if (!entries.length) return el('span', { class: 'chip', text: '—' });
  return el('div', { class: 'chips' },
    ...entries.map(([model, total]) =>
      el('span', { class: 'chip', title: `${model}: ${nf.format(total)} tokens` },
        el('span', { class: 'swatch', style: `background:${modelColor(model)}`, 'aria-hidden': 'true' }),
        shortModel(model),
      ),
    ),
  );
}

function renderRows() {
  const body = document.getElementById('rows');
  const sessions = state.sessions;
  document.getElementById('row-count').textContent =
    `${sessions.length} session${sessions.length === 1 ? '' : 's'}`;

  if (!sessions.length) {
    body.replaceChildren(
      el('tr', {}, el('td', { colspan: '9', class: 'dim', style: 'padding:22px 12px' },
        'No sessions match these filters.')),
    );
    return;
  }

  body.replaceChildren(...sessions.map((session) => {
    const label = session.name || session.title || session.session_id.slice(0, 8);
    const row = el('tr', {
      class: state.selected === session.session_id ? 'selected' : '',
      onclick: () => openDrawer(session.session_id),
    },
      el('td', {}, statusCell(session)),
      el('td', {},
        el('div', { class: 'sess-name', text: label }),
        el('div', { class: 'sess-sub', title: session.project, text: basename(session.project) + (session.git_branch ? ` · ${session.git_branch}` : '') }),
      ),
      el('td', {}, modelChips(session)),
      el('td', { class: 'num' }, nf.format(session.agent_count)),
      el('td', { class: 'num dim' }, nf.format(session.subagent_count)),
      el('td', { class: 'num' }, compact(session.user_messages + session.assistant_messages)),
      el('td', { class: 'num dim' }, compact(session.tool_calls)),
      el('td', { class: 'num', title: `${nf.format(session.combined_tokens)} tokens` }, compact(session.combined_tokens)),
      el('td', { class: 'num dim' }, ago(session.last_activity_at)),
    );
    return row;
  }));
}

/* ---------- detail drawer ---------- */

function kv(pairs) {
  const list = el('dl', { class: 'kv' });
  for (const [key, value, mono] of pairs) {
    if (value === null || value === undefined || value === '') continue;
    list.append(el('dt', { text: key }), el('dd', { class: mono ? 'mono' : '', text: String(value) }));
  }
  return list;
}

function agentCard(agent) {
  return el('div', { class: 'agent' },
    el('div', { class: 'head' },
      el('span', { class: 'type', text: agent.agent_type }),
      el('span', { class: 'depth', text: `depth ${agent.spawn_depth}` }),
      agent.worktree_branch ? el('span', { class: 'depth', text: agent.worktree_branch }) : null,
    ),
    agent.description ? el('div', { class: 'desc', text: agent.description }) : null,
    el('div', { class: 'meta' },
      el('span', { text: `${compact(agent.tokens.total)} tokens` }),
      el('span', { text: `${nf.format(agent.messages)} turns` }),
      el('span', { text: `${nf.format(agent.tool_calls)} tools` }),
      el('span', { text: duration(agent.duration_seconds) }),
      ...agent.models.map((m) => el('span', {},
        el('span', { class: 'swatch', style: `display:inline-block;width:7px;height:7px;border-radius:2px;margin-right:4px;background:${modelColor(m.model)}` }),
        shortModel(m.model),
      )),
    ),
  );
}

async function openDrawer(sessionId) {
  state.selected = sessionId;
  // Reflected in the URL so a session view can be bookmarked or shared.
  if (location.hash !== `#/session/${sessionId}`) {
    history.replaceState(null, '', `#/session/${sessionId}`);
  }
  renderRows();
  const drawer = document.getElementById('drawer');
  const backdrop = document.getElementById('backdrop');
  drawer.hidden = false;
  backdrop.hidden = false;
  drawer.replaceChildren(el('p', { class: 'empty', text: 'Loading…' }));

  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
  if (!response.ok) {
    drawer.replaceChildren(el('p', { class: 'empty', text: 'Session not found.' }));
    return;
  }
  const s = await response.json();

  const controls = el('div', { style: 'display:flex;gap:8px;margin-top:10px;flex-wrap:wrap' },
    el('button', {
      class: 'ghost',
      onclick: (event) => {
        navigator.clipboard.writeText(s.resume_command);
        event.target.textContent = 'copied';
        setTimeout(() => { event.target.textContent = 'copy resume command'; }, 1400);
      },
    }, 'copy resume command'),
    ALLOW_CONTROL && s.is_live ? el('button', {
      class: 'ghost', onclick: () => control(s.session_id, 'interrupt'),
    }, 'interrupt') : null,
    ALLOW_CONTROL && s.is_live ? el('button', {
      class: 'ghost danger', onclick: () => control(s.session_id, 'terminate'),
    }, 'terminate') : null,
  );

  const topTools = Object.entries(s.tools).slice(0, 8);

  drawer.replaceChildren(
    el('button', { class: 'ghost close', onclick: closeDrawer }, 'close'),
    el('h2', { text: s.name || s.title || s.session_id.slice(0, 12) }),
    el('div', { class: 'path', text: s.project }),
    el('div', { style: 'margin-top:10px' }, statusCell(s)),
    el('div', { class: 'resume', style: 'margin-top:12px' }, s.resume_command),
    controls,

    el('section', {},
      el('h3', { text: 'Session' }),
      kv([
        ['Session id', s.session_id, true],
        ['Title', s.title],
        ['Pid', s.pid],
        ['Kind', [s.kind, s.entrypoint].filter(Boolean).join(' · ')],
        ['CLI version', s.version],
        ['Git branch', s.git_branch],
        ['Permission mode', s.permission_mode],
        ['Started', s.started_at ? new Date(s.started_at).toLocaleString() : null],
        ['Last active', s.last_activity_at ? `${ago(s.last_activity_at)} (${new Date(s.last_activity_at).toLocaleTimeString()})` : null],
        ['Open tasks', s.open_tasks || null],
        ['Transcript', bytes(s.transcript_bytes)],
      ]),
    ),

    el('section', {},
      el('h3', { text: 'Traffic' }),
      kv([
        ['Turns', `${nf.format(s.user_messages)} user · ${nf.format(s.assistant_messages)} assistant`],
        ['Tool calls', nf.format(s.tool_calls)],
        ['Main-loop tokens', nf.format(s.tokens.total)],
        ['Subagent tokens', nf.format(s.agent_tokens.total)],
        ['Output tokens', `${nf.format(s.tokens.output)} (${nf.format(s.tokens.thinking)} thinking)`],
        ['Cache', `${nf.format(s.tokens.cache_read)} read · ${nf.format(s.tokens.cache_creation)} written`],
        ['Effort', Object.entries(s.efforts).map(([k, v]) => `${k}×${v}`).join(', ')],
      ]),
    ),

    s.models.length ? el('section', {},
      el('h3', { text: 'Models' }),
      el('div', { class: 'bars' },
        ...s.models.map((m) => el('div', { class: 'bar-row' },
          el('div', { class: 'name', text: shortModel(m.model) }),
          el('div', { class: 'amount', text: `${compact(m.tokens.total)} tok · ${nf.format(m.messages)} turns` }),
          el('div', { class: 'bar-track' },
            el('div', {
              class: 'bar-fill',
              style: `width:${(m.tokens.total / Math.max(...s.models.map((x) => x.tokens.total), 1)) * 100}%;background:${modelColor(m.model)}`,
            }),
          ),
        )),
      ),
    ) : null,

    topTools.length ? el('section', {},
      el('h3', { text: 'Top tools' }),
      el('div', { class: 'chips' },
        ...topTools.map(([name, count]) => el('span', { class: 'chip', text: `${name} ${count}` })),
      ),
    ) : null,

    el('section', {},
      el('h3', { text: `Agents (${s.agent_count}${s.spawn_count > s.agent_count ? ` of ${s.spawn_count} spawned` : ''})` }),
      s.agents.length
        ? el('div', {}, ...s.agents.map(agentCard))
        : el('p', { class: 'empty', text: 'This session spawned no subagents.' }),
    ),

    s.last_prompt ? el('section', {},
      el('h3', { text: 'Last prompt' }),
      el('p', { style: 'font-size:12.5px;color:var(--text-secondary);margin:0', text: s.last_prompt }),
    ) : null,
  );
}

function closeDrawer() {
  state.selected = null;
  if (location.hash.startsWith('#/session/')) history.replaceState(null, '', location.pathname);
  document.getElementById('drawer').hidden = true;
  document.getElementById('backdrop').hidden = true;
  renderRows();
}

async function control(sessionId, action) {
  if (!confirm(`${action} session ${sessionId.slice(0, 8)}?`)) return;
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/${action}`, { method: 'POST' });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    alert(detail.detail || `Could not ${action} the session.`);
    return;
  }
  await refreshSessions();
  openDrawer(sessionId);
}

/* ---------- data ---------- */

function queryString() {
  const params = new URLSearchParams({ sort: state.sort, order: state.order });
  for (const [key, value] of Object.entries(state.filters)) {
    if (value) params.set(key, value);
  }
  return params.toString();
}

async function refreshSessions() {
  const response = await fetch(`/api/sessions?${queryString()}`);
  if (!response.ok) return;
  state.sessions = await response.json();
  assignModelColors(state.sessions.flatMap((s) => s.models.map((m) => m.model)));
  renderRows();
}

function applyOverview(overview) {
  state.overview = overview;
  assignModelColors(overview.models.map((m) => m.model));
  renderTiles(overview);
  renderModelBars(overview);
  renderAgentBars(overview);
  renderActivity(overview);
  refreshQuota();
  refreshRecs();
  if (!state.filtersLoaded) loadFilters();
}

async function loadFilters() {
  const response = await fetch('/api/filters');
  if (!response.ok) return;
  const data = await response.json();
  state.filtersLoaded = true;

  fillSelect('f-status', data.statuses.map((s) => [s, s === 'all' ? 'all statuses' : s]), state.filters.status);
  fillSelect('f-project', [['', 'all projects'], ...data.projects.map((p) => [p, basename(p)])], state.filters.project);
  fillSelect('f-model', [['', 'all models'], ...data.models.map((m) => [m, shortModel(m)])], state.filters.model);
  fillSelect('f-agent-type', [['', 'all agent types'], ...data.agent_types.map((t) => [t, t])], state.filters.agent_type);
}

function fillSelect(id, options, selected) {
  const node = document.getElementById(id);
  node.replaceChildren(...options.map(([value, label]) =>
    el('option', { value, selected: value === selected }, label)));
}

function setPulse(kind, text) {
  const pulse = document.getElementById('pulse');
  pulse.className = `pulse ${kind}`;
  document.getElementById('pulse-text').textContent = text;
}

function connect() {
  const source = new EventSource('/api/stream');
  source.addEventListener('overview', (event) => {
    const overview = JSON.parse(event.data);
    applyOverview(overview);
    refreshSessions();
    setPulse('live', `live · scanned in ${Math.round(overview.scan_seconds * 1000)}ms`);
  });
  source.onerror = () => setPulse('stalled', 'reconnecting…');
  source.onopen = () => setPulse('live', 'live');
}

/* ---------- wiring ---------- */

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

function init() {
  document.getElementById('f-search').addEventListener('input', debounce((event) => {
    state.filters.q = event.target.value.trim();
    refreshSessions();
  }, 220));

  for (const [id, key] of [['f-status', 'status'], ['f-project', 'project'], ['f-model', 'model'], ['f-agent-type', 'agent_type']]) {
    document.getElementById(id).addEventListener('change', (event) => {
      state.filters[key] = event.target.value;
      refreshSessions();
    });
  }

  for (const th of document.querySelectorAll('th.sortable')) {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (state.sort === key) state.order = state.order === 'desc' ? 'asc' : 'desc';
      else { state.sort = key; state.order = 'desc'; }
      for (const other of document.querySelectorAll('th.sortable .arrow')) other.remove();
      th.append(el('span', { class: 'arrow', text: state.order === 'desc' ? ' ↓' : ' ↑' }));
      refreshSessions();
    });
  }

  document.getElementById('backdrop').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDrawer(); });

  const toggle = document.getElementById('theme-toggle');
  toggle.addEventListener('click', () => {
    const current = document.documentElement.dataset.theme;
    const next = current === 'dark' ? 'light' : current === 'light' ? '' : 'dark';
    if (next) document.documentElement.dataset.theme = next;
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem('agentboss-theme', next); } catch { /* ignore */ }
    if (state.overview) renderActivity(state.overview);
  });
  try {
    const saved = localStorage.getItem('agentboss-theme');
    if (saved) document.documentElement.dataset.theme = saved;
  } catch { /* ignore */ }

  window.addEventListener('resize', debounce(() => {
    if (state.overview) renderActivity(state.overview);
  }, 180));

  // Keep "3m ago" honest between pushes.
  setInterval(renderRows, 15000);

  refreshSessions();
  connect();

  const deepLink = location.hash.match(/^#\/session\/(.+)$/);
  if (deepLink) openDrawer(decodeURIComponent(deepLink[1]));
}

document.addEventListener('DOMContentLoaded', init);
