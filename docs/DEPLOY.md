# Colloquium DevSpace — Инструкция по развёртыванию (Linux)

## Рекомендуемый путь: автоматический деплой

Базовый и рекомендуемый сценарий: запуск скрипта автоматики из репозитория `Colloquium-DevSpace/scripts`.

```bash
cd ~/GitHub/Colloquium-DevSpace
chmod +x ./scripts/deploy-cqds.sh
TARGET_DIR=/opt/docker/cqds \
MAIN_REPO=~/GitHub/Colloquium-DevSpace \
SANDWICH_REPO=~/GitHub/Sandwich-pack \
NON_INTERACTIVE=1 \
GENERATE_PASSWORD=1 \
STOP_EXISTING=1 \
./scripts/deploy-cqds.sh
```

Что делает автоматика:
- синхронизирует файлы из двух репозиториев;
- поднимает PostgreSQL, приводит роль/пароль `cqds` к актуальному secret;
- гарантирует bootstrap-схему (создаёт таблицы, если это первый запуск без backup);
- при наличии backup может предложить restore;
- после запуска печатает фрагмент `colloquium_core.log` со строкой временного пароля `admin` (если он был сгенерирован).

Если автоматика не сработала в вашем окружении, используйте ручной сценарий ниже (эта инструкция полностью совместима как fallback).

Проект использует **два репозитория**:
- **Основной**: `/home/user/GitHub/Colloquium-DevSpace` (платформа, ядро сервера)
- **Зависимость**: `/home/user/GitHub/Sandwich-pack` (библиотека для анализа кода и чат-данных)

Файлы из обоих репозиториев должны быть **совмещены** перед запуском Docker Compose.

---

## Шаг 1: Подготовка структуры

### 1.1 Скопировать основной проект
```bash
cp -r ~/GitHub/Colloquium-DevSpace/* ~/docker/cqds/
```

Результат в `~/docker/cqds/`:
- `Dockerfile`, `Dockerfile.core`, `Dockerfile.frontend`, `Dockerfile.nginx`
- `docker-compose.yml`
- `agent/` (Python-модули сервера)
- `frontend/` (Vue приложение)
- `nginx.conf`
- `data/`, `logs/`, `projects/` (пустые директории для volumes)

### 1.2 Скопировать библиотеку Sandwich-pack
```bash
cp -r ~/GitHub/Sandwich-pack/src/lib ~/docker/cqds/agent/
cp ~/GitHub/Sandwich-pack/requirements.txt ~/docker/cqds/agent/requirements_sandwich.txt
```

Результат: `~/docker/cqds/agent/lib/` содержит:
- `sandwich_pack.py` (основной класс)
- `content_block.py`, `code_stripper.py`
- `*_block.py` (парсеры для .rs, .py, .js, .ts, .php, .vue, .sh и т.д.)

### 1.3 Скопировать документацию
```bash
cp -r ~/GitHub/Colloquium-DevSpace/docs ~/docker/
cp ~/GitHub/Sandwich-pack/README.md ~/docker/docs/SANDWICH.md
```

---

## Шаг 2: Создание файлов конфигурации

### 2.1 Создать `llm_pre_prompt.md` (заглушка или из второго репо)

**Заглушка** (минимальная версия) — создана автоматически при первом развёртывании.

Если файл существует в `~/GitHub/Colloquium-DevSpace/docs/llm_pre_prompt.md`, скопировать:
```bash
cp ~/GitHub/Colloquium-DevSpace/docs/llm_pre_prompt.md ~/docker/docs/
```

Или создать вручную:
```bash
mkdir -p ~/docker/docs
cat > ~/docker/docs/llm_pre_prompt.md << 'EOF'
# LLM Pre-Prompt для Colloquium DevSpace

Вы ассистент для интерактивной разработки кода с LLM-агентами.

## Роли агентов:
- **Генератор**: Создаёт патчи и улучшения кода
- **Тестировщик**: Проверяет компиляцию и тесты
- **Оптимизатор**: Улучшает производительность

## Команды:
- `@agent code improvement` — генерация патча
- `@agent code test` — запуск тестов
- `@agent commit` — создание коммита

## Контекст:
Изолированная Rust-окружение через Docker с поддержкой Git.
EOF
```

### 2.2 Обновить `data/mcp_config.toml` (если не существует)

Создать файл `~/docker/cqds/data/mcp_config.toml`:
```bash
mkdir -p ~/docker/cqds/data
cat > ~/docker/cqds/data/mcp_config.toml << 'EOF'
[admin]
admin_ips = ["127.0.0.1", "0.0.0.0"]

[server]
host = "0.0.0.0"
port = 8080

[sandbox]
enabled = true
max_iterations = 3
EOF
```

### 2.3 Разрешить домены в `agent/server.py`

Отредактировать `~/docker/cqds/agent/server.py`:
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

Отредактировать или создать `~/docker/cqds/frontend/vite.config.js`:
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
```bash
cd ~/docker/cqds
docker compose build --no-cache
```

**Возможные проблемы:**
- Если образ `colloquium-core` не собирается — проверить наличие `agent/lib/sandwich_pack.py`
- Если фронтенд не собирается — убедиться, что `frontend/` содержит `package.json`

### 3.2 Запуск контейнеров
```bash
cd ~/docker/cqds
docker compose up -d
```

### 3.3 Проверить статус
```bash
cd ~/docker/cqds
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
```bash
docker logs colloquium-core
```

Ищите строку:
```
INFO:uvicorn.error:Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

### 4.2 Получить пароль администратора

Если запускали автоматический скрипт, этот фрагмент уже выводится в конце деплоя.
Ниже команда для ручной проверки:

```bash
cd ~/docker/cqds
docker compose exec colloquium-core cat /app/logs/userman.log | grep admin
```

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

### 5.0 PostgreSQL backup (рекомендуемый путь)

После миграции с SQLite рабочая БД находится в PostgreSQL (`cqds-postgres`).
Для регулярного бэкапа используйте скрипт внутри `colloquium-core`:

```bash
cd ~/docker/cqds
docker compose exec colloquium-core sh /app/postgres/backup_postgres.sh
```

По умолчанию дамп сохраняется в:
`/app/data/backups/pg/<db>_YYYYMMDD_HHMMSS.dump`

Опции:
```bash
# кастомная папка для дампов
docker compose exec -e BACKUP_DIR=/app/data/backups/pg/nightly colloquium-core sh /app/postgres/backup_postgres.sh

# авто-очистка старых дампов (например, хранить 14 дней)
docker compose exec -e RETENTION_DAYS=14 colloquium-core sh /app/postgres/backup_postgres.sh
```

Скрипт читает пароль в приоритете из `PGPASSWORD`, иначе из `PGPASSWORD_FILE`
(`./secrets/cqds_db_password` монтируется как `/run/secrets/cqds_db_password`).

Рекомендуемая последовательность backup:
1. Проверить, что `colloquium-core` и `cqds-postgres` в статусе `healthy`.
2. Выполнить дамп скриптом из `colloquium-core`.
3. Убедиться, что файл создан и `pg_restore --list` проходит без ошибок (скрипт делает это автоматически).
4. Скопировать дамп во внешнее хранилище (off-host).
5. Периодически делать тестовый restore на отдельной БД/контейнере.

Пример restore в пустую БД:
```bash
docker compose exec cqds-postgres psql -U postgres -d postgres -c "CREATE DATABASE cqds_restore OWNER cqds;"
docker compose exec cqds-postgres pg_restore -U cqds -d cqds_restore /app/data/backups/pg/cqds_YYYYMMDD_HHMMSS.dump
```

### 5.1 Редактирование БД (SQLite)
```bash
cd ~/docker/cqds
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
```bash
# Логи сервера
docker logs -f colloquium-core

# Логи пула потоков
docker compose exec colloquium-core tail -f /app/logs/core.log

# Логи пользователей
docker compose exec colloquium-core tail -f /app/logs/userman.log
```

### 5.3 Включить режим репликации (отключить DEBUG)
Отредактировать `docker-compose.yml` в `~/docker/cqds/`:
```yaml
colloquium-core:
  environment:
    - DEBUG_MODE=0  # Вместо True для production
```

Затем перезапустить:
```bash
cd ~/docker/cqds
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
```bash
docker exec mcp-sandbox ps aux
```

Если процесс не запущен или контейнер упал:
```bash
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

## Шаг 8: Остановка и удаление контейнеров

### 8.1 Остановить контейнеры
```bash
cd ~/docker/cqds
docker compose down
```

### 8.2 Удалить образы
```bash
docker compose down --rmi all
```

### 8.3 Очистить volumes (осторожно — теряются данные)
```bash
docker compose down -v
```

---

## Проблемы и решения

| Проблема | Решение |
|----------|---------|
| `ModuleNotFoundError: No module named 'lib.sandwich_pack'` | Скопировать `lib/` из Sandwich-pack (шаг 1.2) |
| `FileNotFoundError: /app/docs/llm_pre_prompt.md` | Создать файл в `~/docker/docs/` (шаг 2.1) |
| `mcp-sandbox` упал с кодом 1 | Проверить `docker logs mcp-sandbox`; возможно нужны зависимости Rust |
| Healthcheck fails для `colloquium-core` | Сервер работает, но endpoint может быть другим; проверить `docker logs` |
| Браузер не может подключиться | Убедиться, что nginx-router слушает на порту 8008; проверить `docker compose logs nginx-router` |
| Ошибка прав доступа при копировании | Использовать `sudo cp` или запустить от пользователя с нужными правами |

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

## Важные пути на Linux

- **Репозитории**: `~/GitHub/Colloquium-DevSpace` и `~/GitHub/Sandwich-pack`
- **Рабочая директория Docker**: `~/docker/cqds`
- **Документация**: `~/docker/docs/`
- **Логи Docker**: `/var/lib/docker/containers/`

---

## Дополнительные ресурсы

- **Документация дизайна**: `~/GitHub/Colloquium-DevSpace/README.md`
- **Sandwich-pack API**: `~/GitHub/Sandwich-pack/README.md`
- **API Colloquium**: Смотреть комментарии в `agent/server.py`
- **Docker документация**: https://docs.docker.com/

---

## Советы для Linux

- **Быстрая проверка портов**: `netstat -tulpn | grep 8008`
- **Разрешение Docker для пользователя**: `sudo usermod -aG docker $USER` (требует перезагрузки)
- **Проверка дискового пространства**: `df -h` или `docker system df`
- **Очистка неиспользуемых образов**: `docker image prune -a`
