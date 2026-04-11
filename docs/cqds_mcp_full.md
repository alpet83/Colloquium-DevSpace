# cqds_mcp_full — Интеграция GitHub Copilot с Colloquium-DevSpace

MCP-сервер (`cqds_mcp_full.py`) транслирует инструменты GitHub Copilot Agent в HTTP-запросы к Colloquium-DevSpace, позволяя Copilot читать чаты, отправлять сообщения и управлять файлами проекта через Colloquium.

> **Предпочтительный вариант для агентов:** для Cursor/Copilot чаще лучше подключать **[cqds_mcp_mini](cqds_mcp_mini.md)** — тот же HTTP API, но **~6× меньше** объявленных MCP-инструментов и **~5× компактнее** JSON-схема `list_tools`, что снижает расход контекста у модели. Полный сервер оставляй для сценариев, где нужны отдельные тонкие инструменты (`cq_host_process_*`, `cq_query_db`, `cq_spawn_script`, пакеты `cq_docker_control_batch` и т.д.) без агрегации через `*_ctl`.

---

## Предварительные требования

### 1. Python-зависимости

Пакеты должны быть установлены в том же Python, что указан в `mcp.json`:

```powershell
python.exe -m pip install httpx mcp
```

Проверка:
```powershell
python.exe -c "import httpx, mcp; print('ok')"
```

### 2. Запущенный Colloquium-DevSpace

```powershell
cd X:\docker\cqds
docker compose up -d
```

После запуска API доступно на `http://localhost:8008`.

> **Маршрутизация через nginx**: все API-вызовы проходят через nginx (порт 8008).
> nginx маршрутизирует `/api/*` → `colloquium-core:8080`, а `/` → frontend.
> MCP-инструмент использует префикс `/api/` внутри — отдельно пробрасывать порт backend **не нужно**.

> **Auth во frontend**: Login-форма предназначена для **человека** — разные пользователи
> (admin, разработчики) работают со своими сессиями в браузере.
> MCP-инструмент использует тот же механизм аутентификации программно — это правильная архитектура.

> **Каталог `logs` в контейнере**: пробрасываемый каталог `logs` часто используется не только для логов ядра,
> но и для журналов непосредственно разрабатываемых проектов (backend/frontend workers, интеграционные скрипты, dev-сервисы).
> Поэтому стратегия диагностики должна учитывать проектные логи как основной источник сигналов.

На первом запуске автоматически создаётся пользователь `admin` с **случайным паролем** — он выводится в лог контейнера:

```powershell
docker logs colloquium-core 2>&1 | Select-String "admin"
# Ищи строку вида: Создан пользователь admin с временным паролем !Xk9...
```

---

## Сервисный пользователь `copilot`

`copilot` должен рассматриваться как постоянный сервисный пользователь для MCP-tool.
Его не нужно создавать и удалять перед каждым запуском VS Code или перезапуском контейнера.

Базовая стратегия:

- `copilot` создаётся один раз на инсталляцию или после полной потери БД
- дальше этот пользователь считается резидентным и переиспользуется всеми локальными запусками MCP-tool
- удаление `copilot` допускается только как аварийная операция: компрометация, пересборка auth-схемы, очистка тестового стенда

Административный скрипт `agent/create_user.py` уже примонтирован в контейнер как `/app/agent/create_user.py`.

```powershell
# Если пользователя ещё нет — создать один раз
docker exec colloquium-core python3 /app/agent/create_user.py copilot devspace

# Если нужен другой пароль при первичной настройке
docker exec colloquium-core python3 /app/agent/create_user.py copilot мой_пароль

# Список всех пользователей
docker exec colloquium-core python3 /app/agent/create_user.py --list
```

Проверка существования `copilot`:
```powershell
docker exec colloquium-core python3 /app/agent/create_user.py --list
# Если copilot уже есть в списке — ничего пересоздавать не нужно
```

Пока отдельная смена пароля не автоматизирована, аварийная ротация может выполняться через удаление и повторное создание,
но это именно исключение, а не штатный сценарий ежедневной работы.

---

## Настройка mcp.json

### Где лежит конфиг

| Редактор | Путь | Корневой ключ в JSON |
|----------|------|----------------------|
| VS Code (GitHub Copilot Agent) | `.vscode/mcp.json` | `servers` |
| Cursor | `.cursor/mcp.json` | `mcpServers` |

Содержимое секции сервера `colloquium` одно и то же; отличаются только обёртка и имя каталога. В Cursor для stdio-серверов нужно поле `"type": "stdio"` (см. [документацию Cursor по MCP](https://cursor.com/docs/context/mcp)).

`cqds_mcp_full.py` при поиске токена в файлах обходит вверх по дереву каталогов и читает **и** `.vscode/mcp.json`, **и** `.cursor/mcp.json`, в том числе блоки `servers` и `mcpServers`.

### Пример для VS Code (фрагмент `servers`)

```json
"colloquium": {
  "command": "X:\\Python3\\python.exe",
  "args": [
    "X:\\docker\\cqds\\cqds_mcp_full.py",
    "--url",  "http://localhost:8008",
    "--username", "copilot"
  ],
  "type": "stdio",
  "env": {
    "MCP_AUTH_TOKEN": "<токен_для_cqds_ctl>"
  }
}
```

### Пример для Cursor (фрагмент `mcpServers`)

```json
{
  "mcpServers": {
    "colloquium": {
      "type": "stdio",
      "command": "X:\\Python3\\python.exe",
      "args": [
        "X:\\docker\\cqds\\cqds_mcp_full.py",
        "--url",
        "http://localhost:8008",
        "--username",
        "copilot"
      ],
      "env": {
        "MCP_AUTH_TOKEN": "<токен_для_cqds_ctl>"
      }
    }
  }
}
```

### Секреты: не хранить токен в JSON в репозитории

Литерал `MCP_AUTH_TOKEN` в закоммиченном `mcp.json` нежелателен. Практичный вариант:

1. Один раз задать переменную окружения на машине (пользовательский профиль PowerShell, системные переменные Windows, CI — свой механизм). Удобное отдельное имя, например `COLLOQUIUM_MCP_TOKEN`, чтобы не путать с другими токенами.
2. В `mcp.json` передать её в процесс MCP через `env` с **интерполяцией** (Cursor поддерживает синтаксис `${env:ИМЯ}` для полей вроде `command`, `args`, `env`):

```json
"env": {
  "MCP_AUTH_TOKEN": "${env:COLLOQUIUM_MCP_TOKEN}"
}
```

При старте Cursor подставит значение из окружения; в git остаётся только шаблон без секрета.

Дополнительно в Cursor можно использовать `${workspaceFolder}` в путях к `python` и к `cqds_mcp_full.py`, чтобы не дублировать абсолютный диск.

> **VS Code**: наличие такой же интерполяции в MCP-конфиге зависит от версии и хоста Copilot. Если `${env:...}` не сработает, оставьте токен только в переменной окружения пользователя (хост всё равно передаёт `env` в дочерний процесс при явной настройке) или держите отдельный локальный `mcp.json` / фрагмент вне репозитория и добавьте путь в `.gitignore`.

`MCP_AUTH_TOKEN` используется инструментом `cq_docker_control` для авторизации вызовов к `cqds_ctl.py` через localhost HTTP API.
Если значение не попало в окружение процесса, `cqds_mcp_full.py` пробует прочитать токен из `mcp.json` (см. выше), и как последний fallback использует значение по умолчанию `Grok-xAI-Agent-The-Best`.

> **Источники `MCP_AUTH_TOKEN` (по приоритету)**:
> 1. `MCP_AUTH_TOKEN` env-переменная окружения **процесса MCP** (в т.ч. заданная через `env` в `mcp.json`, в том числе после интерполяции `${env:...}`)
> 2. поле `env.MCP_AUTH_TOKEN` у сервера `colloquium` в найденном `.vscode/mcp.json` или `.cursor/mcp.json` (включая блоки `servers` / `mcpServers`)
> 3. встроенный fallback `Grok-xAI-Agent-The-Best`

Значение токена должно совпадать с токеном, настроенным в `cqds_ctl.py` / `scripts/cqds_ctl.py`.

### Перезапуск MCP после смены кода или `mcp.json`

У **Cursor** нет отдельной штатной кнопки «Restart MCP server» для каждого stdio-сервера: процесс Python поднимает и держит сам редактор. Включение/выключение сервера в настройках (**Settings** → **Tools & MCP** или **Features → Model Context Protocol**, в зависимости от версии) иногда **не подхватывает** новый код так же надёжно, как полный цикл перезапуска.

Рекомендуемый порядок (от мягкого к жёсткому):

1. **Отключить сервер** в списке MCP → подождать пару секунд → **включить снова**. Имеет смысл, если зависло соединение; после правок в `cqds_mcp_full.py` может оказаться недостаточно.
2. **Reload Window** — палитра команд (`Ctrl+Shift+P` / `Cmd+Shift+P`) → **Developer: Reload Window**. Перезагружает окно редактора и часто **полностью пересоздаёт** stdio MCP-процессы без выхода из Cursor.
3. **Полный выход из Cursor** (закрыть все окна приложения) и запуск заново — **самый надёжный** вариант после изменения `.cursor/mcp.json`, путей к `python`, аргументов, переменных окружения или когда пункты 1–2 не помогли.

Дополнительно: **View → Output** → в выпадающем списке канал вроде **MCP** / **MCP Logs** — по логам видно, стартовал ли процесс и нет ли ошибки импорта/пути.

**Логи вида `skipping invalid path file://p%3A` (Windows):** это обычно **не** `cqds_mcp_full`, а клиент Cursor или MCP **filesystem**, когда в протокол передаётся корень рабочей папки как `file:`-URL. Символ `:` в диске (`P:`) кодируется как `%3A`; для части проверок такой URI **невалиден** (корректная форма для Windows чаще ближе к `file:///P:/...` с тремя слэшами и буквой диска). Сообщение означает «этот root пропускаем»; на **colloquium** и HTTP к CQDS это не влияет. Если страдает именно **filesystem** MCP, попробуй в `mcp.json` путь через **`${workspaceFolder}`**, букву диска в **верхнем регистре** в явном пути (`P:\\...`), либо открыть папку проекта так, чтобы корень workspace совпадал с аргументом сервера — см. также [issues по MCP filesystem на Windows](https://github.com/modelcontextprotocol/servers/issues).

**VS Code** (GitHub Copilot + MCP): после правок `.vscode/mcp.json` или скрипта MCP обычно достаточно **Developer: Reload Window** или перезапуска VS Code.

Предпочтительный локальный вариант: пароль хранится в отдельном файле рядом с `cqds_mcp_full.py`
или в другом защищённом месте на хосте.

### Рекомендуемый вариант: sidecar secret

В репозитории лежит **шаблон** `mcp-tools/cqds_mcp_auth.sample.secret` (можно коммитить). Локально создай рабочий файл **`mcp-tools/cqds_mcp_auth.secret`** (он в `.gitignore`):

1. Скопируй образец: `Copy-Item cqds_mcp_auth.sample.secret cqds_mcp_auth.secret` (из каталога `mcp-tools`).
2. Открой `cqds_mcp_auth.secret` и оставь в нём **ровно одну строку** — пароль пользователя `copilot`, без `#`, без JSON и без пробелов по краям.

После этого `cqds_mcp_full.py` подхватит пароль автоматически, даже если в `mcp.json` пароль не указан.

### Явное указание файла секрета

Если секрет лежит не рядом со скриптом, можно передать путь через `env`:

```json
"env": { "COLLOQUIUM_PASSWORD_FILE": "X:\\secrets\\copilot_password.txt" }
```

или через аргументы:

```json
"args": [..., "--password-file", "X:\\secrets\\copilot_password.txt"]
```

### Прямой пароль

Если файловый секрет пока не используется, пароль можно передать напрямую через аргумент или переменную окружения:

```json
"args": [..., "--password", "мой_пароль"]
```

или через `env`:

```json
"env": { "COLLOQUIUM_PASSWORD": "мой_пароль" }
```

> Не коммитить пароль в git. В репозитории — только `cqds_mcp_auth.sample.secret`; рабочий `cqds_mcp_auth.secret` остаётся локально.

### Приоритет источников пароля

`cqds_mcp_full.py` ищет пароль в таком порядке:

1. `--password`
2. `--password-file`
3. `COLLOQUIUM_PASSWORD`
4. `COLLOQUIUM_PASSWORD_FILE`
5. `cqds_mcp_auth.secret` рядом со скриптом (если его нет — устаревший `copilot_mcp_tool.secret`)
6. fallback: `devspace`

При старте MCP-tool пишет в stderr диагностическую строку с источником пароля и коротким preview:

```text
MCP auth password source: cqds_mcp_auth.secret; preview=tE...
```

Это сделано для быстрой диагностики двух случаев:

- tool неожиданно взял `default` и работает на `devspace`
- tool читает не тот секретный файл, который ожидался

Preview намеренно показывает только первые 2 символа, а не полный пароль.

Альтернатива для локальной машины: дать `cqds_mcp_full.py` возможность читать пароль из локального секрета рядом с размещением,
например из файла вида `cqds_mcp_auth.secret` или через `COLLOQUIUM_PASSWORD_FILE`.

Это разумно, если предполагается, что при получении локального доступа к хосту парольная защита уже не является главным барьером.
В таком режиме в git хранится только путь к секрету, а не сам пароль.

---

## Доступные инструменты

| Инструмент | Описание |
|---|---|
| `cq_list_chats` | Список всех чатов |
| `cq_create_chat` | Создать новый чат (вернёт `chat_id`) |
| `cq_send_message` | Отправить сообщение в чат (в sync-режиме ждёт финальный ответ, пропуская прогресс-заглушки) |
| `cq_wait_reply` | Long-poll ответа AI (до 15 с) |
| `cq_get_history` | Получить текущий срез истории чата без ожидания |
| `cq_chat_stats` | Агрегированная статистика использования чата (calls/tokens/model/cost), опционально за период |
| `cq_edit_file` | Записать файл через `<code_file>` (создать или перезаписать) |
| `cq_patch_file` | Применить unified-diff через `<patch>` |
| `cq_undo_file` | Откатить файл к бэкапу через `<undo>` |
| `cq_list_projects` | Список проектов, зарегистрированных в Colloquium |
| `cq_select_project` | Выбрать активный проект в сессии |
| `cq_list_files` | Лёгкий индекс файлов проекта (без контента); опционально `as_tree` → вложенное JSON-дерево по сегментам `file_name`, `include_flat` — плоский список рядом с деревом |
| `cq_get_index` | Получить rich-index чата/проекта |
| `cq_rebuild_index` | Пересобрать rich-index проекта по требованию |
| `cq_get_code_index` | Совместимый alias для `cq_rebuild_index` |
| `cq_read_file` | Прочитать файл по DB `file_id` |
| `cq_exec` | Выполнить shell-команду в проекте |
| `cq_query_db` | Выполнить read-only SQL через backend DB layer (debug) |
| `cq_set_sync_mode` | Включить/выключить синхронный режим для `cq_send_message` |
| `cq_start_grep` | Старт поиска: `host_fs` на хосте MCP (опционально **`host_async=true`** — фоновый `rg`, опрос через `cq_fetch_result` + `host_grep_job_id`) или первый **stateless-чанк** по проекту; в ответе `chunk_continuation` / `paging` / `host_grep_job_id` |
| `cq_fetch_result` | Следующий чанк (`chunk_continuation`), страница из кэша (`handle`), или снимок **host async** (`host_grep_job_id`; подсказка `host_grep_poll_hint_sec`, обычно ~5 с) |
| `cq_grep_entity` | Поиск строк индекса сущностей (только **объявления**: function/class/method/…), один или несколько regex, поля name/parent/qualified; опционально `ensure_index` |
| `cq_grep_logs` | Сканирование одного/нескольких log-файлов по маскам с regex-фильтрацией |
| `cq_docker_control` | Управление Docker Compose сервисами CQDS на хосте (status/restart/rebuild/clear-logs) |
| `cq_docker_control_batch` | Пакетно то же, что `cq_docker_control`, через `cqds_ctl.py`: массив `requests`, `results` + `all_ok`, опционально `stop_on_error` |
| `cq_docker_exec` | Пакетный **`docker exec`** на машине MCP (не `cqds_ctl`): `requests` с полями `container`, `command` (строка или argv), опционально `workdir`, `user`, `env`, `stdin`, `interactive`, `timeout_sec` |
| `cq_host_process_spawn` | Subprocess на **локальном хосте MCP** (не sandbox): `command` строка или argv, `cwd`, `env`, `timeout` |
| `cq_host_process_io` | Чтение накопленного stdout/stderr и опциональная запись в stdin (текст UTF-8) |
| `cq_host_process_status` / `cq_host_process_list` / `cq_host_process_wait` / `cq_host_process_kill` | Аналоги инструментов семейства `cq_process`, но для хост-процессов |     
| `cq_replace` | Точный replace в файле по `file_id` |
| `cq_process_spawn` | Запустить subprocess в mcp-sandbox (возвращает `process_guid`) |
| `cq_process_io` | Читать/писать stdin/stdout/stderr процесса по `process_guid` |
| `cq_process_status` | Статус процесса + runtime/cpu метрики |
| `cq_process_list` | Список процессов (опционально по `project_id`) |
| `cq_process_wait` | Ожидание условия процесса (`any_output`/`finished`) |
| `cq_process_kill` | Отправить сигнал процессу (`SIGTERM`/`SIGKILL`) |
| `cq_spawn_script` | Создать и выполнить временный bash/python-скрипт в mcp-sandbox за один вызов |
| `cq_project_status` | Диагностика состояния проекта (problems, links, backup/undo, scan/index cache) |

### Типичный рабочий цикл

**Вариант 1: Асинхронный (без ожидания)**
```
1. cq_list_chats           → найти нужный chat_id (или cq_create_chat)
2. cq_send_message         → отправить задачу / вопрос (возвращается сразу)
3. cq_wait_reply           → получить ответ AI (long-poll)
4. cq_edit_file / cq_patch_file → применить изменения в файлах проекта
```

**Вариант 2: Синхронный (рекомендуется для Copilot)**
```
1. cq_set_sync_mode timeout=60  → включить ожидание финального ответа
2. cq_list_chats                → найти нужный chat_id (или cq_create_chat)
3. cq_send_message              → отправить задачу (ждёт ответ до 60 сек, пропуская прогресс)
4. cq_edit_file / cq_patch_file → применить изменения в файлах проекта
```

В sync-режиме `cq_send_message` автоматически пропускает промежуточные прогресс-сообщения
(например "⏳ готовлю ответ") и возвращает только финальный ответ LLM когда он готов.

### Параметры `cq_set_sync_mode`

```
timeout  — время ожидания ответа AI после отправки сообщения, сек
           0 = выключить sync-режим (возвращать ответ сразу, default)
           60 = ждать ответ до 60 сек (пропуская прогресс-заглушки)
           макс: 300
```

Когда включен sync-режим, `cq_send_message` автоматически ждёт финального ответа LLM, пропуская промежуточные сообщения вроде "Запрос принят", "⏳ готовлю ответ". Это улучшает UX — нет нужды вызывать `cq_wait_reply` отдельно.

### Параметры `cq_query_db`

```
project_id — ID проекта (контекст выполнения)
query      — read-only SQL (SELECT / WITH / EXPLAIN)
timeout    — лимит времени выполнения, сек (по умолчанию 30)
```

Ограничения безопасности:

```
- разрешены только read-only запросы (SELECT / WITH / EXPLAIN)
- мутационные ключевые слова (INSERT/UPDATE/DELETE/...) блокируются на стороне MCP-tool
```

Пример использования (отладка):

```powershell
cq_query_db project_id=1 query="SELECT COUNT(*) FROM posts"
```

### Параметры `cq_grep_logs`

```
project_id    — ID проекта (контекст выполнения в контейнере)
query         — regex для фильтрации строк логов
log_masks     — JSON-массив glob-масок (например ["logs/*.log", "logs/**/*.txt"])
tail_lines    — лимит количества совпавших строк на файл (по умолчанию 100)
since_seconds — необязательное окно по времени в секундах; когда > 0, выбираются строки только за последние N секунд
case_sensitive — чувствительность regex к регистру (по умолчанию false)
```

Формат результата:

```json
{
  "logs/app.log": ["...", "..."],
  "logs/worker/error.log": []
}
```

`cq_grep_logs` читает логи внутри контейнерного контекста проекта; ссылочные/служебные логи,
которые не опубликованы наружу, доступны именно через этот инструмент.

### Параметры process tools (`cq_process_*`)

Ключевой идентификатор процесса: `process_guid` (UUID-строка).

Важно:

- legacy-поле `process_id` больше не поддерживается
- для `cq_process_io`, `cq_process_status`, `cq_process_wait`, `cq_process_kill` параметр `process_guid` обязателен

`cq_process_spawn`

```
project_id — ID проекта (контекст и лимиты)
command    — команда shell/python
engine     — bash | python (default: bash)
cwd        — рабочий каталог (опционально)
env        — dict переменных окружения (опционально)
timeout    — TTL процесса в секундах (default: 3600)
```

Успешный ответ:

```json
{
  "process_guid": "uuid",
  "status": "spawned"
}
```

`cq_process_status`

```
process_guid — GUID процесса
```

Возвращает в том числе метрики производительности:

```
runtime_ms  — wall-clock длительность выполнения процесса
cpu_time_ms — накопленное CPU время процесса (user+system)
```

Типичный ответ:

```json
{
  "process_guid": "uuid",
  "status": "running|finished|error",
  "alive": true,
  "exit_code": null,
  "started_at": 1774600380.866,
  "finished_at": null,
  "runtime_ms": 154,
  "cpu_time_ms": 0,
  "pid": 31
}
```

`cq_process_io`

```
process_guid    — GUID процесса
input           — опциональная строка для stdin
read_timeout_ms — timeout чтения (default: 5000)
max_bytes       — максимум байт фрагментов stdout/stderr (default: 65536)
```

`cq_process_wait`

```
process_guid    — GUID процесса
wait_timeout_ms — timeout ожидания (default: 30000)
wait_condition  — any_output | finished
```

`cq_process_kill`

```
process_guid — GUID процесса
signal       — SIGTERM | SIGKILL (default: SIGTERM)
```

### Параметры `cq_edit_file`

```
chat_id  — ID чата, куда будет отправлен XML-блок
path     — путь к файлу относительно корня проекта (например, src/app.ts)
content  — полное содержимое файла
```

Файл сохраняется в `/app/projects/<project_name>/<path>` внутри контейнера.  
Чтобы Copilot видел результат на хосте, примонтируй нужную папку в `docker-compose.yml`:

```yaml
volumes:
  - P:/vps.alpet.me/sigsys-ts:/app/projects/sigsys-ts
```

### Параметры `cq_patch_file`

```
chat_id  — ID чата
path     — путь к файлу
diff     — unified diff (формат `diff -u`)
```

### Параметры `cq_undo_file`

```
chat_id   — ID чата
file_id   — числовой ID файла в Colloquium (из ответа edit/patch)
time_back — seconds to look back (по умолчанию 3600)
```

---

## 🔒 Безопасность и бэкапы при работе с БД

### Важно: защита декоративна, контроль поведения — ваша ответственность

Инструмент `cq_query_db` **блокирует** явные мутационные запросы (INSERT/UPDATE/DELETE/DROP), но:

- **Через `cq_exec`** вы можете выполнить любой скрипт или команду в проекте  
  ⟹ включая доступ к БД с любыми намерениями  
- **Через `cq_send_message` → LLM** agent может предложить опасные операции  
- **В разработке** обычно используется SQLite или копия Postgres — проще восстановить

### ⚠️  Правило: **BACKUP перед любыми изменениями БД**

**Всегда:**
1. **Сделай бэкап** перед операциями с БД:
   ```powershell
   docker exec colloquium-core pg_dump -U postgres colloquium_db > backup_$(Get-Date -Format 'yyyyMMdd_HHmmss').sql
   # или для SQLite:
   docker exec colloquium-core cp /data/colloquium.db /backups/colloquium_$(Get-Date -Format 'yyyyMMdd_HHmmss').db
   ```

2. **Проверь запрос** перед выполнением:
   - Убедись, что это SELECT (для `cq_query_db`)  
   - Убедись, что ты понимаешь, что произойдёт

3. **После удаления/обновления:**
   - Визуально проверь результат (SELECT * FROM таблица WHERE ...)
   - Если что-то не так — восстанови из бэкапа:
     ```powershell
     docker exec -i colloquium-core psql -U postgres colloquium_db < backup_YYYYMMDD_HHMMSS.sql
     ```

### Сценарии восстановления

**Случайно удалил строку(и) из БД:**
```bash
# 1. Останови контейнер (чтобы БД не писала дальше)
docker stop colloquium-core

# 2. Восстанови бэкап
docker exec -i colloquium-core psql -U postgres colloquium_db < backup.sql

# 3. Запусти контейнер
docker start colloquium-core
```

**Нужно откатить последние N часов:**
```bash
# Если используешь WAL или иную систему версионирования, восстанови из бэкапа + примени логи
# В простом случае — восстанови последний бэкап до этого момента
```

---

## Параметры командной строки

```
--url        URL Colloquium-DevSpace  (default: http://localhost:8008)
             env: COLLOQUIUM_URL
--username   Имя пользователя         (default: copilot)
             env: COLLOQUIUM_USERNAME
--password   Пароль                   (highest priority)
             env: COLLOQUIUM_PASSWORD
--password-file  Файл с паролем       (next priority after --password)
                 env: COLLOQUIUM_PASSWORD_FILE
--chat-id    Чат по умолчанию (инфо) (default: 0)
             env: COLLOQUIUM_CHAT_ID
```

Логирование MCP-tool:

```text
По умолчанию runtime-лог пишется в ./logs/cqds_mcp_full.runtime.log
```

Можно переопределить переменными окружения:

```text
COLLOQUIUM_MCP_LOG_FILE  полный путь к log-файлу
COLLOQUIUM_MCP_LOG_LEVEL уровень логирования (например INFO/DEBUG)
```

---

## Проверка работоспособности

Запусти сервер вручную — если стартует без ошибок, конфигурация верна:

```powershell
python.exe X:\docker\cqds\mcp-tools\cqds_mcp_full.py `
  --url http://localhost:8008 --username copilot
# Должен зависнуть (ожидает stdin от MCP-клиента) — Ctrl+C для выхода
```

Проверь, что логин работает:

```powershell
Invoke-RestMethod -Method POST -Uri "http://localhost:8008/api/login" `
  -ContentType "application/json" `
  -Body '{"username":"copilot","password":"<пароль_из_файла_или_секрета>"}'
# Ожидаемый ответ: объект с полями role, user_id и т.п.
```

---

## Инструмент `cq_docker_control`

Позволяет Copilot управлять Docker Compose сервисами CQDS без прямого доступа к shell.

### Параметры

```
command  — действие:
  status      — состояние контейнеров, health, недавние ошибки в логах
  restart     — docker compose restart + ожидание стабильного состояния
  rebuild     — docker compose up -d --build + ожидание стабильного состояния
  clear-logs  — обрезка json-file логов контейнеров через Docker VM
services — список имён compose-сервисов (опционально; без параметра — все сервисы)
           Имена сервисов: postgres, nginx-router, frontend, colloquium-core, mcp-sandbox
timeout  — секунд ожидания стабильного состояния (по умолчанию 90; для status/restart/rebuild)
wait     — для status: блокировать до stable/failed вместо моментального снимка (по умолчанию false)
```

### Примеры

```
→ cq_docker_control command=status
  Ожидаемый ответ: JSON с overall_status (stable/unstable) и списком сервисов

→ cq_docker_control command=status services=["colloquium-core"]
  Снимок только для одного сервиса

→ cq_docker_control command=restart services=["colloquium-core"] timeout=120
  Перезапуск ядра с ожиданием до 120 секунд

→ cq_docker_control command=clear-logs
  Очистить log-файлы всех контейнеров (нужно при зависании docker logs)
```

## Инструмент `cq_docker_control_batch`

Те же действия, что у `cq_docker_control`, но **несколько шагов за один вызов** через `scripts/cqds_ctl.py`: поле `requests` — JSON-массив объектов `{ command, services?, timeout?, wait? }`. Ответ: `results` (по порядку, у каждого `ok`, при успехе `response` — JSON от `cqds_ctl`, при ошибке `error` / `stdout` / `stderr`), плюс `all_ok` и `count`. Параметр `stop_on_error` (по умолчанию false): при `true` выполнение прерывается после первого `ok: false`.

## Инструмент `cq_docker_exec`

Пакетный вызов **`docker exec`** на той же машине, где запущен MCP (CLI из `PATH`). Каждый элемент `requests`: обязательные `container` и `command` (строка выполняется как `sh -c` **внутри контейнера**, массив строк — как argv без оболочки). Опционально: `workdir`, `user`, `env` (объект → флаги `-e`), `stdin` (UTF-8), `interactive` (флаг `-i`; если задан `stdin`, `-i` включается автоматически), `timeout_sec` (1–600, по умолчанию 120). Ответ: `results` с полями `ok`, `returncode`, `stdout`, `stderr`, `request`; плюс `all_ok`, `count`. Рабочий каталог процесса `docker` — корень репозитория cqds (рядом со `scripts/cqds_ctl.py`).

## Хост-процессы: семейство `cq_host_process`

Интерактивная модель как у семейства `cq_process`, но процесс создаётся **на хосте MCP** (`asyncio` subprocess), без HTTP в mcp-sandbox. После `cq_host_process_spawn` используйте `cq_host_process_io`, `cq_host_process_wait`, `cq_host_process_status`; завершение — `cq_host_process_kill` (запись удаляется из реестра; лимит одновременных записей ~48, буферы stdout/stderr усечены по объёму).

**Пейджер (`more` / `less`) внутри контейнера через `docker exec`:** если запустить на хосте только `docker exec -i <id> more /path/to/file`, вывода в pipe часто не будет — утилита ждёт нормальный TTY. Обход: внутри контейнера обернуть вызов в псевдо-терминал, например `script -q -c 'more /path/to/file' /dev/null` (в образе должен быть `script`, обычно пакет `util-linux`). Тогда `cq_host_process_spawn` поднимает **на хосте** процесс `docker exec -i … sh -c '…'` (удобно передавать **argv-массивом**, без экранирования длинной строки для cmd), а `cq_host_process_io` читает накопленный stdout (в ответе — хвост буфера), шлёт в stdin пробел для следующей страницы и `q` для выхода из `more`.

> **Примечание о `docker logs --tail`**: при ручной обрезке json-file логов контейнера
> команда `docker logs --tail=N` начинает висеть бесконечно — известное поведение Docker.
> `cqds_ctl.py` обходит это, читая json-файл логов напрямую через вспомогательный alpine-контейнер,
> поэтому `status` работает корректно даже после `clear-logs`.

---

## Быстрый старт: первые тесты в Copilot

После запуска `docker compose up -d` и создания пользователя `copilot`:

1. **Убедись, что API доступна:**
   ```
   ✓ cq_list_chats → должна вернуть массив чатов (может быть пусто: [])
   ```

2. **Создай тестовый чат:**
   ```
   → cq_create_chat description="Test MCP"
   ✓ Должна вернуть chat_id (например, 1)
   ```

3. **Отправь тестовое сообщение:**
   ```
   → cq_set_sync_mode timeout=30  (включи ожидание ответа)
   → cq_send_message chat_id=1 message="Привет! Я сообщение от Copilot."
   ✓ Должно вернуться с ответом (либо "Message sent", либо финальный ответ LLM)
   ```

4. **Тестирование `cq_query_db` (опционально):**
   ```
   → cq_list_projects         (получи project_id)
   → cq_query_db project_id=1 query="SELECT 1 as test"
   ✓ Должна вернуть: {"status": "success", "rows": [[1]]}
   ```

Если всё завелось — MCP tool работает готов!

---

## Security TODOs

- [ ] **Стратегия сервиса `copilot`** — считать `copilot` резидентным техническим пользователем MCP-tool. Не удалять и не пересоздавать его при обычных перезапусках. Все инструкции и runbook должны исходить из того, что пользователь уже существует.
- [ ] **Ограничение входа по источнику (`host`)** — расширить схему `users`: добавить поле `host_policy` или `host`, которое будет ограничивать допустимый источник логина для конкретного пользователя. Поддерживаемые режимы: `any`, `localhost`, `local_subnet`, `exact_ip`. Проверка должна выполняться в `/login` по `Request.client.host` до создания сессии.
- [x] **Секрет из локального файла** — `cqds_mcp_full.py` умеет читать пароль из `COLLOQUIUM_PASSWORD_FILE` или sidecar `cqds_mcp_auth.secret` (шаблон для копирования: `cqds_mcp_auth.sample.secret`), поэтому пароль можно не хранить в `mcp.json`.
- [ ] **Приоритет вариантов** — краткосрочно предпочтителен секретный файл на хосте: меньше изменений, нет миграции таблицы `users`, быстрее внедряется. Среднесрочно правильнее добавить `host`-ограничение в auth-схему, чтобы `copilot` был привязан к ожидаемому источнику доступа даже при утечке пароля.
- [ ] **Принудительная смена пароля по умолчанию** — при первом логине с паролем `devspace` возвращать предупреждение в ответе (поле `warn`), чтобы оператор знал о необходимости смены.
