"""Bounded time series over monik's own canonical response records.

No source reads, model calls or provider-limit arithmetic. Timestamps on the X
axis are source event times; evidence is bounded by the observation horizon.
Empty buckets are unknown, not proof that Codex consumed zero tokens.
"""
from __future__ import annotations

import math
import time
from typing import Any

from .model import FIELDS

WINDOWS = (24, 12, 6, 3, 2, 1)
BUCKET_SECONDS = {1: 60, 2: 120, 3: 180, 6: 300, 12: 600, 24: 900}
METRICS = FIELDS + ('uncached_input_tokens',)
MAX_TIMESTAMP = 32_503_680_000


def usage_series(store, profiles: list[dict], *, hours: int = 6,
                 profile: str | None = None, at: float | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """Return a single consistent read snapshot for the half-open interval [a,b).

    A fixed `at` is also a knowledge horizon: later backfill/conflicts are not
    visible. Live requests use the server clock. Values without source times
    are excluded, with an explicit count by arrival time in this interval.
    """
    if type(hours) is not int or hours not in WINDOWS:
        raise ValueError('Допустимые интервалы: 24, 12, 6, 3, 2 или 1 час.')
    clock = time.time() if now is None else now
    if type(clock) not in (float, int) or not math.isfinite(clock):
        raise ValueError('Некорректное время сервера.')
    end = clock if at is None else at
    if (type(end) not in (float, int) or not math.isfinite(end)
            or not hours * 3600 <= end <= min(clock, MAX_TIMESTAMP)):
        raise ValueError('Момент графика должен быть в прошлом и в допустимом диапазоне.')
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 16:
        raise ValueError('Нужен список из 1–16 профилей.')
    names = [p['name'] for p in profiles]
    if profile is not None and profile != '' and profile not in names:
        raise ValueError('Профиль отсутствует в конфигурации monik.')
    selected = [p for p in profiles if not profile or p['name'] == profile]
    names = [p['name'] for p in selected]
    start = end - hours * 3600
    step = BUCKET_SECONDS[hours]
    first = math.floor(start / step) * step
    count = math.ceil((end - first) / step)
    assert count <= 98  # Hard bound on response size, independent of source history.
    marks = ','.join('?' for _ in names)
    valid = 'valid=1 AND disputed=0'
    aggregates = ','.join(
        f'sum(CASE WHEN {valid} THEN {k} END) AS {k},'
        f'count(CASE WHEN {valid} THEN {k} END) AS {k}_measured'
        for k in METRICS
    )
    sql = f'''WITH measured AS MATERIALIZED (
        SELECT e.profile,CAST((e.event_at-?)/? AS INTEGER) AS slot,u.*,
          EXISTS(SELECT 1 FROM events c WHERE c.kind='conflict'
            AND json_extract(c.data,'$.canonical_uid')=e.uid
            AND c.ingested_at<=?) AS disputed
        FROM response_usage u JOIN events e ON e.id=u.event_id
        WHERE e.event_at>=? AND e.event_at<? AND e.ingested_at<=?
          AND e.profile IN ({marks})
    )
    SELECT profile,slot,count(*) AS responses,
        sum(CASE WHEN {valid} THEN 1 ELSE 0 END) AS accepted,
        sum(CASE WHEN valid=0 THEN 1 ELSE 0 END) AS invalid,
        sum(disputed) AS conflicts,{aggregates}
    FROM measured GROUP BY profile,slot ORDER BY profile,slot'''
    with store.connect() as db:
        # Cursor, counts and time series share this snapshot. No per-bucket query.
        cursor = db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
        rows = db.execute(sql, (first, step, end, start, end, end, *names)).fetchall()
        missing = dict(db.execute(f'''SELECT e.profile,count(*)
            FROM response_usage u JOIN events e ON e.id=u.event_id
            WHERE e.event_at IS NULL AND e.ingested_at>=? AND e.ingested_at<?
              AND e.profile IN ({marks}) GROUP BY e.profile''', (start, end, *names)))
    indexed = {(r['profile'], r['slot']): dict(r) for r in rows}
    series = []
    for definition in selected:
        name = definition['name']
        totals = {k: None for k in METRICS}
        measured = {k: 0 for k in METRICS}
        counters = {'responses': 0, 'accepted': 0, 'invalid': 0, 'conflicts': 0}
        points = []
        for i in range(count):
            left, right = max(start, first + i * step), min(end, first + (i + 1) * step)
            point = {'start': left, 'end': right, 'partial': right-left < step,
                     'state': 'no_records', 'values': {k: None for k in METRICS},
                     'measured': {k: 0 for k in METRICS},
                     'responses': 0, 'accepted': 0, 'invalid': 0, 'conflicts': 0}
            row = indexed.get((name, i))
            if row is not None:
                point.update({k: row[k] for k in counters})
                point['state'] = 'measured' if row['accepted'] else 'excluded'
                for k in METRICS:
                    value = row[k]
                    point['values'][k] = value
                    point['measured'][k] = row[k + '_measured']
                    measured[k] += point['measured'][k]
                    if value is not None:
                        totals[k] = (totals[k] or 0) + value
                for k in counters:
                    counters[k] += row[k]
            points.append(point)
        series.append({'profile': name, 'label': definition.get('label') or {'astra': 'Astra', 'sol': 'Sol'}.get(name, name),
                       'points': points, 'totals': totals, 'measured': measured,
                       **counters, 'unknown_source_time': missing.get(name, 0)})
    return {'schema_version': 1, 'hours': hours, 'window_start': start, 'window_end': end,
            'bucket_seconds': step, 'cursor': cursor, 'generated_at': clock,
            'mode': 'historical' if at is not None else 'live', 'series': series,
            'coverage': 'partial', 'time_basis': 'source_event_time',
            'knowledge_at': end, 'empty_bucket': 'unknown_not_zero',
            'rate_formula': 'bucket_metric * 60 / bucket_seconds; partial buckets not extrapolated',
            'accounting': 'canonical responses; cached within input; reasoning within output; conflicts excluded',
            'scope': 'configured profile including its attributed root and child sessions',
            'unknown_time_basis': 'arrivals within selected window; not placed on source-time axis'}
