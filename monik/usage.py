"""Response accounting with temporal conflict exclusion.

Keep original canonical evidence immutable. A conflicting delivery disqualifies
its metric only in views that can see that conflict. No cumulative counters or
provider percentages are included in these sums.
"""
from __future__ import annotations

from .model import FIELDS


class UsageQueries:
    def usage(self, params):
        # Text/kind filters belong to the timeline, not unrelated usage payloads.
        selection = {k: v for k, v in params.items() if k not in ('q', 'kind', 'search_mode')}
        where, values = self.filters(selection, include_kind=False)
        conflict_clock = 'ingested_at' if params.get('view') == 'knowledge' else 'time'
        conflict_where = "c.kind='conflict' AND json_extract(c.data,'$.canonical_uid')=e.uid"
        conflict_values = []
        for name in ('at', 'until'):
            if params.get(name) is not None:
                conflict_where += f' AND c.{conflict_clock}<=?'
                conflict_values.append(params[name])
        # Materialization avoids repeating the indexed conflict lookup for every
        # subtotal. Both queries share one read transaction in Store.connect().
        cte = f'''WITH measured AS MATERIALIZED (
            SELECT u.*, e.model,
                   EXISTS(SELECT 1 FROM events c WHERE {conflict_where}) AS disputed
            FROM response_usage u JOIN events e ON e.id=u.event_id WHERE {where}
        ) '''
        bindings = (*conflict_values, *values)
        columns = FIELDS + ('uncached_input_tokens',)
        sums = ','.join(
            f'sum(CASE WHEN valid AND NOT disputed THEN {key} END) AS {key},'
            f'count(CASE WHEN valid AND NOT disputed THEN {key} END) AS {key}_measured'
            for key in columns
        )
        with self.connect() as db:
            result = dict(db.execute(cte + f'''SELECT count(*) AS responses,
                coalesce(sum(NOT valid),0) AS invalid,
                coalesce(sum(disputed),0) AS conflicts, {sums} FROM measured''', bindings).fetchone())
            groups = [dict(row) for row in db.execute(cte + '''
                SELECT profile,thread_id,model,count(*) AS responses,
                    sum(disputed) AS conflicts,
                    sum(CASE WHEN valid AND NOT disputed THEN total_tokens END) AS total_tokens
                FROM measured GROUP BY profile,thread_id,model
                ORDER BY total_tokens DESC,profile,thread_id,model LIMIT 2001''', bindings)]
        result['cache_hit_ratio'] = (
            result['cached_input_tokens'] / result['input_tokens']
            if result['input_tokens'] and result['cached_input_tokens_measured'] == result['input_tokens_measured']
            else None
        )
        result.update(
            groups=groups[:2000], groups_capped=len(groups) > 2000,
            coverage='partial',
            formula='total=input+output; cached is within input; reasoning is within output; disputed measurements excluded',
            dedup_key='profile,thread_id,response_id',
            filter_scope='profile,thread_id,model,time; q and kind apply only to the timeline',
        )
        return result
