# Core Background Workers (CQDS)

Документ описывает фоновые воркеры ядра (`core_maint_loop`), очередь maint-пула и рабочие паттерны использования через MCP в чате.

## 1) Что это и зачем

Фоновый контур ядра решает две задачи:

- поддержка ссылок `attached_files` и ленивого reconcile по активным проектам;
- фоновый `code_index` без удержания длинного синхронного HTTP.

Базовая идея: чат и MCP не блокируются на тяжелых шагах, а получают статус и готовый результат из кеша.

## 2) Основные компоненты

- `agent/scripts/core_maint_loop.py` - оркестратор и воркеры.
- `agent/lib/maint_pool.py` - очередь задач (`maint_pool_jobs`), lease, progress.
- `agent/lib/maint_code_index_job.py` - выполнение `code_index` внутри воркера.
- `agent/lib/core_status_snapshot.py` - снимок для `GET /api/core/status`.
- `agent/routes/project_routes.py` - `POST /project/maint_enqueue`, `GET /project/code_index`.
- `agent/routes/core_routes.py` - `/core/background_tasks`, `/core/status`.

## 3) Модель выполнения

### 3.1 Режимы процесса maint

- `CORE_MAINT_POOL_WORKERS=1` - одиночный цикл.
- `CORE_MAINT_POOL_WORKERS>1` - оркестратор + подпроцессы-воркеры.
- воркер без job спит (`CORE_MAINT_POOL_IDLE_SLEEP_SEC`), не делает активную работу.

### 3.2 Очередь задач maint

Таблица: `maint_pool_jobs`.

Ключевые свойства:

- статусы: `queued -> running -> done|error`;
- lease (`lease_expires_at`) и heartbeat;
- `progress_json` + stdout-строки `MAINT_POOL_PROGRESS ...`;
- уникальный активный слот на проект:
  - уникальный индекс по `project_id` при `status IN ('queued','running')`;
  - одновременно может быть только одна активная задача любого `kind` для проекта.

Поддерживаемые `kind`:

- `reconcile_tick`
- `code_index` (обычно приоритет выше).

## 4) Что делает каждый вид задач

### 4.1 `reconcile_tick`

- выбирает активные проекты (сессии + недавняя активность контекста);
- делает snapshot файлов (`find`);
- сравнивает с `attached_files`;
- при mutate-режиме:
  - деградация `missing_ttl`,
  - восстановление найденных,
  - добавление новых,
  - purge stale-ссылок (если включено);
- при необходимости запускает ленивый `scan_project_files` с cooldown.

### 4.2 `code_index`

Воркер вызывает путь, эквивалентный `GET /project/code_index`, но без пользовательской HTTP-сессии:

- `scan_project_files`;
- сборка индекса;
- запись кеша;
- публикация прогресса (`code_index_scan_*`, `code_index_build_*`);
- в summary возвращаются `last_build_kind`, `rebuild_revision`, `rebuild_duration`.

## 5) Наблюдаемость и диагностика

### 5.1 Глобальный статус

`GET /api/core/status` (в MCP: `cq_help#core_status`) показывает:

- uptime ядра;
- состояние оркестратора пула;
- `active_jobs` с `kind/status/busy_sec/progress/error`;
- агрегаты `jobs_by_status`, `running_jobs_by_kind`.

### 5.2 Progress-каналы

- DB: `maint_pool_jobs.progress_json`;
- stdout воркеров: `MAINT_POOL_PROGRESS { ... }`.

## 6) HTTP контракты для индекса

### 6.1 Запуск фоновой сборки

`POST /api/project/maint_enqueue` с `{"project_id": <id>, "kind": "code_index"}`.

Ответ:

- `enqueue=queued` - задача поставлена;
- `enqueue=duplicate` - у проекта уже есть активная задача в пуле (любого kind, из-за уникального слота).

### 6.2 Чтение результата

`GET /api/project/code_index?project_id=<id>&cache_only=true`:

- возвращает кеш, если доступен;
- добавляет `rebuilt_now=1`, если по проекту все еще идет фоновая пересборка;
- может вернуть только `{"rebuilt_now":1}`, если кеша еще нет;
- 404, если нет кеша и нет активной фоновой работы.

## 7) MCP-плейбук для рабочего чата

Ниже паттерн для синхронного UX без долгого блокирующего запроса.

### 7.1 Рекомендуемый сценарий

1. Запустить фон:
   - `cq_files_ctl#rebuild_index` с `background:true`
   - (под капотом: `maint_enqueue`)
2. Пока индекс обновляется:
   - выполнять побочные задачи (подготовка отчета, анализ логов, draft ответа оркестратору).
3. Poll результата:
   - `cq_files_ctl#rebuild_index` с `cache_only:true`
   - интервал 15-20 сек.
4. Условие готовности:
   - `rebuilt_now` отсутствует (или не равен `1`) и payload индекса валиден.
5. Тайм-аут/деградация:
   - по дедлайну показать промежуточный статус и предложить продолжить в async.

### 7.2 Почему именно так

- снижает длительность блокирующих MCP/HTTP вызовов;
- уменьшает риск тайм-аутов при медленном I/O (тома Docker Desktop for Windows);
- позволяет утилизировать время ожидания полезной вторичной работой.

## 8) Практические замечания по ожиданиям

- инкрементальный сценарий "изменен 1 файл -> code_index" обычно быстрее full rebuild;
- на bind-mount/volume в Docker Desktop возможны широкие хвосты задержек;
- синхронный клиентский тайм-аут лучше держать с запасом (обычно 60-120с), даже если типичное время меньше.

## 9) Минимальный чеклист для модели/оркестратора

- перед enqueue убедиться, что проект выбран корректно;
- использовать `background + cache_only` вместо долгого sync ожидания;
- poll 15-20с;
- в ожидании выполнять побочные задачи;
- проверять `core_status`, если долго висит `rebuilt_now=1`;
- помнить про `duplicate`: это может быть не `code_index`, а другая активная задача этого проекта.
