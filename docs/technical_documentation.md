/docs/technical_documentation.md, updated 2025-07-16 16:12 EEST
Technical Documentation for Colloquium Chat Server
Overview
The Colloquium Chat Server is a multi-user chat application with file attachment support, implemented using FastAPI (Python) for the backend and Vue.js with Pinia for the frontend. It supports hierarchical chats, file uploads, project management, and integration with Large Language Model (LLM) services. The backend uses SQLite for data storage, managed via SQLAlchemy's Database class singleton. The system is designed for collaborative code analysis, with features like file indexing, chat replication to LLMs, and token usage statistics.
Database Schema
Table: users

Purpose: Stores user information for authentication and LLM integration.
Schema:CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name TEXT,
    llm_class TEXT,
    llm_token TEXT,
    password_hash TEXT,
    salt TEXT
)


Fields:
user_id: Unique user identifier.
user_name: User login name (e.g., admin, mcp).
llm_class: Optional LLM class for integration (e.g., super_grok, chatgpt).
llm_token: Optional LLM API token.
password_hash: SHA256 hash of password with salt.
salt: Random salt for password hashing.



Table: chats

Purpose: Stores chat metadata, including hierarchy via parent_msg_id.
Schema:CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_description TEXT,
    user_list TEXT DEFAULT 'all',
    parent_msg_id INTEGER,
    FOREIGN KEY(parent_msg_id) REFERENCES posts(id)
)


Fields:
chat_id: Unique chat identifier.
chat_description: Chat description (e.g., "New Chat").
user_list: Comma-separated list of user IDs or all for public access.
parent_msg_id: ID of parent message for hierarchical chats.



Table: posts

Purpose: Stores chat messages with support for file attachments.
Schema:CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    message TEXT,
    timestamp INTEGER,
    FOREIGN KEY (chat_id) REFERENCES chats(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
)


Fields:
id: Unique message identifier.
chat_id: ID of the chat.
user_id: ID of the user who posted the message.
message: Message content, may include @attach#<file_id> or @attach_dir#<dir_name> for file/directory references.
timestamp: Unix timestamp of message creation.



Table: attached_files

Purpose: Stores uploaded files with metadata.
Schema:CREATE TABLE IF NOT EXISTS attached_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content BLOB,
    file_id INTEGER,
    ts INTEGER,
    file_name TEXT,
    project_id INTEGER,
    FOREIGN KEY (project_id) REFERENCES projects(id)
)


Fields:
id: Unique file identifier.
content: Binary file content.
file_id: Redundant field (not used consistently, replaced by id).
ts: Unix timestamp of file upload or update.
file_name: File path (e.g., trade_report/src/main.rs).
project_id: ID of the associated project.



Table: sessions

Purpose: Stores user session data.
Schema:CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
)


Fields:
session_id: Unique session identifier (UUID).
user_id: ID of the authenticated user.



Table: projects

Purpose: Stores project metadata.
Schema:CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT,
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

Purpose: Tracks the last processed post for LLM replication.
Schema:CREATE TABLE IF NOT EXISTS llm_context (
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

Purpose: Stores LLM responses for debugging and tracking.
Schema:CREATE TABLE IF NOT EXISTS llm_responses (
    response_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    chat_id INTEGER,
    response_text TEXT,
    timestamp INTEGER,
    triggered_by INTEGER,
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



Logging

Initialization Logs:
Written to stderr during server startup, visible via docker logs colloquium-core.
Format: [%Y-%m-%d %H:%M:%S]. #<tag>: <message> (e.g., [2025-07-16 16:00:00]. #INFO: Сервер Colloquium запускается...).


Application Logs:
Written to /app/logs/colloquium_core.log.
Format: [%(asctime)s] #%(levelname)s: %(message)s (e.g., [2025-07-16 16:00:00] #INFO: Saved context to /app/logs/context-grok.log).
Includes request logging, replication details, and errors.


LLM Context Logs:
Saved to /app/logs/context-<username>.log for each LLM actor.
Contains JSON index and sandwich content for debugging.


Access:
Initialization: docker logs colloquium-core.
Application: cat /app/logs/colloquium_core.log.
LLM context: cat /app/logs/context-<username>.log.



Constants

Defined in /agent/globals.py:
LOG_DIR = "/app/logs": Directory for log files.
LOG_FILE = "/app/logs/colloquium_core.log": Path to application log file.
CONFIG_FILE = "/app/data/colloquium_config.toml": Path to configuration file.
DB_PATH = "/app/data/multichat.db": Path to SQLite database.
Manager placeholders: user_manager, chat_manager, post_manager, file_manager, replication_manager (initialized as None).



Backend Modules and Functions
/agent/server.py

Purpose: Main FastAPI application entry point, initializes managers and routes, handles server lifecycle.
Functions:
log_msg(message, tag): Logs messages to stderr during initialization.
server_init(): Initializes routers and managers (UserManager, ChatManager, PostManager, FileManager, ReplicationManager).
log_requests_and_exceptions(request, call_next): Middleware to log HTTP requests and exceptions.
lifespan(app): Manages server startup (loads config, initializes managers) and shutdown.
chat_loop(): Periodically checks chat history for updates to trigger replication.
shutdown(): Handles graceful shutdown on signals (SIGTERM, SIGINT, SIGQUIT).
handle_shutdown(signum, frame): Signal handler for shutdown.



/agent/db.py

Purpose: Provides database access via SQLAlchemy singleton.
Class: Database
__init__(): Initializes SQLite connection (sqlite:////app/data/multichat.db).
get_database(): Returns singleton instance of Database.
execute(query, params): Executes a SQL query with optional parameters.
fetch_all(query, params): Fetches all rows for a query.
fetch_one(query, params): Fetches one row for a query.



/agent/managers/users.py

Purpose: Manages user authentication and data.
Class: UserManager
__init__(): Initializes database, creates admin (password: colloquium) and mcp (user_id=2) users.
_create_tables(): Creates users table.
_init_admin_user(): Initializes default users.
check_auth(username, password): Verifies credentials, returns user_id or None.
get_user_name(user_id): Returns user_name for a given user_id.
get_user_id_by_name(user_name): Returns user_id for a given user_name.
is_llm_user(user_id): Checks if user is associated with an LLM.



/agent/managers/chats.py

Purpose: Manages chat creation, deletion, and hierarchy.
Class: ChatManager
__init__(): Initializes database.
_create_tables(): Creates chats table.
list_chats(user_id): Returns list of chats accessible to user_id (based on user_list).
create_chat(description, user_id, parent_msg_id): Creates a new chat with optional parent message.
delete_chat(chat_id, user_id): Deletes a chat if no sub-chats exist and user has permission.
get_chat_hierarchy(chat_id): Returns list of chat IDs in the hierarchy (parent and child chats).



/agent/managers/posts.py

Purpose: Manages chat messages and triggers replication.
Class: PostManager
__init__(user_manager, replication_manager): Initializes with database and managers.
init_db(): Creates posts table.
add_message(chat_id, user_id, message): Adds a message, triggers replicate_to_llm.
get_history(chat_id): Retrieves message history, parsing @attach#<file_id> and @attach_dir#<dir_name> for file names.
delete_post(post_id, user_id): Deletes a message if user is the author or admin.
edit_post(post_id, user_id, message): Edits a message if user has permission.



/agent/managers/files.py

Purpose: Manages file uploads, updates, and retrieval.
Class: FileManager
__init__(): Initializes database.
_create_tables(): Creates attached_files table.
upload_file(chat_id, user_id, content, file_name): Uploads a file, stores content as BLOB.
update_file(file_id, user_id, content, file_name): Updates a file if user has permission.
delete_file(file_id, user_id): Deletes a file if not referenced in posts and user is admin.
list_files(user_id): Lists all files accessible to user_id.
get_file(file_id): Retrieves file metadata and content.
get_sandwiches_index(): Loads file index from sandwiches_index.json (if available).



/agent/managers/replication.py

Purpose: Handles replication of chat context to LLM services.

Class: ReplicationManager

__init__(user_manager, chat_manager, post_manager, file_manager, debug_mode): Initializes with managers and debug mode.
_init_tables(): Creates llm_context and llm_responses tables.
_load_actors(): Loads users with LLM configurations as ChatActor instances.
_resolve_file_id(match, file_ids, file_map): Resolves @attach#<file_id> and @attach_dir#<dir_name> to @attached_file#<file_id> or @attached_files#[<id1>,<id2>,...].
_assemble_posts(chat_id, exclude_source_id, file_ids, file_map): Collects posts and resolves file references.
_assemble_files(file_ids, file_map): Collects files for context.
_pack_and_send(content_blocks, users, chat_id, exclude_source_id, debug_mode): Packs context using SandwichPack, sends to LLMs, and stores responses.
replicate_to_llm(chat_id, exclude_source_id, debug_mode): Orchestrates replication process with token limit (131072).
_store_response(actor_id, chat_id, response_text, triggered_by): Stores LLM responses and posts them if triggered by @<username>, @all, or #critics_allowed.
last_sent_tokens: Stores token count of the last sent context.


Class: ChatActor

__init__(user_id, user_name, llm_class, llm_token, post_manager): Initializes actor with LLM connection (XAIConnection or OpenAIConnection).
Supports grok and chatgpt models.



/agent/lib/sandwich_pack.py

Purpose: Packs chat posts and files into a context for LLM processing.
Class: SandwichPack
__init__(max_size, system_prompt): Initializes with maximum context size (80,000 bytes) and optional system prompt.
register_block_class(block_class): Registers custom block classes for parsing.
load_block_classes(): Loads block classes from *_block.py files.
supported_type(content_type): Checks if content type (e.g., .rs, .vue) is supported.
create_block(content_text, content_type, file_name, timestamp, **kwargs): Creates a ContentBlock or custom block.
generate_unique_file_id(): Generates unique file_id using busy_ids to avoid conflicts.
pack(blocks, users): Packs blocks into sandwiches, generates JSON index with files, entities, users, and sandwiches.
busy_ids: Set of used file_id to prevent conflicts.



/agent/lib/content_block.py

Purpose: Base class for content blocks (posts and files).
Class: ContentBlock
__init__(content_text, content_type, file_name, timestamp, **kwargs): Initializes block with content and metadata.
estimate_tokens(content): Estimates token count (length ÷ 4).
parse_content(): Returns parsed entities and dependencies (default: empty).
to_sandwich_block(): Formats block as <tag> (e.g., <post> or <document>) with attributes.
supported_types: Supports :post and :document.



/agent/routes/auth_routes.py

Purpose: Handles user authentication.
Routes:
POST /login: Authenticates user, creates session, sets session_id cookie.
POST /logout: Deletes session, clears session_id cookie.
GET /user/info: Returns user role and ID.



/agent/routes/chat_routes.py

Purpose: Manages chat operations and statistics.
Routes:
GET /chat/list: Lists chats accessible to the user.
POST /chat/create: Creates a new chat with optional parent_msg_id.
POST /chat/delete: Deletes a chat if user has permission.
GET /chat/get_stats: Returns chat statistics (chat_id, tokens from ReplicationManager.last_sent_tokens).



/agent/routes/post_routes.py

Purpose: Manages message operations.
Routes:
GET /chat/get: Retrieves chat history with parsed file references.
POST /chat/post: Adds a new message, triggers replication.
POST /chat/edit_post: Edits a message if user has permission.
POST /chat/delete_post: Deletes a message if user has permission.



/agent/routes/file_routes.py

Purpose: Manages file operations.
Routes:
POST /chat/upload_file: Uploads a file, stores in attached_files.
POST /chat/update_file: Updates a file’s content and name.
POST /chat/delete_file: Deletes a file if not referenced and user is admin.
GET /chat/list_files: Lists files, optionally filtered by project_id.
GET /chat/get_sandwiches_index: Returns file index from sandwiches_index.json (if available).



/agent/managers/project.py

Purpose: Manages project metadata.
Class: ProjectManager
__init__(): Initializes database.
_create_tables(): Creates projects table.
list_projects(): Lists all projects.
create_project(project_name, description, local_git, public_git, dependencies): Creates a new project.
update_project(project_id, project_name, description, local_git, public_git, dependencies): Updates a project.



Frontend Modules
/frontend/rtm/src/stores/auth.js

Purpose: Manages authentication state using Pinia.
Store: useAuthStore
State: isLoggedIn, username, password, loginError, backendError, isCheckingSession, userRole, userId, apiUrl.
Actions:
checkSession(): Checks session validity via /api/chat/list and fetches user info.
login(username, password): Authenticates user via /api/login, sets userRole and userId.
logout(): Clears session via /api/logout.





/frontend/rtm/src/stores/chat.js

Purpose: Manages chat-related state and operations.
Store: useChatStore
State: chats, selectedChatId, history, newChatDescription, newChatParentMessageId, chatError, backendError, apiUrl.
Actions:
fetchHistory(): Fetches chat history via /api/chat/get.
sendMessage(message): Sends a message via /api/chat/post.
editPost(postId, message): Edits a message via /api/chat/edit_post.
deletePost(postId, postUserId, userId, userRole): Deletes a message if authorized.
createChat(description): Creates a chat via /api/chat/create.
deleteChat(): Deletes the selected chat via /api/chat/delete.
setChatId(chatId): Sets the selected chat ID.
openCreateChatModal(parentMessageId): Opens the create chat modal.
closeCreateChatModal(): Closes the create chat modal.
buildChatTree(chats): Builds a hierarchical chat tree based on parent_msg_id.





/frontend/rtm/src/stores/files.js

Purpose: Manages file-related state and operations.
Store: useFileStore
State: files, pendingAttachment, chatError, backendError, apiUrl.
Actions:
fetchFiles(): Fetches files via /api/chat/list_files.
deleteFile(fileId): Deletes a file via /api/chat/delete_file.
updateFile(fileId, event): Updates a file via /api/chat/update_file.
uploadFile(file, fileName, chatId): Uploads a file via /api/chat/upload_file.
clearAttachment(): Clears the pending attachment.





/frontend/rtm/src/components/App.vue

Purpose: Root component, orchestrates the three-panel layout.
Components: Login, SideBar, ChatContainer, RightPanel.
Behavior:
Displays Login.vue if authStore.isLoggedIn is false.
Displays three-panel layout (main-content) if authStore.isLoggedIn is true.
Calls authStore.checkSession() on mount to initialize state.



/frontend/rtm/src/components/SideBar.vue

Purpose: Displays chat list and token statistics.
Features:
Lists chats from chatStore.chats with hierarchical display via ChatTree.vue.
Allows selecting a chat (chatStore.selectChat).
Displays token count (stats.tokens) from /api/chat/get_stats at the bottom.
Collapsible to 30px using a toggle button (▶/◄).



/frontend/rtm/src/components/ChatContainer.vue

Purpose: Displays chat messages, input field, and modals for chat creation, file uploads, and post editing.
Features:
Shows message history (chatStore.history) with formatted timestamps and file references.
Supports sending messages with @attach#<file_id> and @attach_dir#<dir_name>.
Modals for creating chats, uploading files, and editing posts.
Polls /api/chat/get every 5 seconds for updates when a chat is selected.



/frontend/rtm/src/components/RightPanel.vue

Purpose: Manages project selection and file tree display.
Features:
Lists projects via /api/project/list.
Allows creating/editing projects via /api/project/create and /api/project/update.
Displays file tree (FileTree.vue, FileManager.vue) with files from fileStore.files.
Supports file filtering by project_id.
Collapsible to 30px using a toggle button (▶/◄).



/frontend/rtm/src/components/ChatTree.vue

Purpose: Renders hierarchical chat tree.
Features:
Displays chats with expand/collapse functionality (▼/▶).
Emits select-chat event when a chat is clicked.



/frontend/rtm/src/components/FileTree.vue

Purpose: Renders hierarchical file tree.
Features:
Displays directories and files with expand/collapse (▼/▶).
Emits select-file event to add @attach#<file_id> to messages.



/frontend/rtm/src/components/FileManager.vue

Purpose: Manages file tree rendering and file operations.
Features:
Builds file tree from fileStore.files.
Handles file selection, deletion, and updates via fileStore actions.



/frontend/rtm/src/components/Login.vue

Purpose: Handles user login interface.
Features:
Input fields for username and password.
Calls authStore.login on submit.
Displays authStore.loginError if authentication fails.



/frontend/rtm/src/main.js

Purpose: Initializes Vue app and Pinia.
Features:
Creates Vue app, mounts to #app.
Initializes Pinia for state management.
Provides mitt event bus for inter-component communication.



Deployment

Docker:
Backend: Runs in colloquium-core container, exposed on http://localhost:8080.
Frontend: Runs via npm run serve on http://vps.vpn:8008.
Command: docker compose up -d --build.


Nginx:
Proxies /api/* requests to /chat/*, /login, /logout, etc.


Logs:
Frontend: /opt/docker/mcp-server/frontend/rtm/npm-debug.log.
Backend: /app/logs/colloquium_core.log, /app/logs/context-<username>.log.


Configuration:
Loaded from /app/data/colloquium_config.toml.


File Indexing:
Uses sandwiches_index.json for file metadata, generated by SandwichPack.



Notes

Supported File Types: .rs (Rust), .vue (Vue.js), .js (JavaScript), .py (Python), .rulz (documentation).
LLM Integration:
Supports grok (via XAIConnection) and chatgpt (via OpenAIConnection).
Triggered by @<username>, @all, or #critics_allowed in messages.
Context limited to 131072 tokens, packed into sandwiches with JSON index.


Limitations:
last_sent_tokens tracks only the last context, not per-chat history.
No support for ZIP uploads or git clone.
Limited file filtering (only by project_id).
No integration with llm_hands.


Testing:
API: curl -H "Cookie: session_id=<your_session_id>" "http://vps.vpn:8008/api/chat/get_stats?chat_id=1".
Database: sqlite3 /app/data/multichat.db "SELECT * FROM projects;".
Unit tests: docker exec colloquium-core python -m unittest tests.test_replication -v.


