"""Bounded time series over monik's own canonical response records.

No source reads, model calls or provider-limit arithmetic. Timestamps on the X
axis are source event times; evidence is bounded by the observation horizon.
Empty buckets are unknown, not proof that Codex consumed zero tokens.
"""
from __future__ import annotations

import bisect
import math
import statistics
import time
from typing import Any

from .model import FIELDS

WINDOWS = (24, 12, 6, 3, 2, 1)
BUCKET_SECONDS = {1: 60, 2: 120, 3: 180, 6: 300, 12: 600, 24: 900}
METRICS = FIELDS + ('uncached_input_tokens',)
MAX_TIMESTAMP = 32_503_680_000
ANOMALY_BUCKET_SECONDS = 15 * 60
ANOMALY_BASELINE_BUCKETS = 24


def _anomaly_summary(label: str, buckets: dict[int, dict]) -> dict[str, Any]:
    """Score recent confirmed token rate against this installation's own baseline.

    The index is deliberately local and relative. It does not infer billing or
    convert provider percentages into tokens. Missing buckets do not become zero.
    """
    baseline=[]
    for slot in range(ANOMALY_BASELINE_BUCKETS):
        row=buckets.get(slot)
        if row and row['accepted'] and row['total_tokens'] is not None:
            baseline.append(row['total_tokens']/15)
    recent=buckets.get(ANOMALY_BASELINE_BUCKETS)
    recent_tokens=recent['total_tokens'] if recent and recent['accepted'] and recent['total_tokens'] is not None else None
    recent_rate=recent_tokens/15 if recent_tokens is not None else None
    reference=statistics.median(baseline) if baseline else None
    confidence='unknown';score=None;ratio=None
    if recent_tokens==0:
        score=0;ratio=0 if reference is not None else None
    elif recent_tokens is not None and len(baseline)>=4:
        ratio=(recent_rate/reference) if reference else None
        raw=100 if reference==0 else (25*ratio if ratio<=1 else 25+37.5*math.log2(ratio))
        if len(baseline)>=16 and recent['accepted']>=3: confidence='high';cap=100
        elif len(baseline)>=8: confidence='medium';cap=79
        else: confidence='low';cap=49
        score=max(0,min(cap,round(raw)))
    if score is not None and confidence=='unknown':
        if len(baseline)>=16 and recent and recent['accepted']>=3: confidence='high'
        elif len(baseline)>=8 and recent and recent['accepted']>=1: confidence='medium'
        elif len(baseline)>=4 and recent and recent['accepted']>=1: confidence='low'
    level='unknown' if score is None else 'green' if score<35 else 'yellow' if score<70 else 'red'
    return {'label':label,'score':score,'level':level,'confidence':confidence,
            'recent_tokens':recent_tokens,'recent_rate_per_minute':recent_rate,
            'baseline_rate_per_minute':reference,'ratio':round(ratio,3) if ratio is not None else None,
            'recent_responses':recent['accepted'] if recent else 0,
            'measured_baseline_buckets':len(baseline),
            'invalid_recent':recent['invalid'] if recent else 0,
            'conflicts_recent':recent['conflicts'] if recent else 0}


def _anomaly(rows, definitions: list[dict]) -> dict[str, Any]:
    names=[p['name'] for p in definitions]
    by_profile={name:{} for name in names};combined={}
    for source in rows:
        row=dict(source);profile=row.pop('profile');slot=row.pop('slot')
        by_profile[profile][slot]=row
        target=combined.setdefault(slot,{'responses':0,'accepted':0,'invalid':0,'conflicts':0,'total_tokens':None})
        for key in ('responses','accepted','invalid','conflicts'): target[key]+=row[key]
        if row['total_tokens'] is not None:
            target['total_tokens']=(target['total_tokens'] or 0)+row['total_tokens']
    profiles=[]
    for definition in definitions:
        profiles.append({'profile':definition['name'],**_anomaly_summary(
            definition.get('label') or {'astra':'Astra','sol':'Sol'}.get(definition['name'],definition['name']),
            by_profile[definition['name']])})
    overall=_anomaly_summary('Все профили',combined)
    overall.update(score_source='combined',score_source_label='Все профили',score_confidence=overall['confidence'])
    alert=max((p for p in profiles if p['score'] is not None),key=lambda p:p['score'],default=None)
    if alert is not None and (overall['score'] is None or alert['score']>overall['score']):
        overall.update(score=alert['score'],level=alert['level'],score_source=alert['profile'],
                       score_source_label=alert['label'],score_confidence=alert['confidence'])
    return {**overall,'profiles':profiles,
            'recent_minutes':15,'baseline_hours':6,'formula_version':1,
            'semantics':'relative confirmed total-token rate; local median of measured 15-minute buckets',
            'score_basis':'maximum alert across the combined pace and configured profiles',
            'thresholds':{'green_max':34,'yellow_max':69,'red_min':70},
            'unknown_policy':'missing or insufficient measurements do not become zero',
            'provider_limit_arithmetic':False}


def _anomaly_index(rows, definitions: list[dict]) -> dict[str, dict[str, list]]:
    """Build bounded prefix sums for rolling anomaly samples.

    Every historical point uses the same fixed 15-minute/6-hour definition as
    the live card.  Prefix sums avoid rescanning six hours of responses for
    every plotted point.
    """
    names=[p['name'] for p in definitions]
    grouped={name:[] for name in names}
    for source in rows:
        row=dict(source)
        if row['profile'] in grouped: grouped[row['profile']].append(row)
    indexed={}
    for name,items in grouped.items():
        items.sort(key=lambda row:row['event_at'])
        values={key:[0] for key in ('accepted','invalid','conflicts','measured','total_tokens')}
        for row in items:
            accepted=int(row['valid']==1 and not row['disputed'])
            measured=int(accepted and row['total_tokens'] is not None)
            increments={'accepted':accepted,'invalid':int(row['valid']==0),
                        'conflicts':int(bool(row['disputed'])),'measured':measured,
                        'total_tokens':row['total_tokens'] if measured else 0}
            for key,value in increments.items(): values[key].append(values[key][-1]+value)
        indexed[name]={'times':[row['event_at'] for row in items],**values}
    return indexed


def _anomaly_window(index: dict[str, list], start: float, end: float) -> dict[str, Any]:
    lo=bisect.bisect_left(index['times'],start);hi=bisect.bisect_left(index['times'],end)
    delta=lambda key:index[key][hi]-index[key][lo]
    measured=delta('measured')
    return {'responses':hi-lo,'accepted':delta('accepted'),'invalid':delta('invalid'),
            'conflicts':delta('conflicts'),'total_tokens':delta('total_tokens') if measured else None}


def _anomaly_at(indexed: dict[str, dict[str, list]], definitions: list[dict], end: float) -> dict[str, Any]:
    rows=[];start=end-(ANOMALY_BASELINE_BUCKETS+1)*ANOMALY_BUCKET_SECONDS
    for definition in definitions:
        profile=definition['name'];index=indexed[profile]
        for slot in range(ANOMALY_BASELINE_BUCKETS+1):
            left=start+slot*ANOMALY_BUCKET_SECONDS
            rows.append({'profile':profile,'slot':slot,
                         **_anomaly_window(index,left,left+ANOMALY_BUCKET_SECONDS)})
    return _anomaly(rows,definitions)


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
          EXISTS(SELECT 1 FROM events c INDEXED BY events_conflict_uid WHERE c.kind='conflict'
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
        anomaly_start=start-(ANOMALY_BASELINE_BUCKETS+1)*ANOMALY_BUCKET_SECONDS
        anomaly_rows=db.execute(f'''SELECT e.profile,e.event_at,u.valid,u.total_tokens,
              EXISTS(SELECT 1 FROM events c INDEXED BY events_conflict_uid WHERE c.kind='conflict'
                AND json_extract(c.data,'$.canonical_uid')=e.uid
                AND c.ingested_at<=?) AS disputed
            FROM events e JOIN response_usage u ON u.event_id=e.id
            WHERE e.event_at>=? AND e.event_at<? AND e.ingested_at<=?
              AND e.profile IN ({marks})
            ORDER BY e.profile,e.event_at''',(end,anomaly_start,end,end,*names)).fetchall()
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
    anomaly_index=_anomaly_index(anomaly_rows,selected)
    anomaly_series=[]
    for i in range(count):
        left,right=max(start,first+i*step),min(end,first+(i+1)*step)
        sample=_anomaly_at(anomaly_index,selected,right)
        anomaly_series.append({'start':left,'end':right,'partial':right-left<step,
            'score':sample['score'],'level':sample['level'],'confidence':sample['score_confidence'],
            'score_source':sample['score_source'],'score_source_label':sample['score_source_label']})
    return {'schema_version': 1, 'hours': hours, 'window_start': start, 'window_end': end,
            'bucket_seconds': step, 'cursor': cursor, 'generated_at': clock,
            'mode': 'historical' if at is not None else 'live', 'series': series,
            'anomaly':_anomaly_at(anomaly_index,selected,end),'anomaly_series':anomaly_series,
            'coverage': 'partial', 'time_basis': 'source_event_time',
            'knowledge_at': end, 'empty_bucket': 'unknown_not_zero',
            'rate_formula': 'bucket_metric * 60 / bucket_seconds; partial buckets not extrapolated',
            'accounting': 'canonical responses; cached within input; reasoning within output; conflicts excluded',
            'scope': 'configured profile including its attributed root and child sessions',
            'unknown_time_basis': 'arrivals within selected window; not placed on source-time axis'}
