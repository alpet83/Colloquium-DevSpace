# cqds_mcp_mini — компактный MCP для Colloquium-DevSpace

Скрипт `cqds_mcp_mini.py` в каталоге `mcp-tools/` поднимает stdio MCP-сервер с **усечённой презентацией** инструментов: вместо десятков отдельных вызовов (`cq_list_files`, `cq_read_file`, …) клиент видит **семь агрегаторов** `*_ctl` плюс **`cq_help`**. Под капотом те же обработчики HTTP API, что и у полного сервера [`cqds_mcp_full.py`](cqds_mcp_full.md).

---

## Зачем mini, если есть full

| | `cqds_mcp_mini` | `cqds_mcp_full` |
|---|-----------------|-----------------|
| Число инструментов в `list_tools` | **7** | **42** (типично) |
| Объём JSON описаний (оценка) | **~5× меньше** | база |
| Модель видит | короткие имена + батчи `requests[]` | длинный перечень с детальными схемами |

Итог: **меньше токенов на системный промпт и на ответ `tools/list`**, проще удерживать рабочий контекст в длинных сессиях агента.

Инструмент **`cq_help`** отдаёт справочник по действиям (`tool_ref` вида `cq_files_ctl#read_file` и т.п.); модель может подгружать детали по мере необходимости.

---

## Что входит в поверхность mini

Семь MCP-инструментов (имена фиксированы в коде):

| Инструмент | Назначение |
|------------|------------|
| `cq_help` | Каталог и мануалы по `*_ctl` и действиям (`tool_ref`, опционально `#action`). |
| `cq_chat_ctl` | Чаты: список, создание, сообщения, история, sync-mode, статистика и связанные сценарии. |
| `cq_project_ctl` | Проекты: список, выбор, статус, индекс, сущности. |
| `cq_files_ctl` | Файлы и поиск: list/read/replace, патч, grep, индекс, логи и др. (через поле `action`). |
| `cq_exec_ctl` | Выполнение команд в окружении проекта на стороне CQDS. |
| `cq_process_ctl` | Долгоживущие процессы (spawn / io / wait / …) через MCP-sandbox. |
| `cq_docker_ctl` | Управление compose CQDS с хоста MCP (`status`, `restart`, …). |

Пакетные сценарии: у агрегаторов `*_ctl` предусмотрены вызовы с массивом **`requests[]`** — предпочитай их, когда шагов несколько (см. подсказки в описании `cq_help`).

**Нет в mini (есть только в full):** отдельные инструменты `cq_host_process_*`, прямой `cq_query_db`, `cq_spawn_script`, тонкая нарезка docker-хелперов (`cq_docker_control` vs `cq_docker_ctl`) и т.д. Если нужен именно такой контур — подключай [`cqds_mcp_full.py`](cqds_mcp_full.md).

---

## Предварительные требования

Те же, что для полного сервера:

- Python с пакетами `httpx` и `mcp` (см. раздел в [cqds_mcp_full.md](cqds_mcp_full.md#1-python-зависимости)).
- Запущенный Colloquium-DevSpace (`docker compose up -d`, API на `http://localhost:8008`).
- Сервисный пользователь **`copilot`** (создание и проверка — в [cqds_mcp_full.md](cqds_mcp_full.md#сервисный-пользователь-copilot)).

---

## Настройка `mcp.json`

Расположение конфигов и ключи JSON — как в [cqds_mcp_full.md](cqds_mcp_full.md#настройка-mcpjson): VS Code — `.vscode/mcp.json` (`servers`), Cursor — `.cursor/mcp.json` (`mcpServers`), для stdio нужен `"type": "stdio"`.

### Пример для Cursor (фрагмент `mcpServers`)

Путь к Python и к скрипту замени на свои; для Windows удобно `P:\\opt\\docker\\cqds\\mcp-tools\\cqds_mcp_mini.py`.

```json
"cqds_mcp_mini": {
  "command": "C:\\Apps\\Python3\\python.exe",
  "args": [
    "P:\\opt\\docker\\cqds\\mcp-tools\\cqds_mcp_mini.py",
    "--url", "http://localhost:8008",
    "--username", "copilot"
  ],
  "type": "stdio",
  "env": {
    "MCP_AUTH_TOKEN": "Grok-xAI-Agent-The-Best",
    "MCP_HOST_REMAP": "nginx-router=localhost",
    "CQDS_BASE_URL": "http://localhost:8008",
    "COLLOQUIUM_PASSWORD": "devspace"
  }
}
```

**Пароль:** `cqds_mcp_mini` на старте читает **`--password`** и переменную **`COLLOQUIUM_PASSWORD`** (по умолчанию в коде — строка `devspace`). Переменная **`COLLOQUIUM_PASSWORD_FILE` и sidecar `cqds_mcp_auth.secret` этим процессом не подхватываются** (в отличие от `cqds_mcp_full.py`). Варианты:

- задать пароль в `env` сервера (не коммитить реальные значения);
- либо использовать полный сервер [`cqds_mcp_full`](cqds_mcp_full.md): там sidecar из **`cqds_mcp_auth.secret`**, создаваемый по шаблону **`cqds_mcp_auth.sample.secret`**.

---

## Логи и отладка

- По умолчанию задаётся **`COLLOQUIUM_MCP_LOG_STEM=cqds_mcp_mini`** — журналы рядом с `mcp-tools/logs/`.
- Переопределение: **`COLLOQUIUM_MCP_LOG_FILE`**, **`COLLOQUIUM_MCP_LOG_LEVEL`** — как для full (см. [cqds_mcp_full.md](cqds_mcp_full.md#параметры-командной-строки)).

Опционально включается **heartbeat активных проектов** на хосте (переменные `CQDS_MCP_PROJECT_HEARTBEAT`, `CQDS_MCP_HEARTBEAT_PROJECTS_DIR`, наличие `docker` в `PATH`) — см. модуль `cq_runtime_host_heartbeat` и предупреждения в stderr при старте.

---

## Параметры командной строки

```
--url        URL Colloquium-DevSpace (default: http://localhost:8008 или COLLOQUIUM_URL)
--username   Пользователь API (default: copilot или COLLOQUIUM_USERNAME)
--password   Пароль (default: devspace или COLLOQUIUM_PASSWORD)
```

Проверка вручную: запуск без клиента MCP должен «зависнуть» на stdio — как для full (см. [cqds_mcp_full.md](cqds_mcp_full.md#проверка-работоспособности)).

---

## Сравнение объёмов (локальная проверка)

Из корня `mcp-tools/`:

```powershell
python.exe scripts\compare_mcp_tool_surface.py
```

В выводе — число инструментов, размер JSON описаний и соотношение full/mini.

---

## Связанные документы

- [cqds_mcp_full.md](cqds_mcp_full.md) — полный сервер, секреты, токен MCP, Docker, security notes.
- [MCP_DELEGATION_QUICK_START.md](MCP_DELEGATION_QUICK_START.md) и соседние файлы — стратегия делегирования инструментов в агентах.
