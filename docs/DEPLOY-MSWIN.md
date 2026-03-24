# Colloquium DevSpace — Инструкция по развёртыванию (Windows)

Проект использует **два репозитория**:
- **Основной**: `X:\GitHub\Colloquium-DevSpace` (платформа, ядро сервера)
- **Зависимость**: `X:\GitHub\Sandwich-pack` (библиотека для анализа кода и чат-данных)

Файлы из обоих репозиториев должны быть **совмещены** перед запуском Docker Compose.

---

## Шаг 1: Подготовка структуры

### 1.1 Скопировать основной проект
```powershell
Copy-Item -Path "X:\GitHub\Colloquium-DevSpace\*" -Destination "X:\opt\docker\cqds\" -Recurse -Force
```

Результат в `X:\opt\docker\cqds\`:
- `Dockerfile`, `Dockerfile.core`, `Dockerfile.frontend`, `Dockerfile.nginx`
- `docker-compose.yml`
- `agent/` (Python-модули сервера)
- `frontend/` (Vue приложение)
- `nginx.conf`
- `data/`, `logs/`, `projects/` (пустые директории для volumes)

### 1.2 Скопировать библиотеку Sandwich-pack
```powershell
Copy-Item -Path "X:\GitHub\Sandwich-pack\src\lib" -Destination "X:\opt\docker\cqds\agent\" -Recurse -Force
Copy-Item -Path "X:\GitHub\Sandwich-pack\requirements.txt" -Destination "X:\opt\docker\cqds\agent\requirements_sandwich.txt" -Force
```

Результат: `X:\opt\docker\cqds\agent\lib\` содержит:
- `sandwich_pack.py` (основной класс)
- `content_block.py`, `code_stripper.py`
- `*_block.py` (парсеры для .rs, .py, .js, .ts, .php, .vue, .sh и т.д.)

### 1.3 Скопировать документацию
```powershell
Copy-Item -Path "X:\GitHub\Colloquium-DevSpace\docs" -Destination "X:\opt\docker\" -Recurse -Force
Copy-Item -Path "X:\GitHub\Sandwich-pack\README.md" -Destination "X:\opt\docker\docs\SANDWICH.md" -Force
```

---

## Шаг 2: Создание файлов конфигурации

### 2.1 Создать `llm_pre_prompt.md` (заглушка или из второго репо)

**Заглушка** (минимальная версия) — создана автоматически при первом развёртывании.

Если файл существует в `X:\GitHub\Colloquium-DevSpace\docs\llm_pre_prompt.md`, скопировать:
```powershell
Copy-Item -Path "X:\GitHub\Colloquium-DevSpace\docs\llm_pre_prompt.md" -Destination "X:\opt\docker\docs\" -Force
```

### 2.2 Обновить `data/mcp_config.toml` (если не существует)

Создать файл `X:\opt\docker\cqds\data\mcp_config.toml`:
```toml
[admin]
admin_ips = ["127.0.0.1", "0.0.0.0"]

[server]
host = "0.0.0.0"
port = 8080

[sandbox]
enabled = true
max_iterations = 3
```

### 2.3 Разрешить домены в `agent/server.py`

Отредактировать `X:\opt\docker\cqds\agent\server.py`:
```python
# Около строки создания FastAPI-приложения
app = FastAPI(...)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Для development; в production указать конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### 2.4 Разрешить хосты в `frontend/vite.config.js`

Отредактировать или создать `X:\opt\docker\cqds\frontend/vite.config.js`:
```javascript
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    allowedHosts: ['localhost', '127.0.0.1', 'nginx-router']
  }
})
```

---

## Шаг 3: Сборка и запуск Docker

### 3.1 Сборка образов
```powershell
cd X:\opt\docker\cqds
docker compose build --no-cache
```

**Возможные проблемы:**
- Если образ `colloquium-core` не собирается — проверить наличие `agent/lib/sandwich_pack.py`
- Если фронтенд не собирается — убедиться, что `frontend/` содержит `package.json`

### 3.2 Запуск контейнеров
```powershell
cd X:\opt\docker\cqds
docker compose up -d
```

### 3.3 Проверить статус
```powershell
cd X:\opt\docker\cqds
docker compose ps
```

Ожидаемый результат:
```
NAME              STATUS
colloquium-core   Up (health: starting)
mcp-sandbox       Up
frontend          Up
nginx-router      Up
```

---

## Шаг 4: Инициализация

### 4.1 Проверить логи ядра
```powershell
docker logs colloquium-core
```

Ищите строку:
```
INFO:uvicorn.error:Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

### 4.2 Получить пароль администратора
```powershell
cd X:\opt\docker\cqds
docker compose exec colloquium-core cat /app/logs/userman.log
```

Отфильтровать строку с `admin` в выводе (ищите `#WARN: Создан пользователь admin`).

Пример вывода:
```
WARNING:root:#WARN: Создан пользователь admin с временным паролем !qqr8rfOuPp
```

### 4.3 Проверить веб-интерфейс
- **Frontend**: `http://localhost:8008`
- **API**: `http://localhost:8008/api/chat/list`

### 4.4 Залогиниться
1. Открыть `http://localhost:8008`
2. Логин: `admin`
3. Пароль: из logs (см. 4.2)
4. Изменить пароль при первом входе

---

## Шаг 5: Работа с БД и логами

### 5.1 Редактирование БД (SQLite)
```powershell
cd X:\opt\docker\cqds
docker compose exec colloquium-core sqlite3 /app/data/colloquium.db
```

Полезные команды:
```sql
-- Добавить LLM-пользователя
INSERT INTO users (username, role, api_token) 
VALUES ('llm_agent', 'agent', 'token_xyz...');

-- Просмотр пользователей
SELECT id, username, role FROM users;
```

### 5.2 Просмотр логов
```powershell
# Логи сервера
docker logs -f colloquium-core

# Логи пула потоков (выводит последние 20 строк)
docker exec colloquium-core Get-Content -Tail 20 -Wait /app/logs/core.log

# Логи пользователей
docker exec colloquium-core Get-Content -Tail 20 -Wait /app/logs/userman.log
```

### 5.3 Включить режим репликации (отключить DEBUG)
Отредактировать `docker-compose.yml` в `X:\opt\docker\cqds\`:
```yaml
colloquium-core:
  environment:
    - DEBUG_MODE=0  # Вместо True для production
```

Затем перезапустить:
```powershell
cd X:\opt\docker\cqds
docker compose up -d
```

---

## Шаг 6: Тестирование функциональности

### 6.1 Создать первый проект
1. Залогиниться как `admin`
2. Перейти в раздел **Projects**
3. Нажать **New Project**
4. Заполнить название (e.g., `test_project`)
5. Нажать **Create**

### 6.2 Создать чат
1. Открыть проект
2. Нажать **New Chat**
3. Ввести сообщение тестовое
4. Нажать **Send**

### 6.3 Проверить добавление/удаление постов
- Добавить пост в чат (кнопка **+**)
- Удалить пост (иконка корзины)
- Если ошибки — проверить браузер **DevTools** (F12) и `logs/core.log`

### 6.4 Проверить MCP-песочницу
```powershell
docker exec mcp-sandbox ps aux
```

Если процесс не запущен или контейнер упал:
```powershell
docker logs mcp-sandbox
```

---

## Шаг 7: Интеграция с Sandwich-pack (опционально)

Если требуется анализ кода через Sandwich-pack MCP:

### 7.1 Установить сервер в контейнер
Добавить в `Dockerfile.core`:
```dockerfile
RUN pip install --no-cache-dir watchdog
COPY --from=sandwich /path/to/spack_agent.py /app/spack_agent.py
```

### 7.2 Настроить MCP-сервер
```bash
python /app/spack_agent.py --project /app/projects/[project_name]
```

---

## Проблемы и решения

| Проблема | Решение |
|----------|---------|
| `ModuleNotFoundError: No module named 'lib.sandwich_pack'` | Скопировать `lib/` из Sandwich-pack (шаг 1.2) |
| `FileNotFoundError: /app/docs/llm_pre_prompt.md` | Создать файл в `X:\opt\docker\docs\` (шаг 2.1) |
| `mcp-sandbox` упал с кодом 1 | Проверить `docker logs mcp-sandbox`; возможно нужны зависимости Rust |
| Healthcheck fails для `colloquium-core` | Сервер работает, но endpoint может быть другим; проверить `docker logs` |
| Браузер не может подключиться | Убедиться, что nginx-router слушает на порту 8008; проверить `docker compose logs nginx-router` |

---

## Переменные окружения

Редактировать в `docker-compose.yml`:

```yaml
colloquium-core:
  environment:
    - PYTHONUNBUFFERED=1      # Вывод логов в реальном времени
    - DEBUG_MODE=True          # True для development, 0 для production
    - LOG_LEVEL=DEBUG          # DEBUG, INFO, WARNING, ERROR
    - SANDBOX_ENABLED=True     # Включить MCP-песочницу
    - MAX_ITERATIONS=3         # Макс. итераций для фиксов
```

---

## Дополнительные ресурсы

- **Документация дизайна**: `X:\GitHub\Colloquium-DevSpace\README.md`
- **Sandwich-pack API**: `X:\GitHub\Sandwich-pack\README.md`
- **API Colloquium**: Смотреть комментарии в `agent/server.py`
