# Приёмка v0.1.2

## Проверенный состав

Релиз сохраняет read-only архитектуру: нет Codex RPC, model calls, команд управления сессиями или записи в источники. Config и собственная SQLite schema остаются v1. Production `serve` запускается обычным пользователем и требует Linux Landlock ABI не ниже 3.

В v0.1.2 закрыты результаты производственного аудита:

- текущие лимиты выбираются из одной новейшей атомарной `token_count`-доставки профиля, поэтому прежний provider limit ID больше не выглядит текущим;
- knowledge-time ограничивает не только событие, но и provenance выбранного снимка;
- график показывает локальный индекс аномалии 0–100, confidence и отдельные значения Astra/Sol;
- проверка conflict использует индекс canonical UID и не замедляет live-график полным перебором conflict-событий;
- новые `CommandExecution` не дублируют bulk output, а `monik compact` уплотняет старую собственную БД пакетно, без удаления событий/provenance;
- HTTPS LAN на высоком порту работает через owner user-service без sudo; низкий порт сохраняет узкую service capability;
- установщик сохраняет TLS LAN-конфигурацию при обновлении и проверяет уже поднятый сервис штатным клиентом.

Подробный разбор: `PRODUCTION_AUDIT_20260910.md`.

## Автоматические проверки

Проверки выполнены 10 сентября 2026 обычным пользователем на Linux, Python 3.14.4, SQLite 3.46.1 и Landlock ABI 8. Тестовые источники синтетические или обезличенные.

| Проверка | Результат |
|---|---:|
| Полный `unittest` | 159 PASS, 0 FAIL, 0 SKIP |
| Контрольные фикстуры | 11/11 PASS |
| Настоящий loopback HTTP/SSE | 23/23 PASS |
| Native Chromium: основной web | 45/45 PASS |
| Token graph: unit/API | 23/23 PASS |
| Token graph: HTTP/DOM/адаптивность/индекс | 43/43 PASS |
| Token graph: native login/API/SSE | PASS |
| Wheel из другого cwd и package-data | PASS |
| Установленный wheel: production Landlock, auth/API/graph и TUI `--once` (6 страниц) | PASS |
| Действующая HTTPS LAN-служба и повторное обновление с сохранением TLS | PASS |

Проверенный wheel `codex_monik-0.1.2-py3-none-any.whl`: SHA-256 `567da56b97bd9892eb26a1e7b77009c3d4418e8ffe94b227398b5235ee6f1e81`. Wheel не хранится в Git; хеш относится к воспроизводимому локальному прогону этого состава.

Native Chromium проверил cookie-вход, профили, logout/login, настоящий EventSource, обновление по commit, потерю связи, 401 без старого private DOM, ширины 360/390/412/768/1440, обе темы и отсутствие внешних запросов. График дополнительно проверяет шесть периодов, метрики, null-разрывы, реальные нули, half-open границы, partial buckets, конфликт/late backfill, паузу, инспектор, таблицу, индекс 0–100, profile breakdown и очистку индекса после 401.

На действующей большой собственной БД один уже инициализированный запрос графика измерен примерно в 0,4 секунды. Холодный старт Store дополнительно выполняет `quick_check` и занимает около двух секунд в этой среде. Это локальные измерения, не SLA.

На целевой машине подтверждены единственная owner user-служба, точный private-IP bind на непривилегированном порту, IP SAN, проверка локальным CA, авторизованные limits/usage-series, TUI и сохранение HTTPS-конфигурации после повторного запуска установщика. Firewall самой VM отключён; достижимость с физического клиента зависит также от режима сети VM.

## Воспроизведение

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/fixture_validator.py tests
python tests/wire_acceptance.py --output /tmp/monik-wire.json
python tests/browser_acceptance.py --chromium /path/to/chromium --output /tmp/monik-browser
python tests/graph_browser_acceptance.py --chromium /path/to/chromium --output /tmp/monik-graph
```

## Границы результата

Физический телефон, его trust store, реальная смена Wi-Fi и внешний firewall требуют отдельного клиентского устройства и не доказываются процессом внутри VM. Monik показывает частичную локальную историю: скрытые рассуждения, streaming-дельты, provider billing, удалённые до чтения байты и источники вне явной конфигурации недоступны.
