# copilot_mcp_tool — Интеграция GitHub Copilot с Colloquium-DevSpace

MCP-сервер (`copilot_mcp_tool.py`) транслирует инструменты GitHub Copilot Agent в HTTP-запросы к Colloquium-DevSpace, позволяя Copilot читать чаты, отправлять сообщения и управлять файлами проекта через Colloquium.

---

## Предварительные требования

### 1. Python-зависимости

Пакеты должны быть установлены в том же Python, что указан в `mcp.json`:

```powershell
C:\Apps\Python3\python.exe -m pip install httpx mcp
```

Проверка:
```powershell
C:\Apps\Python3\python.exe -c "import httpx, mcp; print('ok')"
```

### 2. Запущенный Colloquium-DevSpace

```powershell
cd P:\GitHub\Colloquium-DevSpace\src
docker compose up -d
```

После запуска API доступно на `http://localhost:8008`.

> **Маршрутизация через nginx**: все API-вызовы проходят через nginx (порт 8008).
> nginx маршрутизирует `/api/*` → `colloquium-core:8080`, а `/` → frontend.
> MCP-инструмент использует префикс `/api/` внутри — отдельно пробрасывать порт backend **не нужно**.

> **Auth во frontend**: Login-форма предназначена для **человека** — разные пользователи
> (admin, разработчики) работают со своими сессиями в браузере.
> MCP-инструмент использует тот же механизм аутентификации программно — это правильная архитектура.

На первом запуске автоматически создаётся пользователь `admin` с **случайным паролем** — он выводится в лог контейнера:

```powershell
docker logs colloquium-core 2>&1 | Select-String "admin"
# Ищи строку вида: Создан пользователь admin с временным паролем !Xk9...
```

---

## Создание пользователя `copilot`

Административный скрипт `agent/create_user.py` уже примонтирован в контейнер как `/app/agent/create_user.py`.

```powershell
# Создать пользователя copilot с паролем devspace
docker exec colloquium-core python3 /app/agent/create_user.py copilot devspace

# Создать с другим паролем
docker exec colloquium-core python3 /app/agent/create_user.py copilot мой_пароль

# Список всех пользователей
docker exec colloquium-core python3 /app/agent/create_user.py --list

# Удалить пользователя
docker exec colloquium-core python3 /app/agent/create_user.py --delete copilot
```

Смена пароля — удали и создай заново:
```powershell
docker exec colloquium-core python3 /app/agent/create_user.py --delete copilot
docker exec colloquium-core python3 /app/agent/create_user.py copilot новый_пароль
```

---

## Настройка mcp.json

В `.vscode/mcp.json` вашего проекта добавь секцию `colloquium`:

```json
"colloquium": {
  "command": "C:\\Apps\\Python3\\python.exe",
  "args": [
    "P:\\GitHub\\Colloquium-DevSpace\\src\\copilot_mcp_tool.py",
    "--url",  "http://localhost:8008",
    "--username", "copilot"
  ],
  "type": "stdio"
}
```

Пароль по умолчанию `devspace`. Чтобы задать другой — через аргумент или переменную окружения:

```json
"args": [..., "--password", "мой_пароль"]
```

или через `env`:

```json
"env": { "COLLOQUIUM_PASSWORD": "мой_пароль" }
```

> Не коммить пароль в git. Предпочтительный вариант — `env` с системной переменной окружения.

---

## Доступные инструменты

### Чаты и сообщения

| Инструмент | Описание |
|---|---|
| `cq_list_chats` | Список всех чатов |
| `cq_create_chat` | Создать новый чат (вернёт `chat_id`) |
| `cq_send_message` | Отправить сообщение в чат (в sync-режиме — ждёт ответ) |
| `cq_wait_reply` | Long-poll ответа AI (до 15 с) |
| `cq_get_history` | Снapshot истории чата без ожидания |
| `cq_set_sync_mode` | Включить синхронный режим: `cq_send_message` будет ждать ответ AI до N секунд |

Рекомендация по режимам работы:
- Асинхронный режим (`cq_set_sync_mode(timeout=0)`) удобен для долгих задач LLM/агента, когда можно отправить запрос и переключиться на другие подзадачи.
- Асинхронный режим также лучше подходит для параллельного запуска задач в нескольких чатах.
- Синхронный режим удобен для коротких итераций «вопрос → ответ → следующее действие» в одном активном чате.

### Файлы и патчи (через chat-цикл)

| Инструмент | Описание |
|---|---|
| `cq_edit_file` | Записать файл через `<code_file>` (создать или перезаписать) |
| `cq_patch_file` | Применить unified-diff через `<patch>` |
| `cq_undo_file` | Откатить файл к бэкапу через `<undo>` |

### Прямые операции (без LLM/chat-цикла)

| Инструмент | Описание |
|---|---|
| `cq_read_file` | Прочитать содержимое файла по DB `file_id` — один HTTP-запрос |
| `cq_exec` | Выполнить shell-команду в рабочей директории проекта — результат сразу |
| `cq_smart_grep` | Поиск по проекту в преднастроенных наборах файлов (code/logs/docs/all) |
| `cq_replace` | Точечная замена в одном файле по `file_id` (plain/regex) |

Параметры фокусировки для `cq_smart_grep`:
- `profile`: преднастроенный профиль выборки `all | backend | frontend | docs | infra | tests | logs`.
- `time_strict`: фильтр по времени в компактном SQL-like формате.

Поддерживаемый синтаксис `time_strict`:
- `<field><op><value>`, где `field`: `mtime | ctime | ts`
- `op`: `>`, `>=`, `<`, `<=`, `=`
- `value`: `YYYY-MM-DD`, `YYYY-MM-DD HH:MM[:SS]` или unix timestamp

Примеры:
- `mtime>2026-03-25`
- `mtime>=2026-03-25 21:00`
- `ctime>1711390800`

Примечание:
- `mtime` и `ts` — самые быстрые, потому что используют метку времени из индекса файлов.
- `ctime` может быть медленнее (нужен файловый stat для каждого кандидата).

### Проекты и индексы

| Инструмент | Описание |
|---|---|
| `cq_list_projects` | Список проектов, зарегистрированных в Colloquium |
| `cq_select_project` | Установить активный проект сессии (нужно после рестарта) |
| `cq_list_files` | Индекс файлов проекта (id, имя, метка времени) без содержимого |
| `cq_get_index` | Граф сущностей (функции/классы) из последнего LLM-контекста чата |
| `cq_get_code_index` | Граф сущностей проекта on-demand, без обращения к LLM |

### Типичные рабочие циклы

**Полный LLM-цикл (исходный):**
```
1. cq_list_chats           → найти chat_id (или cq_create_chat)
2. cq_send_message         → отправить задачу
3. cq_wait_reply           → получить ответ AI
4. cq_edit_file / cq_patch_file → применить изменения
```

**Синхронный режим (экономия одного вызова):**
```
1. cq_set_sync_mode(timeout=60)  → включить один раз
2. cq_send_message               → отправить + дождаться ответа автоматически
3. cq_edit_file / cq_patch_file  → применить изменения
```

**Прямое чтение/запуск (без LLM вообще):**
```
cq_list_files(project_id)        → получить file_id
cq_read_file(file_id)            → прочитать файл напрямую
cq_exec(project_id, "pytest -q") → запустить тесты напрямую
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

## Параметры командной строки

```
--url        URL Colloquium-DevSpace  (default: http://localhost:8008)
             env: COLLOQUIUM_URL
--username   Имя пользователя         (default: copilot)
             env: COLLOQUIUM_USERNAME
--password   Пароль                   (default: devspace)
             env: COLLOQUIUM_PASSWORD
--chat-id    Чат по умолчанию (инфо) (default: 0)
             env: COLLOQUIUM_CHAT_ID
```

---

## Проверка работоспособности

Запусти сервер вручную — если стартует без ошибок, конфигурация верна:

```powershell
C:\Apps\Python3\python.exe P:\GitHub\Colloquium-DevSpace\src\copilot_mcp_tool.py `
  --url http://localhost:8008 --username copilot --password devspace
# Должен зависнуть (ожидает stdin от MCP-клиента) — Ctrl+C для выхода
```

Проверь, что логин работает:

```powershell
Invoke-RestMethod -Method POST -Uri "http://localhost:8008/api/login" `
  -ContentType "application/json" `
  -Body '{"username":"copilot","password":"devspace"}'
# Ожидаемый ответ: объект с полями role, user_id и т.п.
```

---

## Security TODOs

- [ ] **Ограничение авторизации по IP** — добавить в ядро backend (`server.py` / FastAPI middleware) проверку, что запросы на `/api/login` принимаются только с `127.0.0.1` или из доверенной подсети (например, `172.16.0.0/12` для Docker). Это защитит от ситуации, когда пользователь оставил стандартный пароль (`devspace`) и сервер случайно оказался доступен извне. Вариант реализации — middleware на уровне nginx (`allow 127.0.0.1; allow 172.0.0.0/8; deny all;`) или FastAPI `Request.client.host` check для эндпоинта `/login`.
- [ ] **Принудительная смена пароля по умолчанию** — при первом логине с паролем `devspace` возвращать предупреждение в ответе (поле `warn`), чтобы оператор знал о необходимости смены.
