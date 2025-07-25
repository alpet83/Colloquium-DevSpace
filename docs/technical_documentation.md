/docs/technical_documentation.md, updated 2025-07-25 21:30 EEST
# Technical Documentation for Colloquium Chat Server

## Overview
The Colloquium Chat Server is a multi-user chat application with file attachment support, implemented using FastAPI (Python) for the backend and Vue.js with Pinia for the frontend. It supports hierarchical chats, file uploads, project management, and integration with Large Language Model (LLM) services. The backend uses SQLite for data storage, managed via SQLAlchemy's Database class singleton. The system is designed for collaborative code analysis, with features like file indexing, chat replication to LLMs, and token usage statistics.

## Database Schema

### Table: users
**Purpose**: Stores user information for authentication and LLM integration.
**Schema**:
```sql
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name TEXT,
    llm_class TEXT,
    llm_token TEXT,
    password_hash TEXT,
    salt TEXT
)

Fields:

user_id: Unique user identifier.
user_name: User login name (e.g., admin, mcp, grok3).
llm_class: Optional LLM class for integration (e.g., super_grok, chatgpt).
llm_token: Optional LLM API token.
password_hash: SHA256 hash of password with salt.
salt: Random salt for password hashing.

Table: chats
Purpose: Stores chat metadata, including hierarchy via parent_msg_id.Schema:
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_description TEXT,
    user_list TEXT DEFAULT 'all',
    parent_msg_id INTEGER,
    FOREIGN KEY(parent_msg_id) REFERENCES posts(id)
)

Fields:

chat_id: Unique chat identifier.
chat_description: Chat description (e.g., "New Chat").
user_list: Comma-separated list of user IDs or 'all' for public access.
parent_msg_id: ID of parent message for hierarchical chats.

Table: posts
Purpose: Stores chat messages with support for file attachments.Schema:
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    message TEXT,
    timestamp INTEGER,
    rql INTEGER,
    FOREIGN KEY (chat_id) REFERENCES chats(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
)

Fields:

id: Unique message identifier.
chat_id: ID of the chat.
user_id: ID of the user who posted the message.
message: Message content, may include @attach# for file references or , ,  for code submissions and commands (replaced by @attach# or processed output after processing).
timestamp: Unix timestamp of message creation.
rql: Recursion level for LLM responses (optional).

Table: attached_files
Purpose: Stores uploaded files and links to files on disk.Schema:
CREATE TABLE IF NOT EXISTS attached_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content BLOB,
    ts INTEGER,
    file_name TEXT,
    project_id INTEGER,
    FOREIGN KEY (project_id) REFERENCES projects(id)
)

Fields:

id: Unique file identifier.
content: Binary file content (for stored files) or NULL/empty for links. Links are indicated by file_name starting with @ (e.g., @trade_report/example.rs) and represent files stored on disk at /app/projects//.
ts: Unix timestamp of file upload or update.
file_name: File path (e.g., trade_report/example.rs for stored files, @trade_report/example.rs for links).
project_id: ID of the associated project.Notes:
Stored Files: Files uploaded via /chat/upload_file store their content in the content column as a BLOB. These are typically used for direct uploads and are not necessarily tied to the filesystem.
Links: Files created via  tags in messages (processed by llm_hands.py) have empty content and a file_name prefixed with @. The actual file content is stored on disk at /app/projects//, and the FileManager.get_file method retrieves it from disk if content is empty.
Links are validated by FileManager.check at initialization, which converts stale links (files missing on disk) to stored files with content="file was removed?" and removes the @ prefix.

Table: sessions
Purpose: Stores user session data.Schema:
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER,
    active_chat INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
)

Fields:

session_id: Unique session identifier (UUID).
user_id: ID of the authenticated user.
active_chat: ID of the currently active chat for the session.

Table: projects
Purpose: Stores project metadata.Schema:
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL UNIQUE,
    description TEXT,
    local_git TEXT,
    public_git TEXT,
    dependencies TEXT
)

Fields:

id: Unique project identifier.
project_name: Project name.
description: Project description.
local_git: Optional local Git repository path.
public_git: Optional public Git repository URL.
dependencies: Optional dependencies (e.g., for Cargo.toml).

Table: llm_context
Purpose: Tracks the last processed post for LLM replication.Schema:
CREATE TABLE IF NOT EXISTS llm_context (
    actor_id INTEGER,
    chat_id INTEGER,
    last_post_id INTEGER,
    last_timestamp INTEGER,
    PRIMARY KEY (actor_id, chat_id),
    FOREIGN KEY (actor_id) REFERENCES users(user_id),
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
)

Fields:

actor_id: ID of the LLM user.
chat_id: ID of the chat.
last_post_id: ID of the last processed post.
last_timestamp: Timestamp of the last replication.

Table: llm_responses
Purpose: Stores LLM responses for debugging and tracking.Schema:
CREATE TABLE IF NOT EXISTS llm_responses (
    response_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    chat_id INTEGER,
    response_text TEXT,
    timestamp INTEGER,
    triggered_by INTEGER,
    rql INTEGER,
    FOREIGN KEY (actor_id) REFERENCES users(user_id),
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id),
    FOREIGN KEY (triggered_by) REFERENCES posts(id)
)

Fields:

response_id: Unique response identifier.
actor_id: ID of the LLM user.
chat_id: ID of the chat.
response_text: LLM response text.
timestamp: Unix timestamp of response creation.
triggered_by: ID of the post that triggered the response.
rql: Recursion level for the response (optional).

Table: llm_usage
Purpose: Stores statistics of LLM interactions.Schema:
CREATE TABLE IF NOT EXISTS llm_usage (
    ts INTEGER,
    model TEXT,
    sent_tokens INTEGER,
    used_tokens INTEGER,
    chat_id INTEGER
)

Fields:

ts: Unix timestamp of the LLM interaction.
model: Name of the LLM model (e.g., grok3, chatgpt).
sent_tokens: Number of tokens sent to the LLM.
used_tokens: Number of tokens used in the response (from usage.prompt_tokens).
chat_id: ID of the associated chat.

Logging
Initialization Logs

Written to: stderr during server startup, visible via docker logs colloquium-core.
Format: [%Y-%m-%d %H:%M:%S]. #<tag>: <message> (e.g., [2025-07-17 19:00:00]. #INFO: Сервер Colloquium запускается...).

Application Logs

Written to: /app/logs/colloquium_core.log.
Format: [%(asctime)s] #%(levelname)s: %(message)s (e.g., [2025-07-17 19:00:00] #INFO: Saved context to /app/logs/context-grok.log).
Includes: Request logging, replication details, file operations, errors.

LLM Context Logs

Written to: /app/logs/context-<username>.log for each LLM actor.
Content: JSON index and sandwich content for debugging.

Access

Initialization: docker logs colloquium-core.
Application: cat /app/logs/colloquium_core.log.
LLM context: cat /app/logs/context-<username>.log.

Constants
Defined in /agent/globals.py:

LOG_DIR = "/app/logs": Directory for log files.
LOG_FILE = "/app/logs/colloquium_core.log": Path to application log file.
LOG_SERV = "/app/logs/colloquium_serv.log": Path to server log file.
CONFIG_FILE = "/app/data/colloquium_config.toml": Path to configuration file.
PRE_PROMPT_PATH = "/app/prompt.txt": Path to LLM pre-prompt.
Manager placeholders: user_manager, chat_manager, post_manager, file_manager, replication_manager, project_manager, post_processor, sessions_table (initialized as None).

Backend Modules and Functions
/agent/server.py
Purpose: Main FastAPI application entry point, initializes managers and routes, handles server lifecycle.Functions:

log_msg(message, tag): Logs messages to stderr during initialization.
server_init(): Initializes routers and managers (UserManager, ChatManager, PostManager, FileManager, ReplicationManager, ProjectManager).
log_requests_and_exceptions(request, call_next): Middleware to log HTTP requests and exceptions.
lifespan(app): Manages server startup (loads config, initializes managers) and shutdown.
chat_loop(): Periodically checks chat history for updates to trigger replication.
shutdown(): Handles graceful shutdown on signals (SIGTERM, SIGINT, SIGQUIT).
handle_shutdown(signum, frame): Signal handler for shutdown.

/agent/db.py
Purpose: Provides database access via SQLAlchemy singleton.Class: Database

__init__(): Initializes SQLite connection (sqlite:////app/data/multichat.db).
get_database(): Returns singleton instance of Database.
execute(query, params): Executes a SQL query with optional parameters.
fetch_all(query, params): Fetches all rows for a query.
fetch_one(query, params): Fetches one row for a query.Class: DataTable
__init__(table_name, template): Initializes table with schema, creates or updates it.
create(): Creates table based on template.
upgrade(): Adds missing columns to table.
insert_into(values, ignore): Inserts a record, optionally ignoring duplicates.
insert_or_replace(values): Inserts or replaces a record.
update(values, conditions): Updates records based on conditions.
delete_from(conditions): Deletes records based on conditions.
select_from(columns, conditions, order_by, limit, joins, fetch_all): Selects records with optional JOINs.
select_row(columns, conditions, order_by, joins): Selects a single row.

/agent/managers/users.py
Purpose: Manages user authentication and data.Class: UserManager

__init__(): Initializes database, creates admin (password: colloquium) and mcp (user_id=2) users.
_create_tables(): Creates users table.
_init_admin_user(): Initializes default users.
check_auth(username, password): Verifies credentials, returns user_id or None.
get_user_name(user_id): Returns user_name for a given user_id.
get_user_id_by_name(user_name): Returns user_id for a given user_name.
is_llm_user(user_id): Checks if user is associated with an LLM.
get_user_role(user_id): Returns user role (admin, mcp, developer, or LLM).

/agent/managers/chats.py
Purpose: Manages chat creation, deletion, and hierarchy.Class: ChatManager

__init__(): Initializes database.
_create_tables(): Creates chats table.
list_chats(user_id): Returns list of chats accessible to user_id (based on user_list).
create_chat(description, user_id, parent_msg_id): Creates a new chat with optional parent message.
delete_chat(chat_id, user_id): Deletes a chat if no sub-chats exist and user has permission.
active_chat(user_id): Returns the active chat ID for the user from sessions table.
get_chat_hierarchy(chat_id): Returns list of chat IDs in the hierarchy (parent and child chats).

/agent/managers/posts.py
Purpose: Manages chat messages and triggers replication.Class: PostManager

__init__(user_manager): Initializes with database and UserManager.
_create_tables(): Creates posts table.
add_message(chat_id, user_id, message): Adds a message, processes @agent commands via post_processor, saves processed message (e.g., @ Created file: ...), and posts agent response with @ if applicable. Triggers asynchronous replication for non-@agent messages.
get_history(chat_id, only_changes): Retrieves message history, parsing @attach# for file names. Returns {"chat_history": "no changes"} if only_changes=True and no changes are found.
delete_post(post_id, user_id): Deletes a message if user is the author or admin.
edit_post(post_id, user_id, message): Edits a message if user has permission.
add_change(chat_id, post_id, action): Tracks post changes (add, edit, delete) for replication.
get_changes(chat_id): Returns changes for a chat without clearing them.
clear_changes(chat_id): Clears changes for a chat.
trigger_replication(chat_id, post_id): Asynchronously triggers LLM replication for non-@agent messages.

/agent/managers/files.py
Purpose: Manages file uploads, updates, links, and retrieval.Class: FileManager

__init__(): Initializes database, runs check to validate links.
_create_tables(): Creates attached_files table.
check(): Scans attached_files, adds @ prefix to links (empty content), converts stale links (missing files on disk) to stored files with content="file was removed?" and removes the @ prefix.
exists(file_name, project_id): Checks if a file exists as a link (@file_name) or stored file (file_name).
add_file(content, file_name, timestamp, project_id): Adds a file or link to attached_files, uses exists to prevent duplicates.
update_file(file_id, content, file_name, timestamp, project_id): Updates a file or link in attached_files.
unlink(file_id): Removes a file or link record from attached_files without affecting the disk.
backup_file(file_id): Creates a backup of a link file in /agent/projects/backups/..
remove_file(file_id): Removes a link from attached_files and moves the file to backup.
list_files(user_id, project_id): Lists files, optionally filtered by project_id, synchronizes with project files on disk.
get_file(file_id): Retrieves file metadata and content, loading from disk for links (content empty).

/agent/managers/replication.py
Purpose: Handles replication of chat context to LLM services.Class: ReplicationManager

__init__(debug_mode): Initializes with database and debug mode.
_load_actors(): Loads users with LLM configurations as ChatActor instances.
_write_context_stats(content_blocks, llm_name, chat_id, index_json): Writes context statistics to /app/logs/_context.stats.
_recursive_replicate(content_blocks, users, chat_id, actor, exclude_source_id, rql, max_rql): Recursively processes LLM responses, posts them, and triggers further replication for @all. For responses containing @agent, strips content before @agent, processes the command via post_processor, and prepends the stripped prefix to processed_msg.
replicate_to_llm(chat_id, exclude_source_id, debug_mode): Orchestrates replication process with token limit (131072).
_pack_and_send(content_blocks, users, chat_id, exclude_source_id, debug_mode): Packs context using SandwichPack, sends to LLMs, and stores responses.
_store_response(actor_id, chat_id, original_response, processed_response, triggered_by, rql): Stores LLM responses and posts them if triggered by @, @all, or #critics_allowed.
get_processing_status(): Returns replication status (free or busy) with actor and elapsed time.

/agent/chat_actor.py
Purpose: Represents a user with an LLM connection.Class: ChatActor

__init__(user_id, user_name, llm_class, llm_token, post_manager): Initializes actor with LLM connection (XAIConnection or OpenAIConnection).
Supports grok and chatgpt models.

/agent/lib/sandwich_pack.py
Purpose: Packs chat posts and files into a context for LLM processing.Class: SandwichPack

__init__(max_size, system_prompt): Initializes with maximum context size (1,000,000 bytes) and optional system prompt.
register_block_class(block_class): Registers custom block classes for parsing.
load_block_classes(): Loads block classes from *_block.py files.
supported_type(content_type): Checks if content type (e.g., .rs, .vue) is supported.
create_block(content_text, content_type, file_name, timestamp, **kwargs): Creates a ContentBlock or custom block.
generate_unique_file_id(): Generates unique file_id using busy_ids to avoid conflicts.
pack(blocks, users): Packs blocks into sandwiches, generates JSON index with files, entities, users, and sandwiches.
busy_ids: Set of used file_id to prevent conflicts.

/agent/lib/content_block.py
Purpose: Base class for content blocks (posts and files).Class: ContentBlock

__init__(content_text, content_type, file_name, timestamp, **kwargs): Initializes block with content and metadata.
estimate_tokens(content): Estimates token count (length ÷ 4).
parse_content(): Returns parsed entities and dependencies (default: empty).
to_sandwich_block(): Formats block as  (e.g.,  or ) with attributes.
supported_types: Supports :post and :document.

/agent/llm_hands.py
Purpose: Processes @agent commands and tags (, , , , ) for file operations and command execution.Classes:

BlockProcessor: Base class for processing tags (command, code_file, code_patch, shell_code, move_file).
CommandProcessor: Handles <command> tags (e.g., ping, run_test, commit).
ping: Returns @ pong.
run_test: Sends request to MCP_URL/run_test.
commit: Sends request to MCP_URL/commit.


FileEditProcessor: Processes <code_file name="..."> tags, saves files to disk via ProjectManager.edit_file, adds/updates links in attached_files with empty content and @file_name.
FilePatchProcessor: Processes <code_patch file_id="..."> tags, applies patches to files using FileManager.update_file.
FileMoveProcessor: Processes <move_file file_id="..." new_name="..." overwrite="true|false"/> tags.
Purpose: Renames or moves a file (link or stored) in the attached_files table and, for links (file_name starting with @), on the disk at /app/projects/<project_name>/<relative_path>. Validates the new path to ensure it remains within /app/projects. Creates a backup of the original file if it is a link.
Tag Parameters:
file_id: Required, integer, ID of the file in attached_files.
new_name: Required, string, new file path or name (e.g., trade_report/new_example.rs). If no path is provided, prefixed with the project name (e.g., project_name/new_example.rs).
overwrite: Optional, boolean (true or false), allows overwriting an existing file at the new path (default: false).


Behavior: Updates file_name in attached_files. For links, moves the file on disk and creates a backup in /agent/projects/backups/<relative_path>.<timestamp>. Returns success message @<user_name> Файл @attach#<file_id> успешно перемещён в <new_name> or an error if the file is not found, the path is invalid, or the target exists and overwrite=false.


ShellCodeProcessor: Processes <shell_code> tags.
If mcp=true (default), sends command to http://mcp-sandbox:8084/exec_commands (MCP container).
If mcp=false, executes command locally via execute (local container).
Supports <user_input rqs="..." ack="..."/> for interactive commands.Functions:


process_message(text, timestamp, user_name, rql): Processes messages starting with @agent or containing tags (, , , , ). Dynamically generates tag pattern from processor tags, allowing easy extension with new processors.

/agent/routes/auth_routes.py
Purpose: Handles user authentication.Routes:

POST /login: Authenticates user, creates session, sets session_id cookie.
POST /logout: Deletes session, clears session_id cookie.
GET /user/info: Returns user role and ID.

/agent/routes/chat_routes.py
Purpose: Manages chat operations and statistics.Routes:

GET /chat/list: Lists chats accessible to the user.
POST /chat/create: Creates a new chat with optional parent_msg_id.
POST /chat/delete: Deletes a chat if user has permission.
GET /chat/get: Retrieves chat history in format {"posts": [...], "status": {...}}, where status indicates replication state (free or busy with actor and elapsed time). Supports wait_changes with dynamic timeout (20s for busy, 150s for free).
POST /chat/notify_switch: Notifies about chat switching, updates active_chat in sessions table.
POST /chat/post: Adds a new message, triggers replication.
POST /chat/edit_post: Edits a message if user has permission.
POST /chat/delete_post: Deletes a message if user has permission.
GET /chat/get_stats: Returns chat statistics (chat_id, tokens, num_sources_used) from llm_usage table.
GET /chat/get_parent_msg: Returns parent message details for a given post_id.
GET /chat/logs: Returns last 100 log entries (ERROR or WARNING) from /app/logs/colloquium_core.log.

/agent/routes/file_routes.py
Purpose: Manages file operations.Routes:

POST /chat/upload_file: Uploads a file, stores in attached_files as a stored file.
POST /chat/update_file: Updates a file’s content and name.
POST /chat/delete_file: Deletes a file (via unlink) if not referenced and user is admin.
GET /chat/list_files: Lists files, optionally filtered by project_id.
GET /chat/file_contents: Retrieves file content by file_id using FileManager.get_file.

/agent/routes/project_routes.py
Purpose: Manages project operations.Routes:

GET /project/list: Lists all projects.
POST /project/create: Creates a new project.
POST /project/update: Updates a project.
POST /project/select: Sets the active project in globals.project_manager.

Frontend Modules
/frontend/rtm/src/stores/auth.js
Purpose: Manages authentication state using Pinia.Store: useAuthStore

State: isLoggedIn, username, password, loginError, backendError, isCheckingSession, userRole, userId, apiUrl.
Actions:
checkSession(): Checks session validity via /api/chat/list and fetches user info.
login(username, password): Authenticates user via /api/login, sets userRole and userId.
logout(): Clears session via /api/logout.



/frontend/rtm/src/stores/chat.js
Purpose: Manages chat-related state and operations.Store: useChatStore

State: chats, selectedChatId, history, newChatDescription, newChatParentMessageId, chatError, backendError, apiUrl, waitChanges, stats, status, pollingInterval, isPolling.
Actions:
fetchHistory(): Fetches chat history via /api/chat/get, processes posts and status from response.
sendMessage(message): Sends a message via /api/chat/post.
editPost(postId, message): Edits a message via /api/chat/edit_post.
deletePost(postId, postUserId, userId, userRole): Deletes a message if authorized.
createChat(description): Creates a chat via /api/chat/create.
deleteChat(): Deletes the selected chat via /api/chat/delete.
setChatId(chatId): Sets the selected chat ID, notifies /api/chat/notify_switch.
openCreateChatModal(parentMessageId): Opens the create chat modal.
closeCreateChatModal(): Closes the create chat modal.
buildChatTree(chats): Builds a hierarchical chat tree based on parent_msg_id.
fetchChatStats(): Fetches chat statistics via /api/chat/get_stats.



/frontend/rtm/src/stores/files.js
Purpose: Manages file-related state and operations.Store: useFileStore

State: files, pendingAttachment, chatError, backendError, apiUrl.
Actions:
fetchFiles(): Fetches files via /api/chat/list_files.
deleteFile(fileId): Deletes a file via /api/chat/delete_file.
updateFile(fileId, event): Updates a file via /api/chat/update_file.
uploadFile(file, fileName, chatId): Uploads a file via /api/chat/upload_file.
clearAttachment(): Clears the pending attachment.



/frontend/rtm/src/components/App.vue
Purpose: Root component, orchestrates the three-panel layout.Components: Login, SideBar, ChatContainer, RightPanel.Behavior:

Displays Login.vue if authStore.isLoggedIn is false.
Displays three-panel layout (main-content) if authStore.isLoggedIn is true.
Calls authStore.checkSession() on mount to initialize state.

/frontend/rtm/src/components/SideBar.vue
Purpose: Displays chat list and token statistics.Features:

Lists chats from chatStore.chats with hierarchical display via ChatTree.vue.
Allows selecting a chat (chatStore.selectChat).
Displays token count (stats.tokens) from /api/chat/get_stats at the bottom.
Collapsible to 30px using a toggle button (▶/◄).

/frontend/rtm/src/components/ChatContainer.vue
Purpose: Displays chat messages, input field, and modals for chat creation, file uploads, and post editing.Features:

Shows message history (chatStore.history) with formatted timestamps and file references.
Supports sending messages with @attach# and @attach_dir#.
Blocks message sending when chatStore.status.status is busy, displaying error "Отправка заблокирована: идёт обработка запроса", while allowing text editing in the input field.
Messages with newlines (\n) and no special tags (, , , ) are wrapped in  with minimal styling (font-family: monospace, white-space: pre-wrap).
Displays  for file preview on clicking @attach# or @attached_file#, fetching content via /chat/file_contents.
Modals for creating chats, uploading files, and editing posts.
Polls /api/chat/get every 15 seconds for updates when a chat is selected.
Formats messages with:
@attach# and @attached_file# as clickable links () to open file preview.
Unresolved @attach# as Файл  удалён или недоступен.
, , , ,  in styled  blocks.



/frontend/rtm/src/components/RightPanel.vue
Purpose: Manages project selection and file tree display.Features:

Lists projects via /api/project/list.
Allows creating/editing projects via /api/project/create and /api/project/update.
Displays file tree (FileTree.vue, FileManager.vue) with files from fileStore.files.
Supports file filtering by project_id.
Collapsible to 30px using a toggle button (▶/◄).

/frontend/rtm/src/components/ChatTree.vue
Purpose: Renders hierarchical chat tree.Features:

Displays chats with expand/collapse functionality (▼/▶).
Emits select-chat event when a chat is clicked.

/frontend/rtm/src/components/FileTree.vue
Purpose: Renders hierarchical file tree.Features:

Displays directories and files with expand/collapse (▼/▶).
Emits select-file event to add @attach# to messages.

/frontend/rtm/src/components/FileManager.vue
Purpose: Manages file tree rendering and file operations.Features:

Builds file tree from fileStore.files.
Handles file selection, deletion, and updates via fileStore actions.

/frontend/rtm/src/components/Login.vue
Purpose: Handles user login interface.Features:

Input fields for username and password.
Calls authStore.login on submit.
Displays authStore.loginError if authentication fails.

/frontend/rtm/src/main.js
Purpose: Initializes Vue app and Pinia.Features:

Creates Vue app, mounts to #app.
Initializes Pinia for state management.
Provides mitt event bus for inter-component communication.

Deployment
Docker

Backend: Runs in colloquium-core container, exposed on http://localhost:8080.
Frontend: Runs via npm run serve on http://vps.vpn:8008.
Command: docker compose up -d --build.

Nginx

Proxies /api/* requests to /chat/*, /login, /logout, etc.

Logs

Frontend: /opt/docker/mcp-server/frontend/rtm/npm-debug.log.
Backend: /app/logs/colloquium_core.log, /app/logs/context-.log.

Configuration

Loaded from /app/data/colloquium_config.toml.

File Indexing

Uses sandwiches_index.json for file metadata, generated by SandwichPack.

Notes

Supported File Types: .rs (Rust), .vue (Vue.js), .js (JavaScript), .py (Python), .rulz (documentation).
LLM Integration:
Supports grok (via XAIConnection) and chatgpt (via OpenAIConnection).
Triggered by @, @all, or #critics_allowed in messages.
Context limited to 131072 tokens, packed into sandwiches with JSON index.
LLM responses containing @agent have content before @agent stripped, processed as a command, and the prefix prepended to the processed_msg for user visibility.


Shell Code Execution:
 tags are processed by ShellCodeProcessor in llm_hands.py.
If mcp=true (default), commands are sent to http://mcp-sandbox:8084/exec_commands (MCP container).
If mcp=false, commands are executed locally via execute (local container).
Supports  for interactive commands, enabling complex workflows.


Chat Enhancements:
GET /chat/get returns {"posts": [...], "status": {...}}, where status indicates replication state (free or busy with actor and elapsed time). Dynamic timeout for wait_changes (20s for busy, 150s for free) improves responsiveness.
Frontend blocks message sending during busy state, enhancing user experience by preventing queue overload.
Messages with newlines (\n) are displayed in a monospaced font with preserved formatting, improving readability.
File preview available via clicking @attach# or @attached_file#, fetching content from /chat/file_contents.



Change Log
See /docs/change_log.md for recent updates and changes.