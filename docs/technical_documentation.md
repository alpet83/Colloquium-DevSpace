# Technical Documentation for Colloquium Chat Server

## Overview
The Colloquium Chat Server is a multi-user chat application with file attachment support, implemented using FastAPI (Python) for the backend and Vue.js with Pinia for the frontend. It supports hierarchical chats, file uploads, and integration with LLM services. The backend uses SQLite for data storage, managed via SQLAlchemy's `Database` class.

## Database Schema

### Table: `users`
- **Purpose**: Stores user information for authentication.
- **Schema**:
  ```sql
  CREATE TABLE IF NOT EXISTS users (
      user_id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_name TEXT,
      llm_class TEXT,
      llm_token TEXT,
      password_hash TEXT,
      salt TEXT
  )
  ```
- **Fields**:
  - `user_id`: Unique user identifier.
  - `user_name`: User login name (e.g., `admin`, `mcp`).
  - `llm_class`: Optional LLM class for integration (e.g., `super_grok`, `chatgpt`).
  - `llm_token`: Optional LLM API token.
  - `password_hash`: SHA256 hash of password with salt.
  - `salt`: Random salt for password hashing.

### Table: `chats`
- **Purpose**: Stores chat metadata, including hierarchy via `parent_msg_id`.
- **Schema**:
  ```sql
  CREATE TABLE IF NOT EXISTS chats (
      chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_description TEXT,
      user_list TEXT DEFAULT 'all',
      parent_msg_id INTEGER,
      FOREIGN KEY(parent_msg_id) REFERENCES posts(id)
  )
  ```
- **Fields**:
  - `chat_id`: Unique chat identifier.
  - `chat_description`: Chat description.
  - `user_list`: Comma-separated list of user IDs or `all` for public access.
  - `parent_msg_id`: ID of parent message for hierarchical chats.

### Table: `posts`
- **Purpose**: Stores chat messages.
- **Schema**:
  ```sql
  CREATE TABLE IF NOT EXISTS posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER,
      user_id INTEGER,
      message TEXT,
      timestamp INTEGER,
      FOREIGN KEY (chat_id) REFERENCES chats(id),
      FOREIGN KEY (user_id) REFERENCES users(id)
  )
  ```
- **Fields**:
  - `id`: Unique message identifier.
  - `chat_id`: ID of the chat.
  - `user_id`: ID of the user who posted the message.
  - `message`: Message content, may include `@attach#<file_id>` for file references.
  - `timestamp`: Unix timestamp of message creation.

### Table: `attached_files`
- **Purpose**: Stores uploaded files.
- **Schema**:
  ```sql
  CREATE TABLE IF NOT EXISTS attached_files (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      content BLOB,
      file_id INTEGER,
      ts INTEGER,
      file_name TEXT
  )
  ```
- **Fields**:
  - `id`: Unique file identifier.
  - `content`: Binary file content.
  - `file_id`: Redundant field (not used consistently).
  - `ts`: Unix timestamp of file upload.
  - `file_name`: File path or name (e.g., `/src/test.rs`).

### Table: `sessions`
- **Purpose**: Stores user session data.
- **Schema**:
  ```sql
  CREATE TABLE IF NOT EXISTS sessions (
      session_id TEXT PRIMARY KEY,
      user_id INTEGER,
      FOREIGN KEY (user_id) REFERENCES users(user_id)
  )
  ```
- **Fields**:
  - `session_id`: Unique session identifier (UUID).
  - `user_id`: ID of the authenticated user.

## Logging
- **Initialization Logs**:
  - Logs during server initialization are written to `stderr` using the `log_msg` function and are visible via `docker logs colloquium-core`.
  - Format: `[%Y-%m-%d %H:%M:%S]. #<tag>: <message>` (e.g., `[2025-07-14 19:39:00]. #INFO: Сервер Colloquium запускается...`).
- **Application Logs**:
  - Logs are written to `/app/logs/colloquium_core.log` with format `[%(asctime)s] #%(levelname)s: %(message)s` (e.g., `[2025-07-14 19:39:00] #INFO: Ядро запущено на IP 172.18.0.2:8080`).
  - Uvicorn logs (e.g., `Started server process`) use the same format, ensuring timestamps for all entries.
- **Access**:
  - Initialization logs: `docker logs colloquium-core`.
  - Application logs: `cat /app/logs/colloquium_core.log`.

## Constants
- Defined in `/agent/globals.py`:
  - `LOG_DIR = "/app/logs"`: Directory for log files.
  - `LOG_FILE = "/app/logs/colloquium_core.log"`: Path to application log file.
  - `CONFIG_FILE = "/app/data/colloquium_config.toml"`: Path to configuration file.
  - Manager placeholders: `user_manager`, `chat_manager`, `post_manager`, `file_manager`, `replication_manager` (initialized as `None`).

## Modules and Functions

### `/agent/server.py`
- **Purpose**: Main FastAPI application entry point, initializes managers and routes, handles server lifecycle.
- **Functions**:
  - `log_msg(message, tag)`: Logs messages to `stderr` during initialization.
  - `server_init()`: Initializes routers and managers, called within `main`.
  - `log_requests_and_exceptions(request, call_next)`: Middleware to log requests and exceptions.
  - `lifespan(app)`: Manages server startup and shutdown, loads configuration.
  - `chat_loop()`: Periodically checks chat history for updates.
  - `shutdown()`: Handles graceful shutdown on signals (SIGTERM, SIGINT, SIGQUIT).
  - `handle_shutdown(signum, frame)`: Signal handler for shutdown.

### `/agent/db.py`
- **Purpose**: Provides database access via SQLAlchemy.
- **Class: `Database`**
  - `__init__()`: Initializes SQLite or MariaDB connection based on `colloquium_config.toml`.
  - `get_connection()`: Returns a database connection.
  - `execute(query, params)`: Executes a SQL query with optional parameters.
  - `fetch_all(query, params)`: Fetches all rows for a query.
  - `fetch_one(query, params)`: Fetches one row for a query.

### `/agent/managers/users.py`
- **Purpose**: Manages user authentication and data.
- **Class: `UserManager`**
  - `__init__()`: Initializes database and creates admin/mcp users.
  - `_create_tables()`: Creates `users` table.
  - `_init_admin_user()`: Initializes `admin` (password: `colloquium`) and `mcp` users.
  - `check_auth(username, password)`: Verifies user credentials, returns `user_id` or `None`.
  - `get_user_name(user_id)`: Returns `user_name` for a given `user_id`.
  - `get_user_id_by_name(user_name)`: Returns `user_id` for a given `user_name`.
  - `is_llm_user(user_id)`: Checks if user is associated with an LLM.

### `/agent/managers/chats.py`
- **Purpose**: Manages chat creation, deletion, and hierarchy.
- **Class: `ChatManager`**
  - `__init__()`: Initializes database.
  - `_create_tables()`: Creates `chats` table.
  - `list_chats(user_id)`: Returns list of chats accessible to `user_id`.
  - `create_chat(description, user_id, parent_msg_id)`: Creates a new chat.
  - `delete_chat(chat_id, user_id)`: Deletes a chat if no sub-chats exist.
  - `get_chat_hierarchy(chat_id)`: Returns list of chat IDs in hierarchy.

### `/agent/managers/posts.py`
- **Purpose**: Manages chat messages.
- **Class: `PostManager`**
  - `__init__(user_manager)`: Initializes database.
  - `init_db()`: Creates `posts` table.
  - `add_message(chat_id, user_id, message)`: Adds a message to a chat.
  - `get_history(chat_id)`: Retrieves message history for a chat, parsing `@attach#<file_id>` for file names.
  - `delete_post(post_id, user_id)`: Deletes a message if user has permission.

### `/agent/managers/files.py`
- **Purpose**: Manages file uploads and retrieval.
- **Class: `FileManager`**
  - `__init__()`: Initializes database.
  - `_create_tables()`: Creates `attached_files` table.
  - `upload_file(chat_id, user_id, content, file_name)`: Uploads a file.
  - `update_file(file_id, user_id, content, file_name)`: Updates a file.
  - `delete_file(file_id, user_id)`: Deletes a file if not used in posts.
  - `list_files(user_id)`: Lists all files.
  - `get_file(file_id)`: Retrieves file metadata.
  - `get_sandwiches_index()`: Loads file index from `sandwiches_index.json`.

### `/agent/managers/replication.py`
- **Purpose**: Handles message replication to LLM services.
- **Class: `ReplicationManager`**
  - `__init__(user_manager, chat_manager, post_manager)`: Initializes with managers.
  - `_load_actors()`: Loads users with LLM configurations as `ChatActor` instances.
  - `replicate_to_llm(chat_id, exclude_source_id)`: Sends chat context to LLMs.
- **Class: `ChatActor`**
  - `__init__(user_id, user_name, llm_class, llm_token)`: Initializes actor with LLM connection.

### `/agent/routes/auth_routes.py`
- **Purpose**: Handles user authentication.
- **Routes**:
  - `POST /login`: Authenticates user, creates session, sets `session_id` cookie.
  - `POST /logout`: Deletes session, removes `session_id` cookie.

### `/agent/routes/chat_routes.py`
- **Purpose**: Manages chat operations.
- **Routes**:
  - `GET /chat/list`: Lists chats accessible to the user.
  - `POST /chat/create`: Creates a new chat.
  - `POST /chat/delete`: Deletes a chat.

### `/agent/routes/post_routes.py`
- **Purpose**: Manages message operations.
- **Routes**:
  - `GET /chat/test`: Test route for debugging.
  - `GET /chat/test_get`: Additional test route for debugging.
  - `GET /chat/get`: Retrieves chat history.
  - `POST /chat/post`: Adds a new message.
  - `POST /chat/delete_post`: Deletes a message.

### `/agent/routes/file_routes.py`
- **Purpose**: Manages file operations.
- **Routes**:
  - `POST /chat/upload_file`: Uploads a file.
  - `POST /chat/update_file`: Updates a file.
  - `POST /chat/delete_file`: Deletes a file.
  - `GET /chat/list_files`: Lists all files.
  - `GET /chat/get_sandwiches_index`: Returns file index from `sandwiches_index.json`.

## Frontend Modules

### `/frontend/rtm/src/store.js`
- **Purpose**: Manages frontend state using Pinia.
- **Store: `useChatStore`**
  - State: `isLoggedIn`, `username`, `password`, `loginError`, `chatError`, `backendError`, `chats`, `selectedChatId`, `history`, `files`, `sandwichFiles`, etc.
  - Actions: `checkSession`, `login`, `logout`, `fetchHistory`, `fetchFiles`, `fetchSandwichFiles`, `sendMessage`, `uploadFile`, `createChat`, `deletePost`, `deleteChat`, `deleteFile`, `updateFile`, `selectChat`, `openCreateChatModal`, `closeCreateChatModal`, `clearAttachment`, `buildChatTree`.

### `/frontend/rtm/src/components/ChatContainer.vue`
- **Purpose**: Displays chat messages and handles file uploads.
- **Methods**:
  - `openFileConfirmModal`: Opens modal for file name confirmation.
  - `closeFileConfirmModal`: Closes file modal.
  - `confirmFileUpload`: Uploads file.
  - `sendMessage`: Sends a message.
  - `formatMessage`: Formats message with file names and timestamps.

## Notes
- The project uses Docker for deployment, with logs at `/logs/frontend.log` and `/logs/colloquium_core.log`.
- The frontend runs on `http://vps.vpn:8008`, backend on `http://localhost:8080`.
- Nginx proxies requests from `/api/*` to `/chat/*`, `/login`, `/logout`, etc.
- Configuration is loaded from `/app/data/colloquium_config.toml`.
- The `sandwiches_index.json` file is used for file indexing but may be missing, causing errors.