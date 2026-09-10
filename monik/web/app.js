'use strict';
const $ = id => document.getElementById(id), F = ['profile', 'thread_id', 'model', 'kind', 'q', 'search_mode', 'view', 'since', 'until', 'at'];
const S = { tab: 'overview', params: {}, cursor: 0, es: null, authed: false, past: false, pending: 0, before: null, next: null, follow: true, version: 0, timer: null, inflight: false, again: false, detail: null, paused: false, history: [], forceQueued: false, labels: {} };
const number = v => typeof v !== 'number' || !Number.isFinite(v) ? 'нет измерения' : new Intl.NumberFormat('ru-RU').format(v);
const percent = v => typeof v === 'number' && Number.isFinite(v) ? number(v) + '%' : 'нет измерения';
const date = v => v === null || v === undefined ? 'время неизвестно' : new Date(typeof v === 'number' ? v * 1000 : v).toLocaleString('ru-RU');
const age = v => v === null || v === undefined ? 'нет измерения' : v < 60 ? `${Math.floor(v)} с` : v < 3600 ? `${Math.floor(v / 60)} мин` : `${Math.floor(v / 3600)} ч`;
const names = { astra: 'Astra', sol: 'Sol' };
const kinds = {}, states = {};
function el(tag, cls, text) { const n = document.createElement(tag); if (cls)
    n.className = cls; if (text !== undefined && text !== null)
    n.textContent = String(text); return n; }
function append(parent, ...children) { for (const c of children)
    if (c)
        parent.append(c); return parent; }
function button(text, fn, cls = '') { const b = el('button', cls, text); b.type = 'button'; b.addEventListener('click', fn); return b; }
function empty(text = 'Нет доступных свидетельств для выбранного периода.') { return el('div', 'empty', text); }
function metric(label, value) { return append(el('div', 'metric'), el('div', 'metric-value', number(value)), el('div', 'metric-label', label)); }
function params(extra = {}) { const p = new URLSearchParams({ ...S.params, ...extra }); for (const [k, v] of p)
    if (v === null || v === '' || v === 'undefined')
        p.delete(k); return p; }
async function api(path, extra = {}, options) { const r = await fetch('/api/v1/' + path + '?' + params(extra), options || { credentials: 'same-origin' }); if (r.status === 401) {
    showLogin();
    throw Error('Нужен вход владельца.');
} if (!r.ok) {
    let msg;
    try {
        msg = (await r.json()).detail;
    }
    catch {
        msg = `HTTP ${r.status}`;
    }
    throw Error(S.labels.errors?.[msg] || msg);
} return r.json(); }
function showError(e) { $('error').textContent = String(e.message || e); $('error').hidden = false; }
function showLogin() { S.authed = false; if (S.es)
    S.es.close(); S.es = null; S.version++; S.detail = null; if ($('detail').open)
    $('detail').close(); $('content').replaceChildren(); $('detail-body').replaceChildren(); $('workspace').hidden = true; $('login-panel').hidden = false; $('logout').hidden = true; $('connection').textContent = 'Нужен вход'; }
function showWorkspace() { S.authed = true; $('workspace').hidden = false; $('login-panel').hidden = true; $('logout').hidden = false; }
function markMode() { S.past = Boolean(S.params.at); $('notice').hidden = !S.past; $('notice').textContent = S.past ? `ПРОШЛОЕ · ${date(S.params.at)} · ${S.params.view === 'knowledge' ? 'только известное монитору на тот момент' : 'по времени событий, включая поздний backfill'}. Входящие события не меняют T.` : ''; $('filter-count').textContent = Object.entries(S.params).filter(([k, v]) => v && k !== 'view' && k !== 'search_mode').length ? 'Фильтры активны' : ''; }
function evidenceButton(uid, text = 'Источник') { return button(text, () => openDetail(uid), 'linklike'); }
function limitBlock(x) {
    const n = el('div', 'limit-block');
    const val = x.remaining_percent === null || x.remaining_percent === undefined ? 'Нет измерения' : `${number(x.remaining_percent)}% осталось`;
    append(n, append(el('div', 'limit-title'), el('strong', '', val), el('span', 'small muted', `${x.limit_id} · ${S.labels.windows?.[x.role] || x.role}`)));
    if (x.valid) {
        const bar = document.createElement('progress');
        bar.max = 100;
        bar.value = x.remaining_percent;
        bar.setAttribute('aria-label', `${x.limit_id}: осталось ${x.remaining_percent}%`);
        n.append(bar);
    }
    append(n, el('div', 'small muted', `Использовано: ${percent(x.used_percent)} · окно: ${number(x.window_minutes)} мин`), el('div', 'small muted', `Сброс: ${date(x.resets_at)} · возраст снимка: ${age(x.age_seconds)}${x.stale ? ' · УСТАРЕЛ' : ''}`));
    if (x.labels && x.labels.length)
        n.append(el('div', 'small warn', x.labels.map(k => ({ non_monotonic_snapshot: 'Процент уменьшился', window_changed: 'Окно изменилось', reset_time_changed: 'Срок сброса изменился' }[k] || k)).join(' · ') + ' · причина неизвестна'));
    n.append(evidenceButton(x.uid));
    return n;
}
function table(headers, rows) { const wrap = el('div', 'table-wrap'), t = el('table'), head = el('tr'); for (const h of headers)
    head.append(el('th', '', h)); t.append(append(el('thead'), head)); const body = el('tbody'); for (const row of rows) {
    const tr = el('tr');
    for (const value of row) {
        const cell = el('td');
        if (value instanceof Node)
            cell.append(value);
        else
            cell.textContent = value === null || value === undefined ? 'нет измерения' : String(value);
        tr.append(cell);
    }
    body.append(tr);
} t.append(body); wrap.append(t); return wrap; }
function title(text, note) { return append(el('div', 'section-heading'), el('h2', '', text), el('span', 'small muted', note)); }
function renderOverview(o, l, t) {
    const out = el('div', 'stack'), cards = el('div', 'cards');
    for (const p of o.profiles) {
        if (S.params.profile && p.profile !== S.params.profile)
            continue;
        const c = el('article', 'card'), st = p.settings?.data || {}, own = p.own_usage?.total_tokens, tree = p.usage.total_tokens;
        const heading = el('div', 'card-heading');
        append(heading, append(el('div', 'identity'), el('span', 'avatar', p.profile.slice(0, 1).toUpperCase()), el('h2', '', names[p.profile] || p.profile)), el('span', 'pill', 'ЧАСТИЧНОЕ ПОКРЫТИЕ'));
        c.append(heading);
        append(c, el('div', 'settings-line', `${st.model || 'модель неизвестна'} · ${st.effort || st.reasoning_effort || 'effort неизвестен'} · ${st.service_tier || 'tier неизвестен'}`), el('div', 'metric-main', number(tree)), el('div', 'muted small', 'Токены по уникальным ответам · профиль с дочерними ветками'));
        c.append(append(el('div', 'metrics-grid'), metric('Корневая сессия', own), metric('Кэшированный ввод', p.usage.cached_input_tokens), metric('Ответов в учёте', p.usage.responses)));
        const node = t.items.find(n => n.profile === p.profile && n.thread_id === p.root_id);
        append(c, el('div', 'small', node ? (states[node.state] || node.state) + (node.stale ? ' · УСТАРЕЛО' : '') : 'Живое состояние не доказано'), el('div', 'small muted mono', `root: ${p.root_id || 'не установлен'}`));
        if (p.activity)
            append(c, el('div', 'small separated', `Последняя активность: ${p.activity.text.slice(0, 260)}`), el('div', 'small muted', date(p.activity.event_at ?? p.activity.time)));
        if (p.usage.conflicts)
            c.append(el('p', 'small warn', `Спорных измерений: ${p.usage.conflicts}. Они исключены из суммы.`));
        if (p.goal)
            c.append(el('p', 'small separated', `Цель: ${p.goal.text.slice(0, 800)}`));
        if (p.settings)
            append(c, el('div', 'small muted', `Последние известные настройки · ${date(p.settings.time)}`), evidenceButton(p.settings.uid, 'Источник настроек'));
        const buckets = l.latest.filter(x => x.profile === p.profile);
        const sec = el('div', 'separated');
        if (buckets.length)
            for (const x of buckets)
                sec.append(limitBlock(x));
        else
            sec.append(el('p', 'muted small', 'Снимков лимитов пока нет. Это не означает 0%.'));
        c.append(sec);
        cards.append(c);
    }
    out.append(cards);
    const note = el('section', 'panel');
    append(note, title('Что здесь считается', 'Никакой магии в процентах'), el('p', 'muted', 'input + output = total. Кэш уже входит во ввод, reasoning уже входит в вывод. Снимки лимитов и накопительные счётчики показываются отдельно и не прибавляются к расходу.'), button('Открыть ленту команды', () => setTab('activity')));
    out.append(note);
    return out;
}
function renderActivity(data, old) {
    const out = el('div', 'stack'), p = el('section', 'panel');
    append(p, title('Активность и чат', `До 80 событий на странице · ${data.coverage === 'partial' ? 'частичное покрытие' : data.coverage}`));
    const feed = el('div', 'feed');
    feed.id = 'feed';
    feed.tabIndex = 0;
    feed.setAttribute('aria-label', 'Лента событий');
    S.next = data.next_before;
    if (!data.items.length)
        feed.append(empty());
    for (const x of [...data.items].reverse()) {
        const e = el('article', 'event'), h = el('div', 'event-header');
        append(h, el('span', 'pill', names[x.profile] || x.profile), el('span', '', kinds[x.kind] || `Неизвестный тип: ${x.kind}`), el('time', '', date(x.event_at ?? x.time)), evidenceButton(x.uid, 'Детали'));
        append(e, h, el('pre', 'event-text' + (x.kind.startsWith('tool_') ? ' event-code' : ''), x.text || 'Запись без текстового содержимого'), el('span', 'event-thread mono', `${x.thread_id} · ${x.model || 'модель не установлена'}${x.event_at === null ? ' · время наблюдения' : ''}`));
        feed.append(e);
    }
    p.append(feed);
    const actions = el('div', 'feed-actions'), older = button('Более ранние', () => { S.history.push(S.before); S.before = S.next; S.follow = false; S.version++; refresh(true); });
    older.disabled = !S.next;
    const fresh = button('', () => { S.before = null; S.follow = true; S.pending = 0; refresh(true); }, 'primary');
    fresh.id = 'new-events';
    const newer = button('Более поздние', () => { S.before = S.history.pop() ?? null; S.follow = !S.before; S.version++; refresh(true); });
    newer.disabled = !S.history.length;
    append(actions, older, newer, fresh, button('Экспорт этой страницы', exportPage));
    p.append(actions);
    out.append(p);
    out.append(comparePanel());
    requestAnimationFrame(() => { if (!feed.isConnected)
        return; if (old && !S.follow && !S.before)
        feed.scrollTop = old.scrollTop;
    else
        feed.scrollTop = S.follow ? feed.scrollHeight : 0; updateNew(); });
    feed.addEventListener('scroll', () => { S.follow = feed.scrollHeight - feed.clientHeight - feed.scrollTop < 45 && !S.before; updateNew(); }, { passive: true });
    return out;
}
function updateNew() { const b = $('new-events'); if (b)
    b.textContent = S.past ? `Обновить срез T${S.pending ? ' · LIVE: +' + S.pending : ''}` : S.pending ? `Новых событий: ${S.pending} · к LIVE` : (S.past ? 'Обновить срез T' : S.before || !S.follow ? 'К последним событиям' : 'Лента LIVE'); }
function comparePanel() { const d = el('details', 'panel compare'); d.append(el('summary', '', 'Сравнить состояние A / B')); const grid = el('div', 'compare-grid'); for (const id of ['A', 'B']) {
    const label = el('label', '', `Момент ${id}`), input = el('input');
    input.type = 'datetime-local';
    input.step = '1';
    input.id = 'compare-' + id;
    input.value = S.params.at ? toLocal(S.params.at) : toLocal(new Date(Date.now() - (id === 'A' ? 3600000 : 0)).toISOString());
    label.append(input);
    grid.append(label);
} d.append(grid); const result = el('div'); const b = button('Сравнить', async () => { try {
    b.disabled = true;
    const a = $('compare-A').value, z = $('compare-B').value;
    if (!a || !z)
        throw Error('Укажи оба момента.');
    const [A, B] = await Promise.all([api('overview', { at: new Date(a).toISOString() }), api('overview', { at: new Date(z).toISOString() })]);
    result.replaceChildren(table(['Профиль', 'A: модель / токены', 'B: модель / токены', 'Разница измеренных токенов'], A.profiles.map((x, i) => [names[x.profile] || x.profile, `${x.settings?.data?.model || 'неизвестно'} / ${number(x.usage.total_tokens)}`, `${B.profiles[i].settings?.data?.model || 'неизвестно'} / ${number(B.profiles[i].usage.total_tokens)}`, x.usage.total_tokens !== null && B.profiles[i].usage.total_tokens !== null ? number(B.profiles[i].usage.total_tokens - x.usage.total_tokens) : 'нет сопоставимого измерения'])));
}
catch (e) {
    showError(e);
}
finally {
    b.disabled = false;
} }); append(d, b, result, el('p', 'small muted', 'Это разница измеренных ответов в двух срезах, не разница процентов лимита. Интервал и остальные фильтры сохраняются.')); return d; }
function renderTokens(u, c) {
    const p = el('section', 'panel');
    append(p, title('Токены без двойного учёта', 'Уникальные ответы (response_id)'));
    const grid = el('div', 'stat-row');
    for (const [label, key] of [['Ввод', 'input_tokens'], ['Ввод из кэша', 'cached_input_tokens'], ['Ввод без кэша', 'uncached_input_tokens'], ['Вывод', 'output_tokens'], ['Reasoning в выводе', 'reasoning_output_tokens'], ['Всего', 'total_tokens']])
        grid.append(append(el('div', 'stat-tile'), el('strong', '', number(u[key])), el('div', 'small muted', label), el('div', 'small muted', `Измерений: ${number(u[key + '_measured'])}`)));
    append(p, grid, el('p', 'muted', `Ответов: ${number(u.responses)} · некорректных: ${number(u.invalid || 0)} · спорных: ${number(u.conflicts || 0)} · cache hit: ${u.cache_hit_ratio === null ? 'нет измерения' : number(Math.round(u.cache_hit_ratio * 10000) / 100) + '%'}`), el('p', 'small muted', 'total = input + output; cached ⊂ input; reasoning ⊂ output. Учёт: профиль + исходная ветка + response_id. Таблица группирует измерения по моделям; неизвестная модель остаётся неизвестной.'), table(['Профиль', 'Ветка', 'Модель', 'Ответов', 'Всего', 'Свидетельства'], u.groups.map(x => [names[x.profile] || x.profile, el('code', '', x.thread_id), x.model || 'неизвестно', number(x.responses), number(x.total_tokens), button('Ответы', () => { S.params.profile = x.profile; S.params.thread_id = x.thread_id; S.params.kind = 'usage'; syncFilters(); setTab('activity'); })])));
    const count = c.summary;
    append(p, el('h3', 'separated', 'Накопительные счётчики: отдельный диагностический ряд'), el('p', 'small muted', `Базовых точек: ${count.baselines}; повторов: ${count.repeats}; отрицательных изменений: ${count.negative_changes}; известные положительные дельты: ${number(count.known_positive_delta)}. Не включены в total. ${c.capped ? 'Достигнут предел выборки.' : ''}`), table(['Время', 'Профиль', 'Значение', 'Δ', 'Статус'], c.items.slice(-30).map(x => [date(x.event_at ?? x.time), x.profile, number(x.value), number(x.delta), { baseline: 'База, расход неизвестен', repeat: 'Повтор', negative_change: 'Уменьшение: разрыв', positive_delta: 'Положительная дельта', invalid: 'Некорректно' }[x.status] || x.status])));
    return p;
}
function chart(items) { const valid = items.filter(x => x.valid).sort((a, b) => a.time - b.time); if (valid.length < 2)
    return null; const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'); svg.classList.add('chart'); svg.setAttribute('viewBox', '0 0 700 150'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'История оставшегося процента, ступенчатые снимки, от 0 до 100'); const start = valid[0].time, end = valid.at(-1).time; const x = t => 10 + 680 * (t - start) / Math.max(1, end - start), y = v => 140 - v * 1.3; let path = `M ${x(start)} ${y(valid[0].remaining_percent)}`; for (const v of valid.slice(1))
    path += ` H ${x(v.time)} V ${y(v.remaining_percent)}`; const line = document.createElementNS(ns, 'path'); line.setAttribute('d', path); line.setAttribute('fill', 'none'); line.setAttribute('stroke', 'currentColor'); line.setAttribute('stroke-width', '2.5'); svg.append(line); return svg; }
function renderLimits(l) {
    const out = el('div', 'stack'), cards = el('div', 'cards');
    for (const x of l.latest) {
        const c = el('article', 'card');
        append(c, title(`${names[x.profile] || x.profile} · ${x.limit_id}`, x.role), limitBlock(x), chart(l.history.filter(y => y.profile === x.profile && y.limit_id === x.limit_id && y.role === x.role)), el('p', 'small muted', 'График: осталось, 0–100%. Между снимками точное состояние неизвестно.'));
        cards.append(c);
    }
    out.append(cards.childElementCount ? cards : empty('Сохранённых снимков лимита пока нет.'));
    const h = el('section', 'panel');
    append(h, title('История снимков', 'Причины изменений не определяются'), el('p', 'muted', 'Это лимит учётной записи. На него может влиять работа вне наблюдаемых веток. Падения и возвраты процентов не складываются в «расход».'), table(['Профиль / лимит', 'Снимок', 'Осталось', 'Использовано', 'Окно, мин', 'Сброс', 'Источник'], l.history.slice(0, 100).map(x => [`${x.profile} / ${x.limit_id} / ${x.role}`, date(x.event_at ?? x.time), x.remaining_percent === null ? 'нет измерения' : number(x.remaining_percent) + '%', percent(x.used_percent), number(x.window_minutes), date(x.resets_at), evidenceButton(x.uid)])), el('p', 'small muted', `Показано до 100 последних снимков в интерфейсе; API истории ограничен 2000. ${l.history_capped ? 'В API достигнут предел; используй момент T для более ранней истории.' : ''}`));
    out.append(h);
    return out;
}
function renderTree(t) { const p = el('section', 'panel'); append(p, title('Дерево агентов', 'Статус по свидетельствам, не по предположению')); if (!t.items.length)
    p.append(empty()); const by = new Map(t.items.map(n => [n.profile + ':' + n.thread_id, n])); function depth(n) { const seen = new Set(); let d = 0; while (n?.parent_thread_id && !seen.has(n.thread_id)) {
    seen.add(n.thread_id);
    n = by.get(n.profile + ':' + n.parent_thread_id);
    d++;
} return Math.min(d, 2); } for (const n of [...t.items].sort((a, b) => a.profile.localeCompare(b.profile) || depth(a) - depth(b))) {
    const c = el('article', 'tree-node depth-' + depth(n));
    append(c, el('h3', '', `${names[n.profile] || n.profile} · ${n.nickname || n.thread_id}`), el('div', 'small mono', n.thread_id), el('div', 'small muted', `Родитель: ${n.parent_thread_id || 'нет подтверждённого родителя'}`), el('div', n.stale ? 'warn' : '', `${states[n.state] || n.state}${n.stale ? ' · УСТАРЕЛО' : ''}`), el('div', 'small muted', `Возраст состояния: ${age(n.age_seconds)} · assignment: ${n.assignment_id || 'нет данных'} · generation: ${n.generation ?? 'нет данных'}`), append(el('div', 'button-row'), evidenceButton(n.uid), button('Лента ветки', () => { S.params.profile = n.profile; S.params.thread_id = n.thread_id; syncFilters(); setTab('activity'); })));
    p.append(c);
} p.append(el('p', 'small muted', 'Топология из state/registry появляется в истории только с момента наблюдения. Без новых подтверждений старое «активен» не становится текущей активностью. Глубина отступа ограничена, исходные parent ID сохранены.')); return p; }
function renderQuality(q) { const out = el('div', 'stack'), p = el('section', 'panel'), c = q.collector; append(p, title('Качество наблюдения', 'Текущее состояние коллектора, не срез T'), el('p', 'muted', `Циклов: ${number(c.ticks)} · физических JSONL-источников: ${number(c.physical_sources)} · прочитанных записей: ${number(c.source_reads)} · последнее успешное чтение цикла: ${date(c.last_success)}`)); const stats = el('div', 'stat-row'); for (const [label, v] of [['Вызовов моделей', c.model_calls], ['RPC-вызовов', c.rpc_calls], ['Доп. доставок', q.additional_deliveries], ['Свободно, МиБ', Math.floor(q.disk.free_bytes / 1048576)]])
    stats.append(append(el('div', 'stat-tile'), el('strong', '', number(v)), el('span', 'small muted', label))); append(p, stats, el('p', 'small muted', q.filesystem_boundary?.landlock_abi ? `Запрет записи вне monik: Landlock ABI ${q.filesystem_boundary.landlock_abi}` : 'Изолированный тестовый запуск без Landlock; production-команда serve требует ABI ≥ 3.'), table(['Профиль / источник', 'Тип', 'Состояние', 'Проверен', 'Offset / размер', 'Примечание'], q.sources.map(x => [`${x.profile} / ${x.source}`, x.kind, { watching: 'Наблюдается', mapped: 'Сопоставлен', unknown: 'Неизвестно', missing: 'Источник отсутствует', error: 'Ошибка', conflict: 'Конфликт идентичности', partial_line: 'Незавершённая строка', catching_up: 'Догоняем историю', replaced: 'Файл заменён', gap: 'Разрыв' }[x.status] || x.status, `${age(x.check_age_seconds)} назад`, `${number(x.offset)} / ${number(x.size)}`, x.detail])), el('p', 'small muted', `Последняя ошибка цикла: ${c.last_error || 'не зафиксирована'}. Потеря источника не стирает накопленную историю.`)); out.append(p); const coverage = el('section', 'panel'); append(coverage, title('Покрытие и границы достоверности', 'Неизвестное не превращается в ноль'), el('p', 'muted', 'Не собираются: скрытые рассуждения, промежуточные streaming-дельты, сетевые ретраи, биллинг, app-server RPC, тела файлов handoff и thread-history DB. Тексты инструментов очищаются от типовых секретов, но автоматическая очистка не гарантирует удаления всех возможных секретов.'), table(['Профиль', 'Тип', 'Записей', 'Первое событие', 'Последнее событие', 'Без времени источника'], q.intervals.map(x => [x.profile, kinds[x.kind] || x.kind, number(x.records), date(x.earliest), date(x.latest), number(x.unknown_time)]))); out.append(coverage); return out; }
async function refresh(force = false) {
    if (!S.authed)
        return;
    if (S.inflight) {
        S.again = true;
        S.forceQueued ||= force;
        return;
    }
    S.inflight = true;
    const version = S.version;
    try {
        const old = $('feed') ? { scrollTop: $('feed').scrollTop } : null;
        let node;
        if (S.tab === 'overview') {
            const [o, l, t] = await Promise.all([api('overview'), api('limits'), api('threads')]);
            if (version !== S.version)
                return;
            S.cursor = Math.max(S.cursor, o.cursor);
            $('demo-badge').hidden = !o.demo;
            syncProfiles(o.profiles);
            node = renderOverview(o, l, t);
        }
        else if (S.tab === 'activity') {
            if (!force && (!S.follow || S.paused || S.past))
                return;
            const data = await api('events', { limit: 80, before: S.before || '' });
            if (version !== S.version)
                return;
            node = renderActivity(data, old);
        }
        else if (S.tab === 'tokens') {
            const [u, c] = await Promise.all([api('usage'), api('cumulative')]);
            if (version !== S.version)
                return;
            node = renderTokens(u, c);
        }
        else if (S.tab === 'limits')
            node = renderLimits(await api('limits'));
        else if (S.tab === 'tree')
            node = renderTree(await api('threads'));
        else
            node = renderQuality(await api('health/sources'));
        if (version !== S.version)
            return;
        $('content').replaceChildren(node);
        if (!S.past && S.follow && !S.before) {
            S.pending = 0;
            updateNew();
        }
        $('error').hidden = true;
        $('updated').textContent = `Обновлено ${new Date().toLocaleTimeString('ru-RU')}`;
        if (!S.es)
            connect();
    }
    catch (e) {
        showError(e);
    }
    finally {
        S.inflight = false;
        if (S.again) {
            const forced = S.forceQueued;
            S.again = false;
            S.forceQueued = false;
            queueRefresh(forced);
        }
    }
}
function canRefresh() { return !S.paused && !S.past && !document.hidden && !$('detail').open && !document.querySelector('.compare[open]') && !window.getSelection()?.toString(); }
function queueRefresh(force = false) { S.forceQueued ||= force; if (S.timer !== null)
    return; S.timer = setTimeout(() => { S.timer = null; const forced = S.forceQueued; S.forceQueued = false; if (forced || canRefresh())
    refresh(forced); }, 250); }
function connect() { if (!S.authed)
    return; if (S.es)
    S.es.close(); const es = new EventSource('/api/v1/stream?after=' + S.cursor); S.es = es; es.onopen = () => { $('connection').textContent = '● Соединение LIVE'; $('connection').className = 'pill ok'; }; es.onerror = () => { $('connection').textContent = 'Переподключение'; $('connection').className = 'pill warn'; if (Date.now() - (S.authCheck || 0) > 5000) {
    S.authCheck = Date.now();
    api('overview').catch(() => { });
} }; es.addEventListener('committed', e => { const data = JSON.parse(e.data); S.cursor = data.cursor; S.pending += data.ids.length; updateNew(); if (canRefresh() && (S.tab !== 'activity' || S.follow))
    queueRefresh(); }); es.addEventListener('reset', () => { S.cursor = 0; S.pending = 0; if (!S.past)
    queueRefresh(true); }); es.addEventListener('degraded', () => { $('connection').textContent = 'Ошибка хранилища'; $('connection').className = 'pill warn'; }); es.addEventListener('pulse', () => { if (canRefresh() && (S.tab !== 'activity' || S.follow))
    queueRefresh(); }); }
function setTab(tab) { S.tab = tab; S.before = null; S.history = []; S.follow = true; S.version++; document.querySelectorAll('[data-tab]').forEach(b => { if (b.dataset.tab === tab)
    b.setAttribute('aria-current', 'page');
else
    b.removeAttribute('aria-current'); }); refresh(true); }
function toLocal(iso) { const d = new Date(iso); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); }
function syncFilters() { for (const k of F)
    $(k).value = S.params[k] ? (k === 'at' || k === 'since' || k === 'until' ? toLocal(S.params[k]) : S.params[k]) : (k === 'view' ? 'event' : k === 'search_mode' ? 'prefix' : ''); markMode(); }
$('filters').addEventListener('submit', e => { e.preventDefault(); const p = {}; try {
    for (const k of F) {
        const v = $(k).value.trim();
        if (v)
            p[k] = ['at', 'since', 'until'].includes(k) ? new Date(v).toISOString() : v;
    }
    S.params = p;
    S.before = null;
    S.history = [];
    S.pending = 0;
    S.follow = true;
    S.version++;
    markMode();
    refresh(true);
}
catch (e) {
    showError(e);
} });
$('clear').onclick = () => { S.params = {}; S.before = null; S.history = []; S.follow = true; S.version++; syncFilters(); refresh(true); };
$('live').onclick = () => { delete S.params.at; delete S.params.until; S.before = null; S.history = []; S.follow = true; S.version++; syncFilters(); refresh(true); };
async function exportPage() { try {
    const data = await api('export', { limit: 80, before: S.before || '' });
    const a = document.createElement('a'), url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    a.href = url;
    a.download = 'monik-events.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}
catch (e) {
    showError(e);
} }
async function openDetail(uid, offset = 0) { const version = S.version; S.detailRequest = uid; try {
    const data = await api('events/' + uid, { offset });
    if (!S.authed || version !== S.version || S.detailRequest !== uid)
        return;
    S.detail = { uid, next: data.next_offset };
    const b = $('detail-body');
    b.replaceChildren();
    const dl = el('dl', 'definition');
    for (const [label, v] of [['ID', uid], ['Профиль', data.profile], ['Ветка', data.thread_id], ['Тип', kinds[data.kind] || data.kind], ['Время события', date(data.event_at)], ['Время приёма', date(data.ingested_at)], ['Время наблюдения', date(data.observed_at)], ['Происхождение', data.timestamp_kind], ['Доставок', data.provenance_count]])
        append(dl, el('dt', '', label), el('dd', '', v));
    append(b, dl, el('p', 'small muted', `Очищенные данные · символы ${offset}–${Math.min(offset + 32768, data.payload_characters)} из ${data.payload_characters}. Типовые секреты удаляются по правилам, но полная очистка не гарантируется. Скрытые рассуждения не собираются.`), el('pre', '', data.payload_page), el('h3', '', 'Происхождение записи (Provenance)'), el('pre', '', JSON.stringify(data.provenance, null, 2)));
    $('detail-more').hidden = data.next_offset === null;
    if (!$('detail').open)
        $('detail').showModal();
}
catch (e) {
    showError(e);
} }
$('close-detail').onclick = () => { S.detailRequest = null; $('detail').close(); };
$('detail-more').onclick = () => { if (S.detail && S.detail.next !== null)
    openDetail(S.detail.uid, S.detail.next); };
$('detail-link').onclick = async () => { if (!S.detail)
    return; const h = new URLSearchParams({ event: S.detail.uid, ...(S.params.at ? { at: S.params.at } : {}), view: S.params.view || 'event' }); const url = location.origin + '/#' + h; try {
    await navigator.clipboard.writeText(url);
    $('detail-link').textContent = 'Ссылка скопирована';
    setTimeout(() => { $('detail-link').textContent = 'Копировать ссылку'; }, 2000);
}
catch {
    const input = el('input');
    input.readOnly = true;
    input.value = url;
    $('detail-body').append(input);
    input.select();
} };
$('login-form').addEventListener('submit', async (e) => { e.preventDefault(); try {
    await api('login', {}, { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ token: $('token').value }) });
    $('token').value = '';
    $('login-error').textContent = '';
    showWorkspace();
    await refresh(true);
    openHash();
}
catch (e) {
    $('login-error').textContent = e.message;
} });
$('logout').onclick = async () => { try {
    await api('logout', {}, { method: 'POST', credentials: 'same-origin' });
}
catch { } S.es?.close(); S.es = null; showLogin(); };
document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => setTab(b.dataset.tab));
let theme;
try {
    theme = localStorage.getItem('monik-theme');
}
catch { }
document.documentElement.dataset.theme = theme || 'dark';
$('theme').onclick = () => { const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = t; try {
    localStorage.setItem('monik-theme', t);
}
catch { } };
function openHash() { const h = new URLSearchParams(location.hash.slice(1)); if (h.get('at')) {
    if (Number.isNaN(Date.parse(h.get('at')))) {
        showError(Error('Некорректный момент T в ссылке.'));
        return;
    }
    S.params.at = h.get('at');
    S.params.view = h.get('view') === 'knowledge' ? 'knowledge' : 'event';
    S.version++;
    syncFilters();
} if (h.get('event') && /^[a-f0-9]{64}$/.test(h.get('event')))
    openDetail(h.get('event')); }
document.addEventListener('visibilitychange', () => { if (!document.hidden && !S.past && S.follow)
    queueRefresh(); });
window.addEventListener('hashchange', () => { openHash(); refresh(true); });
(async () => { try {
    // Built from labels.ru.json; the existing server API needs no new route.
    S.labels = {
  "pages": {
    "overview": "Обзор",
    "activity": "Активность",
    "tokens": "Токены",
    "limits": "Лимиты",
    "tree": "Агенты",
    "quality": "Качество"
  },
  "events": {
    "message:user": "Владелец",
    "message:assistant": "Ассистент",
    "message:system": "Системное сообщение",
    "message:developer": "Инструкция разработчика",
    "reasoning_summary": "Доступное резюме рассуждений",
    "tool_call": "Вызов инструмента",
    "tool_output": "Результат инструмента",
    "settings": "Настройки",
    "usage": "Токены ответа",
    "limit": "Снимок лимита",
    "cumulative": "Накопительный счётчик",
    "thread": "Ветка",
    "thread_snapshot": "Метаданные ветки",
    "root_binding": "Корневая сессия",
    "task_started": "Задача начата",
    "task_complete": "Задача завершена",
    "turn_aborted": "Задача прервана",
    "turn_started": "Ход начат",
    "turn_completed": "Ход завершён",
    "goal": "Цель",
    "lifecycle_thread": "Жизненный цикл",
    "lifecycle": "Событие жизненного цикла",
    "peer_event": "Sol-link",
    "parse_error": "Ошибка разбора",
    "identity_unknown": "Владелец не определён",
    "source_gap": "Разрыв источника",
    "conflict": "Расхождение источников",
    "compacted": "Сжатие контекста",
    "context_window": "Размер контекста",
    "oversized_record": "Превышен размер записи",
    "unsupported_usage": "Нераспознанный учёт токенов",
    "agent_message": "Сообщение агента",
    "user_message": "Сообщение владельца",
    "warning": "Предупреждение",
    "error": "Ошибка"
  },
  "states": {
    "unknown": "Статус неизвестен",
    "last_task_started": "Последняя задача начата",
    "last_task_complete": "Последняя задача завершена",
    "last_turn_aborted": "Последняя задача прервана",
    "CLOSED": "Закрыт",
    "CANCELLED": "Отменён",
    "ACTIVE": "Активен по записи",
    "SPAWNED": "Создан по записи",
    "CLEANUP_CONFIRMED": "Очистка подтверждена",
    "watching": "Наблюдается",
    "mapped": "Сопоставлен",
    "missing": "Источник отсутствует",
    "error": "Ошибка",
    "conflict": "Конфликт идентичности",
    "partial_line": "Незавершённая строка",
    "catching_up": "Догоняем историю",
    "replaced": "Файл заменён",
    "gap": "Разрыв"
  },
  "windows": {
    "primary": "Основное окно (primary)",
    "secondary": "Дополнительное окно (secondary)",
    "unavailable": "Нет данных об окне"
  },
  "errors": {
    "Filter too long": "Слишком длинный фильтр.",
    "Use an ISO timestamp with timezone": "Укажи дату ISO 8601 с часовым поясом, например 2026-09-10T12:00:00+03:00.",
    "Invalid integer": "Ожидается целое число.",
    "Integer out of bounds": "Число за пределами допустимого диапазона.",
    "Choose before or after": "Укажи только одно направление листания.",
    "Invalid time range": "Начало периода не может быть позже конца.",
    "Unsupported view/search mode": "Неизвестный режим истории или поиска.",
    "Owner authentication required": "Требуется вход владельца.",
    "Same-origin login required": "Вход разрешён только со страницы этого сервера.",
    "Same-origin request required": "Запрос разрешён только со страницы этого сервера.",
    "Invalid owner token": "Неверный токен monik.",
    "Expected owner token": "Введи токен доступа monik.",
    "Login body too large": "Слишком большой запрос входа.",
    "Too many login attempts; retry in one minute": "Слишком много попыток входа. Повтори через минуту.",
    "Too many active sessions": "Достигнут предел активных сеансов входа.",
    "Read-only service": "monik работает только в режиме наблюдения.",
    "Request too large": "Слишком большой запрос.",
    "Observer storage unavailable; sources are unaffected": "Хранилище monik недоступно. Источники Codex не изменялись этим запросом.",
    "Invalid event identifier": "Некорректный идентификатор события.",
    "No event in this time view": "Событие не найдено в выбранном историческом срезе.",
    "Invalid offset": "Некорректная позиция страницы данных.",
    "Too many live browser streams": "Достигнут предел одновременных LIVE-подключений.",
    "Invalid event cursor": "Некорректный курсор событий.",
    "Pagination cursor no longer exists; refresh the timeline": "Курсор больше не существует. Обнови ленту."
  }
};
    Object.assign(kinds, S.labels.events);
    Object.assign(states, S.labels.states);
    const o = await api('overview');
    S.cursor = o.cursor;
    showWorkspace();
    openHash();
    await refresh(true);
}
catch {
    showLogin();
} })();
function syncProfiles(profiles) { const selected = S.params.profile || ''; const select = $('profile'); if (select.options.length === profiles.length + 1 && profiles.every(p => Array.from(select.options).some(o => o.value === p.profile)))
    return; select.replaceChildren(); const all = el('option', '', 'Все профили'); all.value = ''; select.append(all); for (const p of profiles) {
    const o = el('option', '', names[p.profile] || p.profile);
    o.value = p.profile;
    select.append(o);
} select.value = selected; }
$('pause').onclick = () => { S.paused = !S.paused; $('pause').textContent = S.paused ? 'Продолжить обновление' : 'Приостановить экран'; $('pause').setAttribute('aria-pressed', String(S.paused)); $('pause-note').hidden = !S.paused; if (!S.paused)
    queueRefresh(true); };
$('refresh').onclick = () => refresh(true);
$('detail').addEventListener('close', () => { S.detailRequest = null; if (canRefresh() && S.pending)
    queueRefresh(true); });
for (const b of document.querySelectorAll('[data-hours]'))
    b.onclick = () => { const h = Number(b.dataset.hours); if (h)
        S.params.since = new Date(Date.now() - h * 3600000).toISOString();
    else
        delete S.params.since; S.version++; S.before = null; S.history = []; syncFilters(); refresh(true); };
document.addEventListener('keydown', e => { if (!S.authed || e.ctrlKey || e.altKey || e.metaKey || e.target.matches('input,textarea,select') || $('detail').open)
    return; if (e.key === '/') {
    e.preventDefault();
    $('filter-sheet').open = true;
    $('q').focus();
} });
