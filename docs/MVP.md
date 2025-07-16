# /docs/MVP.md, created 2025-07-16 16:09 EEST

# Документация по функциональности платформы (MVP)

## Введение

Платформа представляет собой многопользовательский чат для совместной разработки, анализа кода и взаимодействия с LLM (Large Language Model). Минимально жизнеспособный продукт (MVP) включает функциональность для управления чатами, файлами, проектами и репликацией данных в LLM, а также базовый интерфейс для взаимодействия пользователей. Ниже описаны все возможности платформы в её текущем виде на 16 июля 2025 года.

## 1. Общая архитектура

Платформа состоит из фронтенда (Vue.js с Pinia) и бэкенда (FastAPI с SQLite). Фронтенд предоставляет интерфейс для работы с чатами, файлами и проектами, а бэкенд обрабатывает запросы, управляет базой данных и взаимодействует с LLM через `replication.py`. Основные компоненты:
- **База данных**: SQLite (`/app/data/multichat.db`) для хранения пользователей, чатов, постов, файлов и сессий.
- **Фронтенд**: Vue.js приложение с тремя панелями: левая (чаты), центральная (сообщения), правая (файлы и проекты).
- **Бэкенд**: FastAPI сервер с маршрутами для авторизации, чатов, постов, файлов и статистики.
- **Репликация**: `ReplicationManager` для передачи контекста (посты и файлы) в LLM.

## 2. Функциональность фронтенда

### 2.1. Авторизация
- **Вход**:
  - Пользователи входят через `/login` с именем и паролем (например, `admin`/`colloquium`).
  - Хранится сессия через cookies (`session_id`).
  - Реализовано в `Login.vue` и `auth.js` (`useAuthStore`).
- **Выход**:
  - Выход через `/logout`, очищает сессию и состояние Pinia.
- **Проверка сессии**:
  - Автоматическая проверка сессии при загрузке через `checkSession` в `auth.js`.

### 2.2. Интерфейс
- **Трёхпанельный вид** (`App.vue`):
  - **Левая панель** (`SideBar.vue`): Список чатов, выбор чата, статистика токенов (отображается внизу: `Токенов: <число>`).
  - **Центральная панель** (`ChatContainer.vue`): История сообщений, отправка сообщений, модальные окна для создания чата, редактирования сообщений и загрузки файлов.
  - **Правая панель** (`RightPanel.vue`): Дерево файлов (`FileTree.vue`, `FileManager.vue`), управление проектами (создание, редактирование).
- **Сворачивание панелей**:
  - Левая и правая панели сворачиваются до 30px (`▶`/`◄`).
- **Тёмная и светлая темы**:
  - Поддержка через `prefers-color-scheme` (тёмная: `#333`, светлая: `#f0f0f0`).

### 2.3. Управление чатами
- **Список чатов**:
  - Отображается в `SideBar.vue` через `/api/chat/list`.
  - Дерево чатов строится в `ChatTree.vue` с поддержкой вложенных чатов (через `parent_msg_id`).
- **Создание чата**:
  - Через модальное окно в `ChatContainer.vue` (`/api/chat/create`).
  - Поддерживает указание `parent_msg_id` для ветвления.
- **Удаление чата**:
  - Через `/api/chat/delete`, доступно только для владельца или админа.
- **Выбор чата**:
  - Выбор через `selectChat` в `SideBar.vue`, обновляет `selectedChatId` в `chat.js`.

### 2.4. Управление сообщениями
- **Отправка сообщений**:
  - Через поле ввода в `ChatContainer.vue`, отправка по Enter (`/api/chat/post`).
  - Поддержка тегов `@attach#<file_id>` и `@attach_dir#<dir_name>`.
- **Редактирование сообщений**:
  - Через модальное окно (`editPostModal`), доступно для автора или админа (`/api/chat/edit_post`).
- **Удаление сообщений**:
  - Через кнопку "X", доступно для автора или админа (`/api/chat/delete_post`).
- **Форматирование**:
  - Сообщения форматируются с указанием времени и имени пользователя.
  - Тег `@attach#<file_id>` заменяется на `File: <file_name> (@attached_file#<file_id>, <дата>)`.

### 2.5. Управление файлами
- **Дерево файлов**:
  - Отображается в `RightPanel.vue` через `FileTree.vue` и `FileManager.vue`.
  - Файлы загружаются через `/api/chat/list_files` (с фильтром по `project_id`).
- **Загрузка файлов**:
  - Через `<input type="file">` в `ChatContainer.vue`, с подтверждением имени файла (`/api/chat/upload_file`).
  - Добавляет тег `@attach#<file_id>` к сообщению.
- **Обновление файлов**:
  - Через `FileManager.vue` (`/api/chat/update_file`).
- **Удаление файлов**:
  - Через `FileManager.vue` (`/api/chat/delete_file`), доступно только админам.
- **Поддержка директорий**:
  - Тег `@attach_dir#<dir_name>` (например, `@attach_dir#trade_report/src`) разворачивается в `@attached_files#[<file_id1>,<file_id2>,...]` для всех файлов, начинающихся с `dir_name`.

### 2.6. Статистика
- **Токены чата**:
  - Отображаются внизу левой панели (`SideBar.vue`) через `/api/chat/get_stats`.
  - Показывает количество токенов последнего контекста (`last_sent_tokens` из `ReplicationManager`).

## 3. Функциональность бэкенда

### 3.1. Маршруты API
- **Авторизация** (`auth_routes.py`):
  - `POST /login`: Аутентификация пользователя, возвращает `session_id`.
  - `POST /logout`: Завершение сессии.
  - `GET /user/info`: Информация о пользователе (роль, ID).
- **Чаты** (`chat_routes.py`):
  - `GET /chat/list`: Список чатов пользователя.
  - `POST /chat/create`: Создание чата.
  - `POST /chat/delete`: Удаление чата.
  - `GET /chat/get_stats`: Статистика чата (токены).
- **Сообщения** (`post_routes.py`):
  - `GET /chat/get`: История сообщений чата.
  - `POST /chat/post`: Отправка сообщения.
  - `POST /chat/edit_post`: Редактирование сообщения.
  - `POST /chat/delete_post`: Удаление сообщения.
- **Файлы** (`file_routes.py`):
  - `GET /chat/list_files`: Список файлов (с фильтром по `project_id`).
  - `POST /chat/upload_file`: Загрузка файла.
  - `POST /chat/update_file`: Обновление файла.
  - `POST /chat/delete_file`: Удаление файла.
- **Проекты** (`project.py`):
  - `GET /project/list`: Список проектов.
  - `POST /project/create`: Создание проекта.
  - `POST /project/update`: Обновление проекта.

### 3.2. Репликация
- **ReplicationManager** (`replication.py`):
  - Собирает посты и файлы для контекста LLM.
  - Поддерживает теги `@attach#<file_id>` и `@attach_dir#<dir_name>`.
  - Ограничивает контекст до 131072 токенов.
  - Сохраняет контекст в `/app/logs/context-<username>.log`.
  - Ошибки LLM сохраняются в `posts` (user_id=2, `mcp`).
- **SandwichPack** (`sandwich_pack.py`):
  - Формирует индекс `files` с уникальными `file_id` (использует `busy_ids` для избежания конфликтов).
  - Упаковывает контент в сэндвичи с метаданными (посты, файлы, пользователи).
- **Логирование**:
  - Логи в `/app/logs/colloquium_core.log` для отладки запросов, ошибок и репликации.

### 3.3. База данных
- **SQLite** (`/app/data/multichat.db`):
  - Таблицы: `users`, `sessions`, `chats`, `posts`, `attached_files`, `projects`, `llm_context`, `llm_responses`.
  - Управление через `db.py` (синглтон `Database.get_database()`).

## 4. Возможности для пользователей
- **Роли пользователей**:
  - **admin**: Полный доступ (удаление любых сообщений, файлов, чатов).
  - **developer**: Создание/редактирование своих сообщений, чатов, загрузка файлов.
  - **LLM**: Автоматические ответы от LLM (например, `@grok`).
  - **mcp**: Системные сообщения об ошибках LLM (user_id=2).
- **Поддерживаемые типы файлов**:
  - Код: `.rs`, `.vue`, `.js`, `.py`.
  - Документы: `.rulz` (например, `CLA rules for LLM.rulz`).
- **Интеграция с LLM**:
  - Отправка контекста (посты, файлы) через `XAIConnection` или `OpenAIConnection`.
  - Поддержка тегов `@grok`, `@all`, `#critics_allowed` для активации ответов LLM.
- **Ограничения**:
  - Максимум 131072 токена на контекст.
  - Поддержка до 100 файлов через `@attach_dir`.

## 5. Ограничения и будущие улучшения
- **Ограничения**:
  - `last_sent_tokens` хранит токены только последнего контекста, не сохраняя историю по чатам.
  - Нет фильтрации файлов по сложным критериям (например, по дате или размеру).
  - Нет поддержки загрузки ZIP или `git clone`.
  - Интеграция с `llm_hands` отсутствует.
- **Будущие улучшения**:
  - Добавить фильтрацию файлов в `RightPanel.vue`.
  - Реализовать загрузку ZIP-архивов.
  - Добавить поддержку `git clone` для проектов.
  - Интегрировать `llm_hands` для расширенного анализа кода.

## 6. Технические детали
- **Фронтенд**:
  - Vue 3, Pinia (сторы: `auth.js`, `chat.js`, `files.js`).
  - Папка: `/opt/docker/mcp-server/frontend/rtm`.
  - Компоненты: `App.vue`, `SideBar.vue`, `ChatContainer.vue`, `RightPanel.vue`, `Login.vue`, `ChatTree.vue`, `FileTree.vue`, `FileManager.vue`.
- **Бэкенд**:
  - FastAPI, SQLite.
  - Папка: `/app/agent`.
  - Основные модули: `replication.py`, `sandwich_pack.py`, `db.py`, `chat_routes.py`, `post_routes.py`, `file_routes.py`, `auth_routes.py`, `project.py`.
- **Логи**:
  - `/app/logs/colloquium_core.log`: Логи запросов, ошибок, репликации.
  - `/app/logs/context-<username>.log`: Контекст для LLM.
- **Развертывание**:
  - Фронтенд: `npm run build && npm run serve`.
  - Бэкенд: `docker compose up -d --build`.

## 7. Тестирование
- **API**:
  - `curl -H "Cookie: session_id=<your_session_id>" "http://vps.vpn:8008/api/chat/get_stats?chat_id=1"`
  - `curl -H "Cookie: session_id=<your_session_id>" "http://vps.vpn:8008/api/chat/list_files"`
  - `curl -X POST "http://vps.vpn:8008/api/chat/post" -H "Cookie: session_id=<your_session_id>" -H "Content-Type: application/json" -d '{"chat_id": 1, "message": "@grok Please analyze @attach_dir#trade_report/src"}'`
- **БД**:
  - `sqlite3 /app/data/multichat.db "SELECT * FROM projects; SELECT id, file_name, project_id FROM attached_files; SELECT id, user_id, message FROM posts WHERE user_id=2;"`.
- **Тесты**:
  - `docker exec colloquium-core python -m unittest tests.test_replication -v`.

## 8. Заключение
MVP платформы предоставляет базовую функциональность для многопользовательского чата с поддержкой файлов, проектов и LLM-интеграции. Пользователи могут создавать чаты, отправлять сообщения, прикреплять файлы, управлять проектами и видеть статистику токенов. Система готова к дальнейшему расширению, включая фильтрацию файлов, загрузку ZIP и интеграцию с `llm_hands`.