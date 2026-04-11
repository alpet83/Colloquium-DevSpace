# CORE optimization: semi-autonomous maintenance loop

## Цель

Снизить влияние фоновой рутины на API-ядро без усложнения архитектуры в отдельный
оркестратор с очередями задач.

## Принятый подход

Запускается отдельный процесс `agent/scripts/core_maint_loop.py`, который в цикле:

1. выбирает активные проекты (hybrid policy);
2. строит snapshot файлов через `find`;
3. сравнивает snapshot с `attached_files` в БД;
4. в `dry-run` только логирует diff, в `mutate` применяет reconcile;
5. по cooldown запускает ленивый `scan_project_files` при наличии расхождений.

В текущей реализации процесс поднимается дочерним из ядра через startup hook
(`CORE_MAINT_CHILD_ENABLED=1`), чтобы в будущем можно было добавить STDIO-коммуникацию.

Поддерживаются два режима работы цикла:

- `inotify` (**по умолчанию**, если `CORE_MAINT_MODE` не задан): пассивное ожидание
  FS-событий через `inotifywait` + страховочный периодический проход, если долго нет событий.
- `active`: регулярный полный проход только по таймеру (явный выбор или fallback после self-test).

При старте в режиме `inotify` выполняется **self-test**. Сначала проверяется, что
`/app/projects` существует и является каталогом: иначе сразу **`active`** (основной цикл
всегда вызывает `inotifywait -r` на этом пути). Отдельная функция пробы для несуществующего
пути трактует его как «пропуск» (`skip_missing`), а не провал inotify — из‑за этого раньше
могло казаться, что тест прошёл при отсутствии дерева проектов.

Далее в корне каждого каталога проекта из БД создаётся скрытый файл `.cqds_maint_probe_*`
и сразу удаляется; `inotifywait -m` должен отловить и CREATE, и DELETE. Дополнительно те же
пробы выполняются для **прямых подкаталогов проекта с другим `st_dev`** (типичный bind-mount).

**Гибрид:** если inotify в целом работает, но пробы провалились только для части
путей (например, смонтированный подкаталог), процесс **остаётся в `inotify`**
(глобальный `inotifywait` не отключается). Для каждого провалившегося каталога
периодически выполняется узкий `find` + reconcile только под этим префиксом
(лог: `kind=poll_failed`). Полный переход в **`active`** делается только если
нет `inotifywait`, нет каталога `/app/projects`, провалена проба на `/app/projects`
при отсутствии проектов в БД, или **провалены корни всех существующих** каталогов проектов.

Отключить self-test (на свой риск): `CORE_MAINT_INOTIFY_SELFTEST=0`.
Ограничить число проектов в self-test: `CORE_MAINT_SELFTEST_MAX_PROJECTS` (по умолчанию 64).

## Hybrid policy выбора активных проектов

- Источник A: `sessions.active_project` (высокий приоритет).
- Источник B: недавняя активность по `context_cache_metrics.project_id` за окно `N` часов.
- Итог: объединение, scoring, top-K за тик.

## Reconcile: `find` vs `attached_files`

- DB-эталон: строки `attached_files` с `file_name LIKE '@%'` по `project_id`.
- FS-эталон: `find <project_dir> -type f`, нормализация в relative POSIX path.
- Дифф:
  - `db_only`: деградация `missing_ttl` и обновление `missing_checked_ts`;
  - `fs_only`: `add_file(..., content=None)` для мягкого добавления ссылок;
  - `both`: восстановление TTL до `FILE_LINK_TTL_MAX` при деградации.

Обновления идемпотентны: повтор тика не приводит к неконсистентности.

## Конфигурация (runtime config / env)

- `CORE_MAINT_ENABLED` — глобальный kill-switch цикла.
- `CORE_MAINT_CHILD_ENABLED` — запуск дочернего maintenance-процесса из `server.py`.
- `CORE_STARTUP_FILE_MAINT_ENABLED` — если **не задан**: полный `scan_project_files` по всем проектам + `FileManager.check()` после старта HTTP **не выполняются**, когда согласно env поднимается дочерний `core_maint_loop` (избежание дублирования нагрузки с maint). Явно `1` — всегда делать стартовый проход; явно `0` — никогда.
- **Новый проект:** `POST /project/create` планирует тот же фоновый проход, что и `POST /project/scan_refresh` (`_run_project_scan_refresh`), чтобы индекс появился без рестарта ядра.
- `CORE_MAINT_MUTATE` — режим применения изменений (иначе dry-run).
- `CORE_MAINT_MODE` — `inotify` (default) или `active`.
- `CORE_MAINT_INOTIFY_SELFTEST` — включить стартовый inotify self-test (default on).
- `CORE_MAINT_SELFTEST_MAX_PROJECTS` — максимум проектов для обхода в self-test.
- `CORE_MAINT_INTERVAL_SEC` — период тика.
- `CORE_MAINT_ACTIVE_HOURS` — окно recent-активности.
- `CORE_MAINT_MAX_PROJECTS_PER_TICK` — ограничение top-K.
- `CORE_MAINT_PROJECT_BUDGET_SEC` — бюджет обработки одного проекта.
- `CORE_MAINT_FIND_TIMEOUT_SEC` — timeout для `find`.
- `CORE_MAINT_FIND_SLOW_LOG_SEC` — порог (сек): если сам вызов `find` (subprocess) длился не меньше, в лог пишется `CORE_MAINT find_slow ...` (по умолчанию 1.0).
- `CORE_MAINT_SCAN_ENABLED` — включить/выключить ленивый scan.
- `CORE_MAINT_SCAN_COOLDOWN_SEC` — минимальный интервал между scan одного проекта.
- `CORE_MAINT_INOTIFY_TIMEOUT_SEC` — таймаут ожидания событий в `inotify`-режиме (аналог `-t` у `inotifywait`; при необходимости выставить 180 и т.д.).
- `CORE_MAINT_INOTIFY_DUMP` — при `1`: накапливать **уникальные** строки stdout/stderr за окно `CORE_MAINT_INOTIFY_DUMP_WINDOW_SEC` (по умолчанию 60 с), затем одним батчем в лог (INFO) + гистограмма `rc` за окно.
- `CORE_MAINT_INOTIFY_FORCE_ACTIVE_SEC` — если событий нет: полный тик по выбранным проектам
  (когда гибридный список пуст); при непустом гибриде — дополнительный poll только провалившихся путей.
- `CORE_MAINT_POLL_FAILED_SEC` — интервал периодического `find`+reconcile по провалившимся путям self-test (по умолчанию 60 с).
- `CORE_MAINT_MCP_HEARTBEAT_TUNE` — при `1` (default): если долго нет inotify по файлу `.cqds_mcp_active.pid` (пишет MCP), сужать интервалы ниже.
- `CORE_MAINT_MCP_HEARTBEAT_STALE_SEC` / `CORE_MAINT_MCP_HEARTBEAT_GRACE_SEC` — порог «нет heartbeat» и пауза после старта maintenance перед проверкой.
- `CORE_MAINT_INOTIFY_FORCE_DEGRADED_SEC` / `CORE_MAINT_POLL_FAILED_DEGRADED_SEC` — подставляются вместо базовых force/poll при «просроченном» heartbeat.

**MCP mini на хосте** (`mcp-tools/cqds_mcp_mini.py`, модули в `mcp-tools/runtime/`, heartbeat в `cq_runtime_host_heartbeat`):
фоновая задача обновляет в корне каждого проекта **`.cqds_mcp_active.pid`** (строка 1: **PID** процесса runtime `os.getpid()`, строка 2: unix time). Хостовые пути **не задаются основным env**: выполняется **`docker inspect`** для контейнера (по умолчанию **`cqds-core`**, как `container_name` при `COMPOSE_PROJECT_NAME=cqds`; переопределение: **`CQDS_MCP_HEARTBEAT_DOCKER_CONTAINER`**), из `Mounts` берутся **bind** с `Destination` `/app/projects` (подкаталоги на хосте = проекты) и/или `/app/projects/<имя>`. Если inspect не дал путей — опциональный fallback **`CQDS_MCP_HEARTBEAT_PROJECTS_DIR`**. Ещё: `CQDS_MCP_PROJECT_HEARTBEAT`, `CQDS_MCP_HEARTBEAT_INTERVAL_SEC`, `CQDS_MCP_HEARTBEAT_INSPECT_CACHE_SEC`. На машине должен быть доступен **`docker`** CLI. Селфтест maint по `.cqds_maint_probe_*` на этот файл **не смотрит**.

## Наблюдаемость

Дочерний `core_maint_loop` запускается с закрытым stdout/stderr: сообщения **только в файлы**
BasicLogger, см. `/app/logs/core_maint.log` (симлинк на файл дня). В `docker logs` ядра их не будет.

Для диагностики inotify: при `LOG_VERBOSITY=DEBUG` при создании/удалении `test.tmp`
под `/app/projects` пишется `#DBG ... CORE_MAINT inotify probe_tmp: ...`. Если событий с хоста нет,
строки не появятся (таймаут `inotifywait` ничего не логирует). `CORE_MAINT_INOTIFY_DUMP=1`: раз в `CORE_MAINT_INOTIFY_DUMP_WINDOW_SEC` — сводка `rc_hist` и **уникальные** строки за минуту (меньше шума от повторяющихся правок одного лог-файла).

Процесс пишет строку `CORE_MAINT ...` на каждый обработанный проект:

- обычный тик: `project_id`, `name`, `mode`, `score`;
- гибридный poll: `kind=poll_failed`, `subtree=...` вместо полного дерева;
- `find_count`, `db_count`, `db_only`, `fs_only`, `both`;
- `degraded`, `recovered`, `added`, `scanned`;
- `elapsed_ms`.
- при медленном `find`: `find_slow` с `scope=project_root|subtree`, `path`, `elapsed_sec`, `find_rc`.

Этого достаточно для первичной оценки эффекта и регрессий по latency.

## Rollout

1. Включить только `CORE_MAINT_ENABLED=1`, оставить `CORE_MAINT_MUTATE=0` (dry-run).
2. Проверить логи diff и бюджеты времени.
3. Включить `CORE_MAINT_MUTATE=1` на ограниченном top-K.
4. Поднять `CORE_MAINT_MAX_PROJECTS_PER_TICK` при стабильной нагрузке.
5. При проблемах: вернуть `CORE_MAINT_MUTATE=0` или `CORE_MAINT_ENABLED=0`.

## Production заметки по хранилищу проектов

- Для bind-mount (особенно WSL2/Docker Desktop) `inotify` может быть ненадежным:
  события create/delete/move могут приходить с задержками или теряться.
- Основные издержки обычно именно в bind-монте `projects` (медленный протокол доступа).
- Для production рекомендуется:
  - держать `projects` в закрытом Docker named volume;
  - при необходимости предоставлять внешний доступ к нему через отдельный канал
    (например, опциональный CIFS/Samba-шаринг), а не прямой bind-mount хоста.

## Почему без multi-worker API

- Сохраняется единый in-memory state ядра (LLM cache и runtime-состояние).
- Нет сложности межпроцессной синхронизации API-воркеров.
- Фоновая нагрузка вынесена из request path отдельным простым процессом.
