# copilot_mcp_tool — Интеграция GitHub Copilot с Colloquium-DevSpace

MCP-сервер (`copilot_mcp_tool.py`) транслирует инструменты GitHub Copilot Agent в HTTP-запросы к Colloquium-DevSpace, позволяя Copilot читать чаты, отправлять сообщения и управлять файлами проекта через Colloquium.

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

В `.vscode/mcp.json` вашего проекта добавь секцию `colloquium`:

```json
"colloquium": {
  "command": "X:\\Python3\\python.exe",
  "args": [
    "X:\\docker\\cqds\\copilot_mcp_tool.py",
    "--url",  "http://localhost:8008",
    "--username", "copilot"
  ],
  "type": "stdio"
}
```

Предпочтительный локальный вариант: пароль хранится в отдельном файле рядом с `copilot_mcp_tool.py`
или в другом защищённом месте на хосте.

### Рекомендуемый вариант: sidecar secret

Создай файл `X:\docker\cqds\copilot_mcp_tool.secret`, в котором находится только пароль, без JSON и без лишних строк:

```text
мой_пароль
```

После этого `copilot_mcp_tool.py` подхватит пароль автоматически, даже если в `mcp.json` пароль не указан.

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

> Не коммить пароль в git. Предпочтительный вариант теперь — отдельный локальный файл секрета.

### Приоритет источников пароля

`copilot_mcp_tool.py` ищет пароль в таком порядке:

1. `--password`
2. `--password-file`
3. `COLLOQUIUM_PASSWORD`
4. `COLLOQUIUM_PASSWORD_FILE`
5. `copilot_mcp_tool.secret` рядом со скриптом
6. fallback: `devspace`

При старте MCP-tool пишет в stderr диагностическую строку с источником пароля и коротким preview:

```text
MCP auth password source: copilot_mcp_tool.secret; preview=tE...
```

Это сделано для быстрой диагностики двух случаев:

- tool неожиданно взял `default` и работает на `devspace`
- tool читает не тот секретный файл, который ожидался

Preview намеренно показывает только первые 2 символа, а не полный пароль.

Альтернатива для локальной машины: дать `copilot_mcp_tool.py` возможность читать пароль из локального секрета рядом с размещением,
например из файла вида `copilot_mcp_tool.secret` или через `COLLOQUIUM_PASSWORD_FILE`.

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
| `cq_edit_file` | Записать файл через `<code_file>` (создать или перезаписать) |
| `cq_patch_file` | Применить unified-diff через `<patch>` |
| `cq_undo_file` | Откатить файл к бэкапу через `<undo>` |
| `cq_list_projects` | Список проектов, зарегистрированных в Colloquium |
| `cq_select_project` | Выбрать активный проект в сессии |
| `cq_list_files` | Лёгкий индекс файлов проекта (без контента) |
| `cq_get_index` | Получить rich-index чата/проекта |
| `cq_get_code_index` | Сборка rich-index проекта по требованию |
| `cq_read_file` | Прочитать файл по DB `file_id` |
| `cq_exec` | Выполнить shell-команду в проекте |
| `cq_query_db` | Выполнить read-only SQL через backend DB layer (debug) |
| `cq_set_sync_mode` | Включить/выключить синхронный режим для `cq_send_message` |
| `cq_smart_grep` | Поиск по наборам файлов (code/logs/docs/all) |
| `cq_grep_logs` | Сканирование одного/нескольких log-файлов по маскам с regex-фильтрацией |
| `cq_replace` | Точный replace в файле по `file_id` |
| `cq_process_spawn` | Запустить subprocess в mcp-sandbox (возвращает `process_guid`) |
| `cq_process_io` | Читать/писать stdin/stdout/stderr процесса по `process_guid` |
| `cq_process_status` | Статус процесса + runtime/cpu метрики |
| `cq_process_list` | Список процессов (опционально по `project_id`) |
| `cq_process_wait` | Ожидание условия процесса (`any_output`/`finished`) |
| `cq_process_kill` | Отправить сигнал процессу (`SIGTERM`/`SIGKILL`) |

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
По умолчанию runtime-лог пишется в ./logs/copilot_mcp_tool.runtime.log
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
python.exe X:\docker\cqds\copilot_mcp_tool.py `
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
- [x] **Секрет из локального файла** — `copilot_mcp_tool.py` умеет читать пароль из `COLLOQUIUM_PASSWORD_FILE` или sidecar-файла `copilot_mcp_tool.secret`, поэтому пароль можно не хранить в `mcp.json`.
- [ ] **Приоритет вариантов** — краткосрочно предпочтителен секретный файл на хосте: меньше изменений, нет миграции таблицы `users`, быстрее внедряется. Среднесрочно правильнее добавить `host`-ограничение в auth-схему, чтобы `copilot` был привязан к ожидаемому источнику доступа даже при утечке пароля.
- [ ] **Принудительная смена пароля по умолчанию** — при первом логине с паролем `devspace` возвращать предупреждение в ответе (поле `warn`), чтобы оператор знал о необходимости смены.
