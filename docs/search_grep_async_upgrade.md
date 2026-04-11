# Апгрейд поиска по проекту: stateless-чанки, кэш подкаталогов, MCP `cq_start_grep` / `cq_fetch_result`

Документ фиксирует целевую архитектуру, контракты API и MCP, и чеклист внедрения. Закрывать работу — по секции **«Чеклист (TODO по шагам)»**.

---

## 1. Цели и ограничения

### 1.1 Цели

- Не удерживать один HTTP-запрос до конца полного grep по крупному проекту (таймауты nginx/клиента).
- Отдавать результаты **порциями**; прогресс поиска **не хранить на сервере в привязке к сессии** (нет долгоживущих job в RAM на пользователя).
- Сузить пространство обхода через **подкаталог** (`path_prefix`).
- На сервере безопасно **шарить между пользователями** только производные структуры: **префикс пути → упорядоченный список `file_id`**, обновляемый при рескане файлов.
- MCP: переименование инструментов **без депрекации**: `cq_smart_grep` → **`cq_start_grep`**, `cq_extra_result` → **`cq_fetch_result`** (обновить документацию и конфиги агентов).

### 1.2 Не в рамках первой итерации (при необходимости — отдельные задачи)

- Полнотекстовый индекс вне БД файлов.
- Инкрементальное обновление карты префиксов без полного рескана (сейчас — пересбор при завершённом `scan_project_files` для проекта).

### 1.3 Принципы

| Принцип | Реализация |
|--------|------------|
| Stateless чанки | Клиент передаёт `offset`, `index_epoch`, те же параметры запроса; сервер не хранит курсор между вызовами. |
| Общий кэш | ` (project_id, path_prefix_norm, index_epoch) → [file_id…] ` в памяти процесса с LRU/лимитом ключей. |
| Истечение индекса | При рескане проекта увеличивается **`index_epoch`** (per project); старые запросы с меньшим epoch получают ошибку «перезапустите поиск». |
| TTL 30 мин для «готовых» результатов | На стороне **MCP/клиента** (буфер после `scan_complete`), не как TTL серверного job с момента создания. |
| Лимиты VPS | Кэш префиксов: макс. число ключей, макс. суммарный объём id-списков; одна порция ответа — ограничение по `file_ids` и `hits`. |

### 1.4 Статус реализации (ядро + MCP grep, v0.5)

| Компонент | Файлы |
|-----------|--------|
| `index_epoch`, bump | `agent/globals.py` (`project_index_epoch`, `get_project_index_epoch`, `bump_project_index_epoch`); вызов из `ProjectManager.mark_scan_fresh` в `agent/managers/project.py` (каждое завершение scan, в т.ч. **time_limited** — epoch всё равно растёт; клиент перезапускает чанки). |
| LRU + fingerprint префикса/фильтров | `agent/lib/smart_grep_scope_cache.py`; env: `CQDS_SMART_GREP_SCOPE_CACHE_KEYS`, `CQDS_SMART_GREP_SCOPE_MAX_IDS`. |
| Чанк + index_meta + фильтры | `agent/routes/project_routes.py` (`_filter_entries_for_smart_grep`, `_grep_hits_one_file`, `GET /project/{id}/index_meta`, `POST /project/smart_grep/chunk`); env: `CQDS_SMART_GREP_CHUNK_LIMIT_FILES`, `CQDS_SMART_GREP_CHUNK_MAX_HITS`. |
| Дымовой интеграционный прогон | `src/tests/run_smart_grep_chunk_smoke.py` (после перезапуска `colloquium-core` при 502 — подождать health). |
| MCP: `cq_start_grep` / `cq_fetch_result`, клиент chunk | `mcp-tools/cqds_files.py`, `mcp-tools/cqds_project.py`, `mcp-tools/cqds_client.py`, `mcp-tools/cqds_result_pages.py` (TTL handle после `scan_complete`: env `CQDS_MCP_PAGE_TTL_AFTER_COMPLETE_SEC`). |
| Фаза C: сверка чанков, фронт | `src/tests/compare_smart_grep_search_modes.py` (`--compare-chunks`, `--chunk-vs-sync-only`); в `frontend/src` нет прямого `smart_grep`. |
| Фаза D: host_fs async | `mcp-tools/cqds_host_grep_jobs.py`, `host_async` в `cq_start_grep`, `host_grep_job_id` в `cq_fetch_result`; `build_ripgrep_argv` / `hit_dict_from_rg_json_line` в `cqds_smart_grep_host.py`. |

**Ограничение v1:** если один файл даёт больше совпадений, чем `max_hits`, в чанк попадают только первые `max_hits` совпадений из этого файла; остаток по файлу отбрасывается (следующий `offset` всё равно сдвигается на следующий `file_id`). Уточнить в следующей итерации при необходимости.

---

## 2. Модель данных на сервере (ядро)

### 2.1 `index_epoch` (per `project_id`)

- Целое неотрицательное, монотонно растёт при каждом успешном завершении **`scan_project_files`** для данного проекта (в т.ч. частичного — по политике продукта; зафиксировать в коде один вариант).
- Хранение: в памяти процесса рядом с `ProjectManager` / глобальный словарь `project_id → epoch`; при рестарте ядра epoch сбрасывается → клиенты перезапускают чанки (приемлемо) **или** персист в БД/Redis (опционально, позже).

### 2.2 Кэш «подкаталог → file_id»

**Ключ:** `(project_id, path_prefix_norm, index_epoch)`  
**Значение:** отсортированный по `file_id` возрастанию список id файлов, у которых `file_name` относительно корня проекта **начинается с** `path_prefix_norm` (после нормализации), и которые проходят те же фильтры, что и сегодняшний `smart_grep` (`mode`, `profile`, `include_glob` — если кэш строится под конкретный набор фильтров, включить их в ключ; иначе строить «широкий» список по префиксу и фильтровать в чанке — проще ключ, больше работы в чанке).

**Рекомендация по ключу (v1):** включить в ключ кэша **`mode` + `profile` + хэш `include_glob`**, чтобы список id совпадал с семантикой текущего grep. Альтернатива — один список «все id под префиксом» и фильтрация mode/profile в цикле чанка (меньше записей в кэше, больше CPU на чанк).

**Инвалидация:** при увеличении `index_epoch` для `project_id` все ключи с прежним epoch считаются недействительными (не удалять вручную каждый — достаточно проверки epoch при чтении).

**Лимиты:** см. §6.

---

## 3. HTTP API ядра (контракт)

Базовый префикс как у остального API: `/api/...` (через nginx — `/api/...`).

### 3.1 `POST /project/smart_grep/chunk` (новый эндпоинт)

**Назначение:** один короткий шаг grep по ограниченному числу файлов из заранее определённого упорядоченного списка.

**Заголовки:** как у защищённых маршрутов — сессия (`session_id`).

**Тело JSON:**

```json
{
  "project_id": 1,
  "index_epoch": 0,
  "path_prefix": "agent/routes",
  "offset": 0,
  "limit_files": 50,
  "max_hits": 100,
  "query": "smart_grep",
  "mode": "code",
  "profile": "all",
  "is_regex": false,
  "case_sensitive": false,
  "context_lines": 0,
  "include_glob": [],
  "time_strict": null,
  "search_mode": "project_registered"
}
```

| Поле | Тип | Обяз. | Описание |
|------|-----|-------|----------|
| `project_id` | int | да | |
| `index_epoch` | int | да | Должен совпадать с текущим epoch проекта на сервере. |
| `path_prefix` | string | нет | Пустая строка или отсутствует — весь проект (как сейчас). Нормализация: POSIX, без ведущего `/`, без `..`. |
| `offset` | int | да | Индекс в упорядоченном списке id (0-based). |
| `limit_files` | int | да | Сколько файлов из списка обработать за вызов (жёсткий верх на сервере, напр. 1–200). |
| `max_hits` | int | да | Остановка после стольких попаданий в **этом** чанке (верх на сервере). |
| `query` … `search_mode` | | да/как сейчас | Семантика как у `POST /project/smart_grep`. |

**Поведение `search_mode`:**

- `project_registered` — только индекс, без скана диска.
- `project_refresh` — **в первой итерации** либо запретить в чанке и требовать отдельного вызова полного scan, либо выполнять scan только при `offset === 0` один раз (уточнить в реализации; в спецификации v1 рекомендуется: **в чанке только `project_registered`**; refresh — отдельный явный вызов существующего scan/mаршрута).

**Ответ 200 JSON:**

```json
{
  "status": "ok",
  "project_id": 1,
  "index_epoch": 0,
  "path_prefix": "agent/routes",
  "offset": 0,
  "limit_files": 50,
  "files_scanned": 50,
  "total_ids_in_scope": 420,
  "next_offset": 50,
  "scan_complete": false,
  "truncated_by_max_hits": false,
  "hits": [],
  "next_file_ids": [12, 15, 18],
  "more_file_ids_pending": true
}
```

| Поле | Описание |
|------|----------|
| `files_scanned` | Фактически обработано файлов в этом чанке. |
| `total_ids_in_scope` | Длина списка id для данного scope (префикс + фильтры), после применения кэша. |
| `next_offset` | Смещение для следующего запроса; если `scan_complete`, равно `total_ids_in_scope` или `null` (зафиксировать одно). |
| `scan_complete` | `true`, если все файлы из списка обработаны **в рамках логики чанка** (нет необработанных id после текущего блока). |
| `truncated_by_max_hits` | Остановились из-за `max_hits`; клиент может повторить тот же `offset` с другой политикой (не рекомендуется) или продолжить с `next_offset` после сохранения порога (v1: документировать «при true следующий запрос с тем же offset пропускает уже отданные хиты» — сложнее; проще: **не останавливать mid-file** по max_hits и резать только между файлами). |
| `next_file_ids` | Опционально: до **N** следующих `file_id` (батч для отладки/клиентов); **не обязательно полный хвост** — см. `more_file_ids_pending`. |
| `more_file_ids_pending` | `true`, если после `next_file_ids` ещё есть id до конца списка. |

**Ошибки:**

- `400` — валидация, неверный `path_prefix`.
- `409` — `index_epoch` не совпадает: `{ "detail": "stale_index_epoch", "current_epoch": 3 }`.
- `404` — проект не найден.

### 3.2 Существующий `POST /project/smart_grep`

- **v1 после внедрения чанков:** либо оставить для малых проектов / совместимости фронта, либо перевести на внутреннюю реализацию «один чанк на весь список» с жёстким лимитом и рекомендацией использовать `/chunk` (решение в чеклисте).

---

## 4. MCP (контракт инструментов)

Переименование **без** сохранения старых имён.

### 4.1 `cq_start_grep`

**Назначение:** начать поиск: вызвать первый чанк (или только вернуть метаданные — в v1 достаточно «первый чанк в одном вызове»).

**Параметры** (логически совпадают с телом chunk + опции):

- `project_id`, `query`, `mode`, `profile`, `is_regex`, `case_sensitive`, `context_lines`, `include_glob`, `time_strict`, `path_prefix` (опц.), `search_mode` (по умолчанию `project_registered`).
- `limit_files`, `max_hits`, `offset` (по умолчанию `0`).
- `wait_scan_timeout_sec` — для сценария «дождаться готовности индекса» (если введём); `0` = не ждать.

**Ответ:** JSON как у API chunk + при необходимости обогащение полями для MCP (`paging` при большом ответе — см. существующий `finalize_smart_grep_response`).

### 4.2 `cq_fetch_result`

**Назначение:** следующий чанк **stateless**: передать в API тот же `project_id`, `index_epoch`, `path_prefix`, параметры grep и **`offset = next_offset`** из предыдущего ответа.

**Параметры:**

- Все, что нужно для идемпотентного повторения чанка (копия контекста из предыдущего ответа + `offset`).
- Опционально `page_size` / `limit_files` override.

**TTL 30 минут** после `scan_complete` на стороне MCP: хранить накопленные hits в существующем или расширенном page-store с ключом по **хэшу параметров поиска + session** не обязательно — достаточно держать в памяти клиента агента; если нужен серверный MCP-кэш — см. `cqds_result_pages.py`, TTL от **момента завершения склейки** (`scan_complete`), не от первого чанка.

---

## 5. Согласованность и гонки

- Порядок id — **стабильный** (сортировка по `file_id`).
- Рескан увеличивает `index_epoch` → активные цепочки чанков получают `409` и должны начать с `offset=0` и новым `index_epoch` из ответа ошибки или отдельного `GET /project/{id}/index_meta`.
- Параллельные чанки с одними параметрами от одного пользователя — допустимы (идемпотентность по offset); от разных пользователей — независимы.

---

## 6. Лимиты памяти и производительности (VPS)

| Лимит | Предлагаемое значение (настраиваемо env) |
|-------|------------------------------------------|
| Число ключей кэша префиксов | 32–256 на процесс |
| Суммарное число хранимых `file_id` | 2–10 M (оценка ~8–40 MB) |
| `limit_files` max | 100–200 |
| `max_hits` max на чанк | 500–2000 |
| Размер `next_file_ids` в ответе | ≤ 500 элементов |

При превышении — LRU вытеснение ключей кэша (пересборка при следующем запросе).

---

## 7. Тестирование

- Юнит: нормализация `path_prefix`, построение списка id из мок-индекса.
- Интеграция: склейка чанков = один синхронный `smart_grep` на малом проекте.
- Нагрузка: K чанков подряд, проверка отсутствия утечки памяти (ключи кэша ограничены).
- **Сделано:** `python src/tests/run_smart_grep_chunk_smoke.py --project-id 1` (логин через `mcp-tools/cqds_credentials.py`).
- **Сверка sync vs чанки:** `python src/tests/compare_smart_grep_search_modes.py --project-id N --chunk-vs-sync-only` или полный прогон с `--without-project-refresh --compare-chunks` (и опционально `--mcp-page-size K`).

### 7.1 Метрики на живом поиске (крупный проект)

**Скрипт (одна строка JSON в stdout, прогресс в stderr):**

```bash
python src/tests/run_smart_grep_chunk_smoke.py --project-id N --query "ваш_запрос" \
  --path-prefix "" --limit-files 100 --max-hits 800 --max-returned-items 100 --stats-json
```

В JSON: `core_http_get_index_meta`, `core_http_post_smart_grep_chunk_200` (число успешных чанков), `core_http_post_smart_grep_chunk_409`, `sum_hits_all_chunks`, `hits_per_http_chunk`, оценки MCP `mcp_est_tool_calls_chunk_only`, `mcp_est_cq_fetch_result_handle_calls`, `mcp_est_tool_calls_chunk_plus_handle_paging`, `mcp_est_pages_if_single_merged_hit_list` (как если бы все hits склеить и резать только `max_returned_items`).

**Из access-лога ядра** (пример `logs/colloquium_serv.log`, путь в строке как у uvicorn — часто без префикса `/api`):

```text
# GET index_meta за сессию поиска (ожидается 1 на цепочку, если клиент не перезапрашивает)
rg 'GET /project/[0-9]+/index_meta' logs/colloquium_serv.log

# Каждый успешный чанк
rg 'POST /project/smart_grep/chunk' logs/colloquium_serv.log
```

Счётчики: `rg -c` по тем же шаблонам на **срезе времени** (после прогона) или через `projectman` DEBUG (`POST /project/smart_grep/chunk user_id=... epoch=... offset=...`) — там видно **итерации** и `complete=` по строкам.

**Синхронный** `POST /project/smart_grep` — отдельная строка в access-логе; для чанкового пути смотрите только `chunk` + `index_meta`.

---

## 8. Чеклист (TODO по шагам)

Задачи идти по порядку; пункты с одним уровнем можно параллелить только там, где нет зависимости.

### Фаза A — ядро

- [x] **A1.** Ввести `project_id → index_epoch` в памяти ядра; инкремент в конце успешного `scan_project_files` (и задокументировать поведение при time-limited scan).
- [x] **A2.** Реализовать нормализацию `path_prefix` и функцию «список `file_id` в scope» с учётом mode/profile/include_glob (выбрать стратегию ключа кэша из §2.2).
- [x] **A3.** Реализовать LRU-кэш `(project_id, path_prefix_norm, filters_hash, index_epoch) → list[file_id]` с лимитами из §6.
- [x] **A4.** Реализовать `POST /project/smart_grep/chunk` по контракту §3.1; проверка сессии как у текущего `smart_grep`.
- [x] **A5.** Решить судьбу `project_refresh` в чанке (v1: только registered или scan при offset=0).
- [x] **A6.** (Опционально) `GET /project/{id}/index_meta` → `{ "index_epoch": N }` для удобства клиента.

### Фаза B — MCP

- [x] **B1.** Переименовать инструмент `cq_smart_grep` → **`cq_start_grep`** (schema, description, handler).
- [x] **B2.** Переименовать `cq_extra_result` → **`cq_fetch_result`**; семантика: следующий чанк + опционально дозагрузка из MCP page-store при склеенном режиме.
- [x] **B3.** Обновить `cqds_client.py`: метод(ы) для `/project/smart_grep/chunk`.
- [x] **B4.** Обновить `finalize_smart_grep_response` / TTL: **от момента `scan_complete`** при агрегировании в MCP (уточнить реализацию в `cqds_result_pages.py`).
- [x] **B5.** Обновить `cqds_mcp_full.md` и любые примеры в репозитории.

### Фаза C — фронт и совместимость

- [x] **C1.** Проверка `frontend/src`: прямых вызовов `smart_grep` нет. **Решение:** синхронный `POST /project/smart_grep` остаётся в ядре для совместимости и скриптов; поиск из UI при появлении — либо чанки, либо sync с жёстким лимитом (отдельная задача).
- [x] **C2.** Интеграционный скрипт `src/tests/compare_smart_grep_search_modes.py`: флаги **`--compare-chunks`** (после тройной сверки) и **`--chunk-vs-sync-only`** (быстрая только склейка vs один sync); вспомогательные **`--chunk-limit-files`**, **`--chunk-search-mode-first`**.

### Фаза D — хост `host_fs`

- [x] **D1.** Асинхронный режим **`host_async=true`** (только `search_mode=host_fs`): фоновая задача MCP запускает `rg --json`, читает stdout через **`asyncio.wait_for(readline, timeout=poll)`** (по умолчанию poll **5 с**, env **`CQDS_HOST_GREP_POLL_SEC`**). Пока нет строки — увеличивается **`snapshot_seq`** (тик); при кратности **`max_returned_items`** по числу hits — ещё один тик (логическая «страница»). Итог отдаётся через **`cq_fetch_result` + `host_grep_job_id`**. Синхронный путь `host_async=false` без изменений. Лимиты: **`CQDS_HOST_GREP_MAX_JOBS`**, **`CQDS_HOST_GREP_JOB_RETAIN_SEC`** после `scan_complete`. Python-fallback без `rg` выполняется в executor одним куском (без потокового stdout). Инфраструктура **`cq_host_process_spawn` / cq_process_spawn`** не задействована внутри — тот же паттерн «фон + опрос», но разбор JSON rg встроен в `cqds_host_grep_jobs.py`. Дымовой скрипт: `python src/tests/test_host_grep_job_smoke.py`.

---

## 9. Версионирование документа

| Версия | Дата | Изменения |
|--------|------|-----------|
| 0.1 | 2026-04-01 | Первый черновик со спецификациями и чеклистом |
| 0.2 | 2026-04-01 | Фаза A в коде: `index_epoch`, кэш scope, `GET index_meta`, `POST smart_grep/chunk`, дымовой скрипт; чеклист A1–A6 отмечен |
| 0.3 | 2026-04-01 | Фаза B: MCP `cq_start_grep` / `cq_fetch_result`, `ColloquiumClient.smart_grep_chunk*`, TTL page-store при `scan_complete`; чеклист B1–B5 отмечен |
| 0.4 | 2026-04-01 | Фаза C: сверка склейки чанков vs sync в `compare_smart_grep_search_modes.py`, зафиксировано отсутствие `smart_grep` во фронте; C1–C2 отмечены |
| 0.5 | 2026-04-01 | Фаза D: `host_async` + фоновый rg + `cq_fetch_result(host_grep_job_id)`; D1 отмечен |

При изменении контрактов — поднимать минорную версию и кратко писать в таблицу.
