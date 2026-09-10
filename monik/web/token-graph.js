'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const presets = [24, 12, 6, 3, 2, 1];
  const metrics = {total_tokens: 'Всего', input_tokens: 'Ввод', output_tokens: 'Вывод', cached_input_tokens: 'Ввод из кэша', uncached_input_tokens: 'Ввод без кэша', reasoning_output_tokens: 'Reasoning в выводе'};
  const url = new URL(location.href);
  const state = {hours: presets.includes(Number(url.searchParams.get('hours'))) ? Number(url.searchParams.get('hours')) : 6,
    metric: 'total_tokens', data: null, hidden: new Set(), paused: false, controller: null,
    generation: 0, timer: null, stream: null, dirty: false, index: null, authenticated: true};
  const ns = 'http://www.w3.org/2000/svg';
  const fmt = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1});
  const n = value => typeof value === 'number' && Number.isFinite(value) ? fmt.format(value) : 'нет измерения';
  const time = value => new Date(value * 1000).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
  const date = value => new Date(value * 1000).toLocaleString('ru-RU');
  const node = (tag, className, text) => {const e = document.createElement(tag); if (className) e.className = className; if (text !== undefined) e.textContent = text; return e;};
  const svgNode = (tag, attrs = {}, text) => {const e = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v)); if (text !== undefined) e.textContent = text; return e;};
  const shown = () => (state.data?.series || []).filter(s => !state.hidden.has(s.profile));
  const palette = s => 'series-' + state.data.series.indexOf(s) % 6;
  const rate = p => p.values[state.metric] === null ? null : p.values[state.metric] * 60 / state.data.bucket_seconds;
  const badge = (text, warning = false) => {const e = $('graph-connection'); e.textContent = text; e.className = 'pill ' + (warning ? 'warn' : 'ok');};
  let theme;
  try {theme = localStorage.getItem('monik-theme');} catch {}
  document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
  $('graph-theme').onclick = () => {const v = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = v; try {localStorage.setItem('monik-theme', v);} catch {}};
  $('graph-timezone').textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;

  function controls() {
    for (const b of $('graph-periods').querySelectorAll('button')) b.setAttribute('aria-pressed', String(Number(b.dataset.hours) === state.hours));
    $('graph-pause').setAttribute('aria-pressed', String(state.paused));
    $('graph-pause').textContent = state.paused ? 'Продолжить' : 'Пауза экрана';
    $('graph-paused').hidden = !state.paused;
  }
  function paintCards() {
    const out = document.createDocumentFragment();
    for (const s of state.data.series) {
      const card = node('article', 'card'), head = node('div', 'graph-card-head');
      const name = node('h2', palette(s)); name.append(node('span', 'graph-swatch'), document.createTextNode(s.label));
      head.append(name, node('small', '', `${s.accepted} ответов в учёте`));
      const value = s.totals[state.metric];
      card.append(head, node('div', 'metric-main', n(value)), node('div', 'metric-label', `${metrics[state.metric]} · за ${state.hours} ч`));
      const max = Math.max(0, ...s.points.map(p => rate(p) ?? 0));
      const grid = node('div', 'metrics-grid');
      for (const [label, v] of [['Среднее за период, ток/мин', value === null ? null : value / (state.hours * 60)], ['Пиковая корзина, ток/мин', value === null ? null : max]]) {
        const box = node('div', 'metric'); box.append(node('div', 'metric-value', n(v)), node('div', 'metric-label', label)); grid.append(box);
      }
      card.append(grid, node('div', 'graph-observed', `Измерений показателя: ${s.measured[state.metric]} · покрытие частичное`)); out.append(card);
    }
    $('graph-cards').replaceChildren(out);
  }
  function paintLegend() {
    const out = document.createDocumentFragment();
    for (const s of state.data.series) {
      const b = node('button', `graph-legend-button ${palette(s)}`);
      b.append(node('span', 'graph-swatch'), document.createTextNode(s.label));
      b.setAttribute('aria-pressed', String(!state.hidden.has(s.profile)));
      b.onclick = () => {if (state.hidden.has(s.profile)) state.hidden.delete(s.profile); else state.hidden.add(s.profile); paintLegend(); paintPlot(); paintTable();};
      out.append(b);
    }
    $('graph-legend').replaceChildren(out);
  }
  function paintPlot() {
    const svg = $('graph-svg'), d = state.data, visible = shown();
    const width = Math.max(220, Math.round(svg.parentElement.clientWidth)), height = width < 600 ? 270 : 330;
    const top = 18, bottom = height - 40, left = 66, right = width - 14;
    state.geometry = {width, left, right}; svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const values = visible.flatMap(s => s.points.map(rate).filter(v => v !== null));
    const max = Math.max(1, ...values) * 1.12;
    const x = value => left + (value - d.window_start) / (d.window_end - d.window_start) * (right - left);
    const y = value => bottom - value / max * (bottom - top);
    svg.replaceChildren(svgNode('title', {id: 'graph-title'}, `${metrics[state.metric]}: токенов в минуту, последние ${state.hours} часов`), svgNode('desc', {id: 'graph-description'}, 'Пустые корзины показаны разрывами. Точные значения доступны ползунком и в таблице.'));
    for (let i = 0; i <= 4; i++) {
      const value = i * max / 4, yy = y(value);
      svg.append(svgNode('line', {x1: left, x2: right, y1: yy, y2: yy, class: 'graph-grid'}), svgNode('text', {x: left - 10, y: yy + 4, class: 'graph-axis', 'text-anchor': 'end'}, n(value)));
    }
    const ticks = width < 600 ? 3 : 6;
    for (let i = 0; i <= ticks; i++) {
      const moment = d.window_start + i / ticks * (d.window_end - d.window_start);
      svg.append(svgNode('text', {x: x(moment), y: height - 8, class: 'graph-axis', 'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle'}, time(moment)));
    }
    for (const s of visible) {
      const group = svgNode('g', {class: palette(s)}); let segment = [];
      const flush = () => {
        if (segment.length > 1) {
          const path = segment.map((p, i) => `${i ? 'L' : 'M'}${p[0]},${p[1]}`).join(' ');
          group.append(svgNode('path', {d: path + ` L${segment.at(-1)[0]},${bottom} L${segment[0][0]},${bottom} Z`, class: 'graph-area'}), svgNode('path', {d: path, class: 'graph-line'}));
        }
        segment = [];
      };
      for (const p of s.points) {
        const value = rate(p);
        if (value === null) {flush(); continue;}
        const xx = x((p.start + p.end) / 2), yy = y(value);
        segment.push([xx, yy]);
        const dot = svgNode('circle', {cx: xx, cy: yy, r: 2.6, class: 'graph-dot'});
        dot.append(svgNode('title', {}, `${s.label} · ${time(p.start)} · ${n(p.values[state.metric])} токенов`)); group.append(dot);
      }
      flush(); svg.append(group);
    }
    svg.append(svgNode('line', {id: 'graph-cursor', class: 'graph-cursor', x1: right, x2: right, y1: top, y2: bottom}));
    $('graph-empty').hidden = values.length > 0;
    const points = d.series[0]?.points || [];
    const slider = $('graph-slider'); slider.max = String(Math.max(0, points.length - 1));
    state.index = Math.min(state.index ?? points.length - 1, points.length - 1);
    inspect(state.index);
  }
  function inspect(index) {
    const d = state.data, p = d?.series[0]?.points[index]; if (!p) return;
    state.index = index; $('graph-slider').value = String(index);
    $('graph-slider').setAttribute('aria-valuetext', `${time(p.start)}–${time(p.end)}`);
    const out = document.createDocumentFragment();
    out.append(node('strong', '', `${time(p.start)}–${time(p.end)}${p.partial ? ' · неполная корзина' : ''}`));
    for (const s of shown()) {
      const v = s.points[index];
      out.append(node('span', palette(s), `${s.label}: ${n(v.values[state.metric])} токенов · ${n(rate(v))} ток/мин`));
    }
    $('graph-inspector').replaceChildren(out);
    const g = state.geometry;
    const x = g.left + (((p.start + p.end) / 2) - d.window_start) / (d.window_end - d.window_start) * (g.right - g.left);
    const cursor = $('graph-cursor'); if (cursor) {cursor.setAttribute('x1', x); cursor.setAttribute('x2', x);}
  }
  function paintTable() {
    const out = document.createDocumentFragment();
    for (const s of shown()) for (const p of s.points) {
      const tr = node('tr', p.state === 'no_records' ? 'graph-no-records' : '');
      const status = p.state === 'no_records' ? 'Нет записей' : p.state === 'excluded' ? 'Измерения исключены' : 'Есть измерения';
      for (const text of [`${time(p.start)}–${time(p.end)}`, s.label, n(p.values[state.metric]), n(rate(p)), `${p.accepted} / ${p.responses}`, status + (p.conflicts ? ` · спорных: ${p.conflicts}` : '') + (p.invalid ? ` · некорректных: ${p.invalid}` : '') + (p.partial ? ' · неполная корзина' : '')]) tr.append(node('td', '', text));
      out.append(tr);
    }
    $('graph-table-body').replaceChildren(out);
  }
  function paint() {
    paintCards(); paintLegend(); paintPlot(); paintTable();
    const d = state.data;
    $('graph-step').textContent = `${metrics[state.metric]} · токенов в минуту · шаг ${d.bucket_seconds / 60} мин`;
    $('graph-updated').textContent = `Снимок: ${date(d.window_end)}`;
    $('graph-demo').hidden = !d.demo;
    const bad = d.series.reduce((n, s) => n + s.conflicts + s.invalid + s.unknown_source_time, 0);
    $('graph-quality').hidden = bad === 0;
    $('graph-quality').textContent = d.series.map(s => `${s.label}: спорных ${s.conflicts}, некорректных ${s.invalid}, без времени события ${s.unknown_source_time}`).join(' · ') + '. Эти данные не превращаются в подтверждённый расход на графике.';
  }
  function schedule() {
    state.dirty = true;
    if (state.timer !== null || state.paused || document.hidden || !state.authenticated) return;
    state.timer = setTimeout(() => {state.timer = null; refresh();}, 900);
  }
  function connect() {
    if (state.stream || !state.data || document.hidden || !state.authenticated) return;
    const stream = new EventSource('/api/v1/stream?after=' + state.data.cursor); state.stream = stream;
    stream.onopen = () => badge('● Соединение LIVE');
    stream.onerror = () => badge('Переподключение', true);
    stream.addEventListener('committed', schedule);
    stream.addEventListener('reset', () => {state.stream?.close(); state.stream = null; schedule();});
    stream.addEventListener('degraded', () => badge('Хранилище недоступно', true));
  }
  async function refresh(force = false) {
    if ((!force && (state.paused || document.hidden)) || !state.authenticated) return;
    if (state.controller) {state.dirty = true; return;}
    const controller = new AbortController(), generation = state.generation; let timedOut = false;
    const timeout = setTimeout(() => {timedOut = true; controller.abort();}, 5000);
    state.controller = controller; state.dirty = false;
    try {
      const r = await fetch('/api/v1/usage-series?hours=' + state.hours, {credentials: 'same-origin', signal: controller.signal});
      if (r.status === 401) {
        state.authenticated = false; state.stream?.close(); state.stream = null; state.data = null; state.index = null; state.dirty = false;
        $('graph-login').hidden = false; $('graph-legend').replaceChildren(); $('graph-quality').hidden = true; $('graph-quality').textContent = ''; $('graph-demo').hidden = true; $('graph-updated').textContent = 'Нужен вход владельца'; $('graph-empty').hidden = true; $('graph-cards').replaceChildren(); $('graph-svg').replaceChildren(); $('graph-table-body').replaceChildren(); $('graph-inspector').replaceChildren();
        badge('Нужен вход', true); return;
      }
      if (!r.ok) {let message; try {message = (await r.json()).detail;} catch {} throw Error(message || `Сервер monik: HTTP ${r.status}`);}
      const data = await r.json();
      if (generation !== state.generation) return;
      state.data = data; paint(); $('graph-error').hidden = true; $('graph-login').hidden = true;
      if (!state.stream) connect();
    } catch (e) {
      if ((e.name !== 'AbortError' || timedOut) && generation === state.generation) {
        if (timedOut) e = Error('сервер не ответил за 5 секунд');
        $('graph-error').textContent = 'Не удалось обновить график: ' + e.message + '. Последний снимок сохранён на экране.';
        $('graph-error').hidden = false; badge('Данные устарели', true);
      }
    } finally {
      clearTimeout(timeout);
      if (state.controller === controller) state.controller = null;
      if (state.dirty) schedule();
    }
  }
  for (const b of $('graph-periods').querySelectorAll('button')) b.onclick = () => {
    state.hours = Number(b.dataset.hours); state.index = null; state.generation++;
    state.controller?.abort(); state.controller = null;
    const u = new URL(location.href); u.searchParams.set('hours', state.hours); history.replaceState(null, '', u);
    controls(); refresh(true);
  };
  $('graph-metric').onchange = e => {state.metric = e.target.value; if (state.data) paint();};
  $('graph-pause').onclick = () => {
    state.paused = !state.paused;
    if (state.paused) {state.generation++; state.controller?.abort(); state.controller = null;}
    controls(); if (!state.paused) refresh(true);
  };
  $('graph-refresh').onclick = () => {state.authenticated = true; refresh(true);};
  $('graph-slider').oninput = e => inspect(Number(e.target.value));
  $('graph-svg').addEventListener('pointermove', e => {
    if (!state.data) return;
    const rect = e.currentTarget.getBoundingClientRect(); const g = state.geometry; const x = (e.clientX - rect.left) / rect.width * g.width;
    const moment = state.data.window_start + Math.max(0, Math.min(1, (x - g.left) / (g.right - g.left))) * (state.data.window_end - state.data.window_start);
    const points = state.data.series[0]?.points || [];
    const i = points.findIndex(p => p.start <= moment && moment < p.end);
    inspect(i < 0 ? points.length - 1 : i);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {state.stream?.close(); state.stream = null;}
    else {if (!state.paused) refresh(); else connect();}
  });
  setInterval(() => {if (!state.paused && !document.hidden) schedule();}, 20000);
  let resizeQueued = false;
  new ResizeObserver(() => {if (resizeQueued || !state.data) return; resizeQueued = true; requestAnimationFrame(() => {resizeQueued = false; if (state.data) paintPlot();});}).observe($('graph-svg').parentElement);
  controls(); refresh(true);
})();
