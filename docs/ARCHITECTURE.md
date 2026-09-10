# Архитектура 0.1

```
allowlisted registry/state/rollout/lifecycle/Sol-link/log DB
                   │ read-only, no RPC
                   ▼
          one Collector per service
    identity → live / recent / deep lanes
                   │ bounded normalization
                   ▼
      private SQLite WAL + FTS5 + provenance
                   │ read projections
                   ▼
       authenticated HTTP API + one SSE hub
                   │ durable IDs, bounded pages
                   ▼
              local Russian web UI
```

## Модули

`config.py` описывает только собственные настройки и запреты scope. `sandbox.py` устанавливает Landlock перед потоками. `collector.py` читает разрешённые источники; registry задаёт root, `threads`/`thread_spawn_edges` определяют reachable graph. Shared inode не создаёт вторую копию коллектора. При конфликте владельца соответствующий источник не относится к профилю. Настроенный `logs_db` читается через `mode=ro` короткими диапазонами ID. Фиксированный SQL возвращает только основной процент, длительность окна, reset и план из response headers; полный `feedback_log_body` не возвращается коду адаптера и не сохраняется. Старый config v1 без этого ключа атомарно дополняется соседним с `state_db` путём; явный `null` остаётся opt-out.

`model.py` нормализует и очищает данные. `storage.py` пишет исключительно собственную SQLite. `queries.py` содержит read-проекции. `app.py` предоставляет фиксированный API, owner authentication и SSE. `web/` не требует Node build или внешних ресурсов. `cli.py` даёт init/doctor/token/roots/backup/demo/serve, но не команды Codex.

## Идентичность и время

Usage: `(profile, original_thread_id, response_id)`. Event UID: хеш version/profile/thread/kind/native ID. Без native ID используется идентичность доставки `(physical inode, source generation, byte offset, kind)`. Поэтому точные повторы без стабильного ID на разных физических файлах не объявляются доказанно одинаковыми. Это ограничение указано явно, текст не используется как heuristic dedupe key.

`event_at` приходит из источника. `observed_at` известен для новых завершённых строк после старта наблюдения; при backfill он null. `ingested_at` фиксирует поступление в monik. В snapshot-источниках state/registry не подменяется историческая дата наблюдения. `time` использует event time при наличии, иначе observer time. Knowledge-view ограничивает ingestion horizon, но упорядочивает сами известные события по их времени.

При конфликте canonical ID первая метрика остаётся неизменной; создаётся conflict event с очищенным incoming evidence. Дополнительная доставка попадает в provenance. Source cursor и соответствующая партия событий коммитятся одной транзакцией собственной БД. Ошибка записи откатывает эту партию, а не «подтверждает» потерянные события.

## Live и история

Live offset стартует на границе последней завершённой строки. Незавершённый хвост остаётся для live. Предсуществующие байты импортируются отдельно: recent lane и deep-backfill lane. Все live lanes обходятся до исторической работы. У idle lanes есть короткий in-memory stat cache; состояние источника всё равно периодически обновляется. Inode/mtime/size/anchors помогают обнаружить замену и in-place truncate, но не восстанавливают удалённые байты.

Один SSE hub опрашивает максимальный durable event ID, а не медленно проигрывает всю историю при рестарте. Каждый клиент догоняет собственный cursor страницами до 200 ID. До 24 одновременных SSE streams. Клиенты не создают коллекторы, не выполняют RPC и не запускают повторный source backfill.

## Числа

`total = input + output`; `uncached = input - cached`; `cache_hit = sum(cached)/sum(input)` только при сопоставимом покрытии. Reasoning и cached являются деталями, а не добавками. Invalid usage исключается из сумм с отдельным счётчиком. Если измерений нет, сумма null, не ноль.

Cumulative требует baseline для каждого counter epoch; повтор имеет нулевую дельту, уменьшение явно отмечается и разрывает дальнейшую цепочку. Эти дельты диагностические, не canonical spend.

Rate limits: owner-facing проекция выбирает последнее `codex/primary` по профилю. При наличии `logs_db` свежие значения берутся из уже сохранённых Codex response headers, привязанных к доказанному `thread_id`; это пассивное чтение, без `/status`, provider-запроса или app-server RPC. Дополнительные модельные квоты остаются сохранёнными событиями и не смешиваются с основным лимитом. Основное число `remaining=100-used`, только если 0 <= used <= 100. Старый снимок явно помечается как последнее известное значение. Изменения windows, reset time и немонотонность видимы; причина неизвестна. Счётчика «сумма падений процента» нет.

## Границы и дальнейшие работы

Поддержка схемы источника ограничена перечисленными адаптерами; неизвестное сохраняется безопасно или даёт unavailable, не догадку. Нужны отдельные targeted adapters для thread-history, handoff bodies, richer goal/queue state. Не добавлять app-server без отдельного нейтрального live-доказательства и разрешения владельца.

Глобальная retention не включена; перед добавлением надо согласовать сохранение provenance, cursor/gaps, snapshots и FTS consistency. Количество групп/исторических рядов ограничено и отражается флагами capped; для многолетней истории дальнейшая работа должна идти через проверенные incremental projections, а не увеличение timeouts без измерений.

Нельзя превращать монитор в scheduler/model_changer/dispatcher: управление TUI и оптимизация reasoning/parallelism вне этой архитектуры.
