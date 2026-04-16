Frontend Technical Documentation for Colloquium Chat Server
Overview
The frontend of the Colloquium Chat Server is a single-page application (SPA) built using Vue.js 3 with Pinia for state management. It provides a three-panel interface for multi-user chat, file management, and project selection, integrated with a FastAPI backend. The frontend supports real-time chat updates, file uploads, hierarchical chat navigation, and formatted message rendering with support for code patches, shell commands, and file links. It is designed for collaborative code analysis, particularly for the Rust-based trading reports server.
Frontend Architecture

Framework: Vue.js 3 with Composition API.
State Management: Pinia for managing authentication, chat, and file states.
Event Bus: Mitt for inter-component communication (e.g., file selection).
Styling: CSS with external chat.css for chat-specific styles, supporting light and dark themes via prefers-color-scheme.
API Interaction: Uses fetch for communication with the backend at http://vps.vpn:8008/api.
Deployment: Served via npm run serve, proxied through Nginx to handle /api/* requests.

Components
/frontend/rtm/src/components/App.vue

Purpose: Root component, orchestrates the three-panel layout.
Features:
Conditionally renders Login.vue if authStore.isLoggedIn is false.
Displays a three-panel layout (SideBar.vue, ChatContainer.vue, RightPanel.vue) when authenticated.
Initializes session check via authStore.checkSession() on mount.


Dependencies: Login.vue, SideBar.vue, ChatContainer.vue, RightPanel.vue, auth.js.

/frontend/rtm/src/components/SideBar.vue

Purpose: Displays the chat list and token statistics.
Features:
Renders hierarchical chat list using ChatTree.vue.
Allows chat selection via chatStore.setChatId.
Displays token usage (chatStore.stats.tokens) from /api/chat/get_stats.
Collapsible to 30px using a toggle button (▶/◄).


Dependencies: ChatTree.vue, chat.js, auth.js.

/frontend/rtm/src/components/ChatContainer.vue

Purpose: Manages chat message display, input, and modals for chat creation, file uploads, and post editing.
Features:
Renders chat history (chatStore.history) with formatted messages using computed property formattedMessages.
Supports sending messages with @attached_file#<file_id> and @attach_dir#<dir_name>.
Blocks message sending when chatStore.status.status is 'busy', showing "Отправка заблокирована: идёт обработка запроса".
Formats messages with:
@attach#<file_id> and @attached_file#<file_id> as clickable <span class="file-link"> links, fetching metadata from fileStore.files.
Unresolved file references as <span class="file-unavailable">Файл <file_id> удалён или недоступен</span>.
<code_patch>, <shell_code>, <stdout>, <stderr>, <mismatch> in styled <pre> blocks.
<pre class="plain-pre"> for messages with newlines and no special tags (monospace, white-space: pre-wrap).


Displays <dialog> modals for:
File preview (filePreviewModal) on clicking file links, fetching content via /api/chat/file_contents.
Chat creation (createChatModal).
File upload confirmation (fileConfirmModal).
Post editing (editPostModal).


Polls /api/chat/get every 1 second with wait_changes=1 for updates when a chat is selected.
Uses chat_actions.js for reusable actions (sendMessage, editPost, toggleModal, etc.).


Dependencies: chat.js, files.js, auth.js, chat_actions.js, chat.css.

/frontend/rtm/src/components/RightPanel.vue

Purpose: Manages project selection and file tree display.
Features:
Lists projects via /api/project/list.
Allows creating/editing projects via /api/project/create and /api/project/update.
Renders file tree using FileTree.vue and FileManager.vue with files from fileStore.files.
Supports file filtering by project_id.
Collapsible to 30px using a toggle button (▶/◄).


Dependencies: FileTree.vue, FileManager.vue, files.js.

/frontend/rtm/src/components/ChatTree.vue

Purpose: Renders hierarchical chat tree.
Features:
Displays chats with expand/collapse functionality (▼/▶).
Emits select-chat event to chatStore.setChatId when a chat is clicked.


Dependencies: chat.js.

/frontend/rtm/src/components/FileTree.vue

Purpose: Renders hierarchical file tree.
Features:
Displays directories and files with expand/collapse (▼/▶).
Emits select-file event to add @attached_file#<file_id> to ChatContainer.vue's message input.
Supports file deletion for admins or file owners via fileStore.deleteFile.


Dependencies: files.js, auth.js.

/frontend/rtm/src/components/FileManager.vue

Purpose: Manages file tree rendering and operations.
Features:
Builds file tree from fileStore.files.
Handles file selection, deletion, and updates via fileStore actions.


Dependencies: files.js.

/frontend/rtm/src/components/Login.vue

Purpose: Provides user login interface.
Features:
Input fields for username and password.
Calls authStore.login on submit.
Displays authStore.loginError on authentication failure.


Dependencies: auth.js.

/frontend/rtm/src/main.js

Purpose: Initializes Vue app and Pinia.
Features:
Creates Vue app and mounts to #app.
Initializes Pinia for state management.
Provides mitt event bus for inter-component communication (e.g., select-file events).


Dependencies: Vue.js, Pinia, Mitt.

Stores
/frontend/rtm/src/stores/auth.js

Purpose: Manages authentication state.
Store: useAuthStore
State:
isLoggedIn: Boolean indicating authentication status.
username, password: Credentials for login.
loginError: Error message for failed logins.
backendError: Indicates backend errors (e.g., 500, 502).
isCheckingSession: Tracks session check status.
userRole: User role (admin, mcp, developer, or LLM).
userId: Unique user ID.
apiUrl: Backend URL (http://vps.vpn:8008/api).


Actions:
checkSession(): Verifies session via /api/chat/list and fetches user info via /api/user/info.
login(username, password): Authenticates via /api/login, sets userRole and userId.
logout(): Clears session via /api/logout.



/frontend/rtm/src/stores/chat.js

Purpose: Manages chat state and operations.
Store: useChatStore
State:
chats: Array of accessible chats.
selectedChatId: Currently selected chat ID.
history: Object with post_id as keys, containing chat messages.
quotes: Object with quote_id as keys, containing quoted messages.
newChatDescription, newChatParentMessageId: Data for creating new chats.
chatError, backendError: Error states.
apiUrl: Backend URL.
waitChanges: Controls polling with wait_changes=1.
need_full_history: Triggers full history fetch.
stats: Token and source usage stats (tokens, num_sources_used).
status: Replication status (status, actor, elapsed).
pollingInterval, isPolling: Manages polling state.
awaited_to_del: Tracks posts awaiting deletion.


Actions:
fetchChats(): Fetches chat list via /api/chat/list.
fetchHistory(): Fetches chat history via /api/chat/get, supports wait_changes=1.
scrollToBottom(): Scrolls chat to the latest message.
fetchChatStats(): Fetches stats via /api/chat/get_stats.
sendMessage(message): Sends message via /api/chat/post.
editPost(postId, message): Edits message via /api/chat/edit_post.
deletePost(postId, postUserId, userId, userRole): Deletes message via /api/chat/delete_post if authorized.
createChat(description): Creates chat via /api/chat/create.
setChatId(chatId): Sets active chat, notifies /api/chat/notify_switch.
openCreateChatModal(parentMessageId), closeCreateChatModal(): Manages chat creation modal.
buildChatTree(chats): Builds hierarchical chat tree.
dbFetchParentMsg(parent_msg_id): Fetches parent message via /api/chat/get_parent_msg.



/frontend/rtm/src/stores/files.js

Purpose: Manages file-related state and operations.
Store: useFileStore
State:
files: Array of files from /api/chat/list_files.
pendingAttachment: Temporary storage for file uploads (file_id, file_name).
chatError, backendError: Error states.
apiUrl: Backend URL.


Actions:
fetchFiles(project_id): Fetches files via /api/chat/list_files.
deleteFile(fileId): Deletes file via /api/chat/delete_file.
updateFile(fileId, event): Updates file via /api/chat/update_file.
uploadFile(file, fileName, chatId): Uploads file via /api/chat/upload_file.
clearAttachment(): Clears pendingAttachment.



API Routes (Frontend Interaction)
The frontend interacts with the following backend API endpoints:

GET /api/chat/list: Retrieves list of accessible chats.
POST /api/chat/create: Creates a new chat with optional parent_msg_id.
POST /api/chat/delete: Deletes a chat if no sub-chats exist and user has permission.
GET /api/chat/get?chat_id=<id>&wait_changes=<0|1>: Retrieves chat history, supports polling with wait_changes=1.
POST /api/chat/notify_switch: Updates active_chat in sessions table.
POST /api/chat/post: Adds a new message, triggers replication.
POST /api/chat/edit_post: Edits a message if authorized.
POST /api/chat/delete_post: Deletes a message if authorized.
GET /api/chat/get_stats?chat_id=<id>: Returns chat statistics (tokens, num_sources_used).
GET /api/chat/get_parent_msg?post_id=<id>: Returns parent message details.
GET /api/chat/logs: Returns last 100 log entries (ERROR or WARNING).
POST /api/login: Authenticates user, sets session_id cookie.
POST /api/logout: Clears session and cookie.
GET /api/user/info: Returns user role and ID.
POST /api/chat/upload_file: Uploads a file to attached_files.
POST /api/chat/update_file: Updates a file’s content and name.
POST /api/chat/delete_file: Deletes a file if not referenced and user is admin.
GET /api/chat/list_files: Lists files, optionally filtered by project_id.
GET /api/chat/file_contents?file_id=<id>: Retrieves file content.
GET /api/project/list: Lists all projects.
POST /api/project/create: Creates a new project.
POST /api/project/update: Updates a project.
POST /api/project/select: Sets active project.

Logging

Frontend Logs:

Location: /opt/docker/mcp-server/frontend/rtm/npm-debug.log.
Format: Standard JavaScript console logs with timestamps in browser’s locale (via toLocaleString()).
Content:
Action execution: Action executed: <action> (e.g., send message, open edit post modal).
Message formatting: Formatted file link for file_id: <id>, Processed quote#<quote_id>.
Polling: Polling with wait_changes: <value>, Starting polling for chat_id: <id>.
Errors: Error <action>: <error.message> (via logError).
UI updates: DOM updated for history change, Scrolled to bottom.
File operations: Fetched files count: <count>.


Conditional Logging: Logs enabled only in development mode (process.env.NODE_ENV === 'development') to reduce overhead in production.


Access:

View via browser console or cat /opt/docker/mcp-server/frontend/rtm/npm-debug.log.



Configuration

Environment Variables:
VITE_API_URL: Backend API URL (defaults to http://vps.vpn:8008/api).
Defined in .env or build configuration.


CSS:
External file: /frontend/rtm/src/styles/chat.css.
Supports light/dark themes via prefers-color-scheme.
Styles for messages (file-link, quote, code-patch, etc.), modals, and file preview.



Recent Changes (2025-07-22)

Message Formatting Optimization:

Introduced computed property formattedMessages in ChatContainer.vue to cache formatted messages, eliminating input lag during text entry.
Optimized formatMessage to use a single regular expression /@(attach|attached_file)#(\d+)/g with a callback for file link formatting, reducing code duplication.
Replaced hard-coded Russian locale (toLocaleString('ru-RU')) with browser locale (toLocaleString(undefined)) for date formatting.


Bug Fixes:

Fixed Uncaught ReferenceError: openEditPostModal is not defined by ensuring correct method binding in ChatContainer.vue.
Resolved excessive polling of /api/chat/get by resetting need_full_history after each successful fetch in chat.js.
Eliminated redundant logging of file replacements (Replaced attach#<file_id>) in formatMessage, using targeted Formatted file link for file_id: <id> only for matched files.


Performance Improvements:

Reduced re-rendering by caching formatted messages in formattedMessages, preventing unnecessary calls to formatMessage on input events.
Optimized file link processing by extracting file_id from messages before replacement, avoiding iteration over all fileStore.files.



Testing

Manual Testing:

Login: Verify authStore.login via /api/login with valid/invalid credentials.
Chat Selection: Select chats in SideBar.vue, check /api/chat/notify_switch and chatStore.setChatId.
Message Sending: Send messages with @attached_file#<file_id>, verify /api/chat/post response and UI update.
Message Editing: Edit posts via editPostModal, confirm /api/chat/edit_post works.
File Preview: Click file links, ensure /api/chat/file_contents returns content and modal displays correctly.
Polling: Check /api/chat/get?wait_changes=1 in network tab, verify 1-second interval and no changes response handling.
Localization: Test date formats in different browser locales (e.g., en-US, de-DE).


API Testing:
curl -H "Cookie: session_id=<your_session_id>" "http://vps.vpn:8008/api/chat/get?chat_id=1&wait_changes=1"
curl -H "Cookie: session_id=<your_session_id>" -X POST -H "Content-Type: application/json" -d '{"chat_id": 1, "message": "Test message"}' "http://vps.vpn:8008/api/chat/post"
curl -H "Cookie: session_id=<your_session_id>" "http://vps.vpn:8008/api/chat/file_contents?file_id=72"


Console Logs:

Verify logs in browser console:
Formatted file link for file_id: <id>
Clicked send message
Clicked edit post: <postId>
Action executed: <action>
Fetched files count: <count>





Notes

Supported File Types: .rs (Rust), .vue (Vue.js), .js (JavaScript), .py (Python), .rulz (documentation).
Performance Considerations:
formattedMessages ensures minimal re-rendering during text input.
Polling optimized with wait_changes=1 and 1-second interval.
File links rely on fileStore.files, fetched once per chat selection.


Error Handling:
Errors displayed via chatStore.chatError, fileStore.chatError, or authStore.backendError.
Backend errors (500, 502) logged and trigger backendError state.


Localization:
Dates use browser locale via toLocaleString(undefined), supporting international users.


Event Bus:
mitt used for select-file events from FileTree.vue to ChatContainer.vue.



Future Improvements

Code Optimization:
Extract formatMessage into a separate utility (message_formatter.js) to reduce ChatContainer.vue size (<500 lines).
Consolidate modal handling in chat_actions.js using a single handleModal function with callbacks.
Wrap console logs in if (process.env.NODE_ENV === 'development') to disable in production.


Tag Processing:
Unify processing of <code_patch>, <shell_code>, <stdout>, <stderr>, <mismatch> with a single regular expression and callback.


Testing:
Add unit tests for formatMessage and modal interactions using Vue Test Utils.


Accessibility:
Add ARIA attributes to modals and file links for better screen reader support.


Performance:
Implement lazy loading for long chat histories to reduce initial render time.


