# Приёмка v0.1.1

## Проверенный состав

Релиз сохраняет read-only архитектуру: у monik нет Codex RPC, model calls,
команд управления сессиями или записи в источники. Config и собственная SQLite
schema остаются v1. Production `serve` по-прежнему требует Linux Landlock ABI
не ниже 3 и отказывается от небезопасного fallback.

Закрыты обязательные замечания передачи:

- `monik tui` зарегистрирован с `--config`, `--once`, `--page`, `--profile`,
  `--at` и `--ca-file`; curses импортируется только для этой команды;
- численно равные `resets_at` int/float больше не создают ложный conflict,
  при этом provenance сохраняется, а bool и реальные изменения различаются;
- каталог профилей web отделён от отфильтрованного overview и очищается при
  потере авторизации;
- собственные config, token, SQLite sidecar и lock-файлы проверяются на
  symlink, hardlink, владельца и приватные права; config заменяется атомарно
  через уникальный временный файл;
- user-установщик не сосуществует с system-службой, ставит wheel без
  `PYTHONPATH`, делает backup собственной БД и автоматически возвращает
  прежний состав при ошибке;
- отсутствующий cursor ленты возвращает контролируемый HTTP 400;
- запрос графика имеет пятисекундный тайм-аут, а прежние generation/abort
  guards сохранены.

## Результаты 10 сентября 2026

Проверки выполнялись обычным пользователем на Linux, Python 3.14.4,
SQLite 3.46.1, Landlock ABI 8. Все данные тестов синтетические или
обезличенные.

| Проверка | Результат |
|---|---:|
| Полный `unittest` | 147 PASS, 0 FAIL, 0 SKIP |
| Контрольные фикстуры | 11/11 PASS |
| Настоящий loopback HTTP/SSE | 23/23 PASS |
| Native Chromium: основной web | 45/45 PASS |
| Token graph: unit/API/DOM | 40/40 PASS |
| Token graph: native login/API/SSE | PASS |
| Wheel из другого cwd и package-data | PASS |
| Wheel + production Landlock + TUI `--once` (6 страниц) | PASS |
| PTY: resize, `q`, `й`, восстановление termios | PASS |
| Инъекция отказа установщика и rollback | 9/9 PASS |
| Запрет второго user-unit при system-unit | 3/3 PASS |

Wheel `codex_monik-0.1.1-py3-none-any.whl`, проверенный перед публикацией:
SHA-256 `d695728b7c342910f9ecea50e051c51cc333bed5bfed0c2fe24b999d2c90e8fe`.
Собираемый wheel не хранится в Git; хеш относится к этому локальному прогону.

Native Chromium проверил cookie-вход, фильтры all → Sol → refresh → Astra →
all, logout/login, unknown profile, настоящий EventSource, два таба с одним
Collector, потерю связи, имитацию sleep/return, 401 без старого private DOM,
ширины 360/390/412/768/1440, обе темы, hostile text и только локальные
запросы. Наблюдавшийся максимум восьми задержек запись → web был меньше
0,8 секунды; это локальный образец, не SLA.

График проверен для 24/12/6/3/2/1 часа, всех метрик, null-разрывов, нулей,
half-open границ, partial buckets, конфликтов, late backfill, профилей и
дочерних веток, LIVE/паузы, таблицы, инспектора, тем и адаптивных ширин.

## Воспроизведение

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/fixture_validator.py tests
python tests/wire_acceptance.py --output /tmp/monik-wire.json
python tests/browser_acceptance.py --chromium /path/to/chromium --output /tmp/monik-browser
python tests/graph_browser_acceptance.py --chromium /path/to/chromium --output /tmp/monik-graph
```

## Границы результата

Физический Android, выпуск локального CA, firewall и LAN/TLS не проверялись:
они требуют конкретного клиентского устройства и отдельного сетевого решения.
Loopback и SSH-туннель остаются безопасным рабочим вариантом. Покрытие данных
всегда частичное: monik не видит скрытые рассуждения, streaming-дельты,
биллинг, удалённые до чтения байты и источники вне явной конфигурации.
