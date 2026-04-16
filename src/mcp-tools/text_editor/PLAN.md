# text_editor — план (итерации)

**Канонический черновик** (JSON-first MCP, todos, диаграммы): [P:/opt/docker/docs/mcp-text-editor-plan.md](P:/opt/docker/docs/mcp-text-editor-plan.md).

## Интерактив и «голый текст» vs MCP

Спецификация MCP поверх stdio — это **JSON-RPC**: каждый `tools/call` несёт аргументы как **JSON-значения**. Полностью убрать обёртку для тела команды на транспорте MCP **нельзя**, если клиент (Cursor и т.п.) остаётся совместимым с протоколом: многострочный ввод всё равно окажется внутри JSON-строки с экранированием `\n` и `"`.

Практичные варианты, совместимые с идеей «минимум слэшей и кавычек»:

1. **Структурированный JSON** — `op` + `op_args` (+ `expected_revision` для мутаций); см. черновик в `docs/`.
2. **`payload_b64` / `*_path`** — для патчей и длинных тел внутри того же JSON-контракта.
3. **Отдельный процесс stdio REPL** — «голый» ввод; опционально XML-like для паритета с CQDS, **не** обязательный путь MCP.

## Отдельный MCP-сервер (предложение принято)

Вынести в **отдельный entrypoint** (отдельный `Server` в `mcp-tools/`), не смешивать с `cqds_mcp_mini`:

- **Инициализирующий инструмент** — JSON: путь, кодировка, viewport и т.д.
- **Команда редактирования** — JSON: `session_id`, `expected_revision`, `op`, `op_args`, флаги; для тяжёлых полей — `*_b64` / `*_path`.

Тяжёлые вставки не тащить в одну JSON-строку без b64/path.

## XML-like при обязательном JSON: зачем и когда

Внешний слой MCP всё равно **JSON-RPC**, поэтому «чистый XML без JSON» для вызовов из Cursor **недостижим**. Внутренняя XML-подобная строка — это **ещё один синтаксис внутри JSON-строки**, а не замена JSON.

**Токены**: для типичных операций (`get_view`, `move_cursor`, `undo`) плоский объект вида `{"op":"…", "session_id":…, "expected_revision":…}` обычно **короче**, чем развёрнутый тег `<te …/>` с теми же полями (лишние угловые скобки, имя тега, повтор имён атрибутов). Выигрыш по токенам у XML внутри MCP появляется **редко**; для тяжёлых тел выигрывают **`payload_b64` / `payload_path`**, а не переключение XML↔JSON.

**Сбои**: модели чаще ломают **незакрытые теги** и кавычки в атрибутах, чем ключи JSON при узкой схеме. Строгий JSON Schema + дискретные поля даёт **более предсказуемые** ошибки и проще автогенерацию вызовов.

**Где XML-like всё же полезен** (не ради экономии токенов на MCP, а ради **единого текста** в разных каналах):

1. **stdio REPL / удалённый адаптер** — без JSON-обёртки в REPL; тот же разбор, что и для необязательного поля «как в CQDS».
2. **Паритет с процессорами чата CQDS** — если блоки уже в формате `BlockProcessor`, можно подавать ту же строку в адаптер без переписывания в JSON.
3. **Опциональное поле** `cmd_xml` (или только REPL): вторичный вход; **основной** контракт MCP — **структурированный JSON** (`op` + типизированные поля).

Итого: для MCP — **только JSON-op** в стадии 1; XML-like — **адаптер REPL** при необходимости.

## Связь с ядром

- Router и плагины принимают **нормализованную структуру** (dict); парсеры XML/DSL — на границе транспорта.
- Два транспорта к одному ядру: **MCP** (JSON, опционально b64/path) и **stdio REPL** (голый текст, в т.ч. XML-like).

## Профили редактирования (YAML)

Декларативные профили: **расширение файла** → **`syntax_check`** (команда + timeout), **`indent`**: `strict` / `soft` / `off`, опционально **`format`**. При **`session_open`**: явный **`profile_id`** или **авто** по расширению (`profile_auto`, порядок в каталоге), иначе `default` / встроенный `plain`. Загрузка только из доверенных путей (`text_editor/profiles/`, env `TEXT_EDITOR_PROFILES_DIR`); команды из YAML — с allowlist и плейсхолдером `{path}`. Полная спецификация и пример — в [каноническом плане](P:/opt/docker/docs/mcp-text-editor-plan.md).

## Режим вывода (`response_mode`)

Канон: **`response_mode`** / **`response_mode_default`**; синоним входа **`response_as`**. Режимы: **`viewport`**, **`numbered_lines`** (wrapped-view: `line_num -> string|string[]`), **`full_diff`**, **`changed_lines`**, **`minimal`**. Для любой модификации обязательны поля ответа **`current_revision`** и **`previous_revision`** (для `undo` допустим алиас `redo_revision`). Для защиты модели от «утопления» в выводе: консервативные дефолтные лимиты (`default_max_view_lines`, `hard_max_view_lines`, `truncated=true` при усечении). Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Для снижения лишних help-вызовов: `session_open` принимает входной `capabilities_hint` (CSV, например `navigation,replace,patch`) и возвращает `capabilities_guide` по выбранным темам; тот же параметр использовать в `help`-контракте.

Для защиты от «забывчивого» повторного старта: `session_open` (reopen) может возвращать `recent_ops` — краткий журнал 3-5 последних операций (`revision/op/changed_lines/ts`) без тяжёлых diff.

## Хранилище (кратко)

**registry.sqlite** — реестр сессий; **отдельный `<session_id>.sqlite` на сессию** — cleanup удалением файла. Путь данных по умолчанию: `~/.mcp_text_editor` (или эквивалент ОС), переопределяется `TEXT_EDITOR_DATA_DIR`. `session_id` — детерминированный `md5(canonical_path_utf8)` (один файл = одна сессия во всех оболочках), в реестре UNIQUE по `source_path_hash`. MD5 используется только как компактный идентификатор (не как криптографическая защита). В сессии: **`text_lines`** (`idx`, `idx=0` = пустая строка), **`revision_history`** (`seq`, `revision`, `line_num`, `deleted_idx`, `added_idx`, `flags`; отрицательные idx = пропуск delete/add), плюс **`current_revision`** и **`previous_revision`** (активные `line_num -> line_idx` для быстрого одношагового undo/redo), опционально **`session_meta`**. `flags` в `revision_history`: младшие 16 бит — строковые признаки (`LINE_EDITED=0x0001`), старшие 16 бит — ревизионные (`LINT_SUCCESS=0x10000`, `SAVED_TO_DISK=0x20000`). Авто-cleanup: удалять stale-сессии после 30 дней без записи. Фаза 2: retention истории (хвост 100-200 ревизий), **`base_revision`** как линейная таблица `line_num -> line_idx` + `base_revision_meta`, а также **`revision_meta`** для характеристик ревизий. Текущее состояние — инкрементальный кэш + пересборка по журналу для произвольной ревизии; linked-list строк рассматривать как фазу 2. Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

## Стадия 1 (без CQDS API)

Локальные пути хоста, SQLite, revision/lock, окно и шапка — без изменений; только точка входа MCP — отдельный сервер, как выше.

Принято по конкуренции SQLite (фаза 1): **Вариант A** — WAL, `busy_timeout`, mutex на `session_id` (одна write-транзакция на сессию), optimistic lock по `expected_revision`, атомарный commit (обновление `current/previous` + `revision_history` + мета). Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Принято по `session_id` (фаза 1): **Вариант A** — hash от canonical full path + UNIQUE по `source_path_hash`. Учтены внешние изменения файла (редактирование вне сессии): хранить source-метаданные в `session_meta`, детектировать drift и делать `external_sync` (или требовать `force_sync`). Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Принято по DDL/инвариантам (фаза 1): **Вариант A** — strict schema first (PK/FK/CHECK/UNIQUE + индексы + `schema_version`/миграции), приоритет надёжности и простоты над максимальной производительностью; уточнение по ссылкам: `current/previous.line_idx` всегда валиден, а `revision_history.deleted_idx/added_idx` допускают sentinel `<0` и валидируются как ссылки только при `>=0`. Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Принято по ошибкам API (фаза 1): **Вариант A** с полями **`class` + `code`** (не вместо, а вместе). `class` для маршрутизации обработчиков, `code` для точного кейса; также `retryable`, `hint`, `details`. Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Принято по безопасности файлов (фаза 1): **Вариант A**, но с editor-first perimeter. Приоритет ограничений редактора (allowlist) + локальная read-only policy инструмента (рядом с БД), без опоры на «хакаемые» env как основной security-канал. Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Принято по undo/redo (фаза 1): гибрид — `undo` без параметров = один шаг, `undo(target_revision)` = глубокий откат; redo ограниченный (минимум одношаговый), после новой мутации ветка redo обрезается. Одна операция = одна ревизия; массовость регулируется лимитом замен, а не множеством ревизий. Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Принято по тестированию (фаза 1): интеграционный тест-план по группам A-I (жизненный цикл, canonical path, конкуренция, безопасность, source drift, инварианты ревизий, undo/redo, compaction readiness, error-contract), включая контракт `search_indexed.first_revision` для контент-поиска. Для готовности фазы 1 обязательны группы A-G в CI. Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).

Добавлен финальный **Implementation Checklist (Phase 1)** (20 шагов, от server/schema до CI/DoD) — см. канонический план: [P:/opt/docker/docs/mcp-text-editor-plan.md](P:/opt/docker/docs/mcp-text-editor-plan.md).
В конец фазы 1 добавлен обязательный telemetry-review по токенам (утилизация, доля обвязки, вывод о необходимости лаконизации ключей).

## Удалённый доступ (фаза 2)

Вместо SSH: мини-сервер **FastAPI** в контейнере с локальным сетевым доступом, который проксирует HTTP JSON в тот же `router`/плагины.

Добавлен отдельный **Implementation Checklist (Phase 2)** в каноническом плане: выделение transport-adapter, FastAPI endpoints `/session/open` и `/session/cmd`, auth/ACL, rate limits, parity error-contract/storage/session-id, container hardening, Phase 2 integration tests и DoD.
Фаза 2 дополнена шаблонизацией команд: `template_name`/`based_on` + `overrides` (включать по итогам token telemetry фазы 1).

Фаза 2 дополнена операцией вставки из другого файла (`insert_from_file`) с режимами `whole` / `fragment` / `template` (с token replacements) и двумя типами target: `insert_at_line` или `replace_range` для блочной замены.

Также в фазу 2 добавлен обратный инструмент `export_slice`: выгрузка диапазона в отдельный файл (`target_mode`: create/append/overwrite), опционально `delete_source=true` для атомарного move-сценария при рефакторинге.

## Фаза 3 (roadmap): `Sandwich-Pack`

Интеграция с `Sandwich-Pack` для индекса кода и операций на уровне сущностей. Формализованные контракты: **`list_entities`** (список сущностей с координатами), **`select_entity`** (курсор в начало определения), **`replace_entity_block`** (полная замена кода определения, с `expected_revision`). Все entity-операции проецируются в текущий механизм ревизий. Подробности — [канонический план](P:/opt/docker/docs/mcp-text-editor-plan.md).
