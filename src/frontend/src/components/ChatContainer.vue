<!-- /frontend/rtm/src/components/ChatContainer.vue, updated 2025-07-19 21:15 EEST -->
<template>
  <div class="chat-container">
    <div class="tabs">
      <button :class="{ active: activeTab === 'chat' }" @click="activeTab = 'chat'">Чат</button>
      <button :class="{ active: activeTab === 'debug' }" @click="activeTab = 'debug'">Отладка</button>
    </div>
    <dialog id="createChatModal" ref="createChatModal">
      <h3>Создать новый чат</h3>
      <input v-model="newChatDescription" placeholder="Описание чата" />
      <button @click="chatStore.createChat(newChatDescription)">Создать</button>
      <button @click="chatStore.closeCreateChatModal">Отмена</button>
    </dialog>
    <dialog ref="fileConfirmModal">
      <h3>Подтверждение имени файла</h3>
      <input v-model="pendingFileName" placeholder="Полное имя файла (например, trade_report/src/test.rs)" list="fileSuggestions" />
      <datalist id="fileSuggestions">
        <option v-for="file in fileStore.files" :value="file.file_name" :key="file.id" />
      </datalist>
      <button @click="confirmFileUpload">Загрузить</button>
      <button @click="closeFileConfirmModal">Отмена</button>
    </dialog>
    <dialog ref="editPostModal">
      <h3>Редактировать сообщение</h3>
      <textarea
        v-model="editMessageContent"
        placeholder="Новое сообщение"
        rows="4"
        wrap="soft"
        @input="autoResize($event, 'editMessageContent')"
      ></textarea>
      <button @click="editPost">Сохранить</button>
      <button @click="closeEditPostModal">Отмена</button>
    </dialog>
    <p v-if="chatStore.chatError || fileStore.chatError || authStore.backendError" class="error">
      {{ chatStore.chatError || fileStore.chatError || authStore.backendError }}
    </p>
    <div v-if="activeTab === 'chat'" class="messages" ref="messagesContainer">
      <div v-for="(msg, index) in chatStore.history" :key="msg.id" :class="['message', { 'admin-message': msg.user_id === 1, 'agent-message': msg.user_id === 2, 'deleted': msg.action === 'delete' }]">
        <p v-if="msg.action !== 'delete'" v-html="formatMessage(msg.message, msg.file_names, msg.user_name, msg.timestamp)"></p>
        <p v-else class="deleted-post">
          <strong>{{ msg.user_name }}</strong> ({{ formatTimestamp(msg.timestamp) }}): [Post deleted]
        </p>
        <button v-if="msg.action !== 'delete' && (authStore.userId === msg.user_id || authStore.userRole === 'admin')" class="edit-post" @click="openEditPostModal(msg.id, msg.message)">✎</button>
        <button v-if="msg.action !== 'delete' && (authStore.userId === msg.user_id || authStore.userRole === 'admin')" class="delete-post" @click="chatStore.deletePost(msg.id, msg.user_id, authStore.userId, authStore.userRole)">X</button>
        <hr v-if="index < chatStore.history.length - 1" class="message-separator" />
      </div>
    </div>
    <div v-if="activeTab === 'debug'" class="debug-logs">
      <h3>Логи отладки</h3>
      <div v-for="(log, index) in debugLogs" :key="index" :class="['log-entry', log.type]">
        <p>{{ log.message }} ({{ log.timestamp }})</p>
      </div>
    </div>
    <div v-if="activeTab === 'chat'" class="message-input">
      <p v-if="chatStore.status.status === 'busy'" class="processing-status">
        Идёт обработка запроса {{ chatStore.status.actor }}, прошло {{ chatStore.status.elapsed }} секунд...
      </p>
      <textarea
        v-model="newMessage"
        ref="messageInput"
        placeholder="Сообщение (@agent для команд или <code_file> для кода)"
        rows="4"
        wrap="soft"
        @input="autoResize($event, 'messageInput')"
        @keyup.enter="sendMessage"
      ></textarea>
      <div v-if="fileStore.pendingAttachment" class="attachment-preview">
        <p>Прикреплён: {{ fileStore.pendingAttachment.file_name }} (@attached_file#{{ fileStore.pendingAttachment.file_id }})</p>
        <button @click="fileStore.clearAttachment">Очистить</button>
      </div>
      <input type="file" @change="openFileConfirmModal" />
      <button @click="openCreateChatModal">Ответвление</button>
    </div>
  </div>
</template>

<script>
import { defineComponent, inject } from 'vue'
import { useChatStore } from '../stores/chat'
import { useFileStore } from '../stores/files'
import { useAuthStore } from '../stores/auth'

export default defineComponent({
  name: 'ChatContainer',
  data() {
    return {
      newMessage: '',
      newChatDescription: '',
      pendingFile: null,
      pendingFileName: '',
      editMessageId: null,
      editMessageContent: '',
      pollInterval: null,
      activeTab: 'chat',
      debugLogs: []
    }
  },
  setup() {
    const chatStore = useChatStore()
    const fileStore = useFileStore()
    const authStore = useAuthStore()
    const mitt = inject('mitt')
    return { chatStore, fileStore, authStore, mitt }
  },
  mounted() {
    this.startPolling()
    this.scrollToBottom()
    this.mitt.on('select-file', this.handleSelectFile)
    this.$nextTick(() => {
      this.autoResize({ target: this.$refs.messageInput }, 'messageInput')
    })
    this.overrideConsole()
    this.fetchBackendLogs()
    this.backendLogInterval = setInterval(this.fetchBackendLogs, 30000)
  },
  beforeUnmount() {
    this.stopPolling()
    this.mitt.off('select-file', this.handleSelectFile)
    if (this.backendLogInterval) {
      clearInterval(this.backendLogInterval)
    }
  },
  updated() {
    if (this.activeTab === 'chat') {
      this.scrollToBottom()
    }
  },
  methods: {
    overrideConsole() {
      const originalError = console.error
      const originalWarn = console.warn
      console.error = (...args) => {
        this.debugLogs.push({
          type: 'error',
          message: args.join(' '),
          timestamp: new Date().toLocaleString('ru-RU')
        })
        originalError.apply(console, args)
      }
      console.warn = (...args) => {
        this.debugLogs.push({
          type: 'warn',
          message: args.join(' '),
          timestamp: new Date().toLocaleString('ru-RU')
        })
        originalWarn.apply(console, args)
      }
    },
    async fetchBackendLogs() {
      try {
        const res = await fetch(`${this.chatStore.apiUrl}/chat/logs`, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.ok) {
          const data = await res.json()
          this.debugLogs.push(...data.logs.map(log => ({
            type: log.level.toLowerCase(),
            message: log.message,
            timestamp: new Date(log.timestamp * 1000).toLocaleString('ru-RU')
          })))
          console.log('Fetched backend logs:', data)
        } else {
          console.error('Error fetching backend logs:', await res.json())
          this.debugLogs.push({
            type: 'error',
            message: 'Failed to fetch backend logs: Invalid response',
            timestamp: new Date().toLocaleString('ru-RU')
          })
        }
      } catch (e) {
        console.error('Error fetching backend logs:', e)
        this.debugLogs.push({
          type: 'error',
          message: `Failed to fetch backend logs: ${e.message}`,
          timestamp: new Date().toLocaleString('ru-RU')
        })
      }
    },
    startPolling() {
      if (this.chatStore.selectedChatId !== null) {
        this.pollInterval = setInterval(() => {
          this.chatStore.waitChanges = true
          this.chatStore.fetchHistory()
        }, 15000)
      }
    },
    stopPolling() {
      if (this.pollInterval) {
        clearInterval(this.pollInterval)
        this.pollInterval = null
      }
    },
    scrollToBottom() {
      this.$nextTick(() => {
        const container = this.$refs.messagesContainer
        if (container) {
          container.scrollTop = container.scrollHeight
        }
      })
    },
    openFileConfirmModal(event) {
      this.pendingFile = event.target.files[0]
      this.pendingFileName = this.pendingFile?.name || ''
      this.$refs.fileConfirmModal.showModal()
    },
    closeFileConfirmModal() {
      this.pendingFile = null
      this.pendingFileName = ''
      this.$refs.fileConfirmModal.close()
    },
    async confirmFileUpload() {
      if (!this.pendingFile || !this.pendingFileName) return
      try {
        const response = await this.fileStore.uploadFile(this.pendingFile, this.pendingFileName, this.chatStore.selectedChatId)
        console.log('Upload response:', JSON.stringify(response))
        if (response && response.status === 'ok' && response.file_id) {
          this.newMessage += ` @attach#${response.file_id}`
          this.fileStore.pendingAttachment = { file_id: response.file_id, file_name: this.pendingFileName }
        } else {
          console.error('Invalid upload response:', response)
          this.fileStore.chatError = 'Failed to upload file: Invalid response'
          this.debugLogs.push({
            type: 'error',
            message: 'Failed to upload file: Invalid response',
            timestamp: new Date().toLocaleString('ru-RU')
          })
        }
        this.closeFileConfirmModal()
      } catch (error) {
        console.error('Error uploading file:', error)
        this.fileStore.chatError = `Failed to upload file: ${error.message}`
        this.debugLogs.push({
          type: 'error',
          message: `Failed to upload file: ${error.message}`,
          timestamp: new Date().toLocaleString('ru-RU')
        })
      }
    },
    async sendMessage(event) {
      if (event.shiftKey) return
      if (!this.newMessage && !this.fileStore.pendingAttachment) return
      if (this.chatStore.status.status === 'busy') {
        console.warn('Отправка заблокирована: идёт обработка запроса', this.chatStore.status);
        this.chatStore.chatError = 'Отправка заблокирована: идёт обработка запроса';
        this.debugLogs.push({
          type: 'warn',
          message: `Отправка заблокирована: идёт обработка запроса ${this.chatStore.status.actor} (${this.chatStore.status.elapsed} секунд)`,
          timestamp: new Date().toLocaleString('ru-RU')
        });
        return;
      }
      let finalMessage = this.newMessage.trim()
      if (this.fileStore.pendingAttachment) {
        finalMessage += ` @attach#${this.fileStore.pendingAttachment.file_id}`
      }
      try {
        await this.chatStore.sendMessage(finalMessage)
        this.newMessage = ''
        this.fileStore.clearAttachment()
        this.$nextTick(() => {
          this.autoResize({ target: this.$refs.messageInput }, 'messageInput')
        })
      } catch (error) {
        console.error('Error sending message:', error)
        this.chatStore.chatError = `Failed to send message: ${error.message}`
        this.debugLogs.push({
          type: 'error',
          message: `Failed to send message: ${error.message}`,
          timestamp: new Date().toLocaleString('ru-RU')
        })
      }
    },
    openEditPostModal(postId, message) {
      this.editMessageId = postId
      this.editMessageContent = message
      this.$refs.editPostModal.showModal()
      this.$nextTick(() => {
        const textarea = this.$refs.editPostModal.querySelector('textarea')
        if (textarea) this.autoResize({ target: textarea }, 'editMessageContent')
      })
    },
    closeEditPostModal() {
      this.editMessageId = null
      this.editMessageContent = ''
      this.$refs.editPostModal.close()
    },
    async editPost() {
      if (!this.editMessageId || !this.editMessageContent) return
      try {
        await this.chatStore.editPost(this.editMessageId, this.editMessageContent)
        this.closeEditPostModal()
      } catch (error) {
        console.error('Error editing post:', error)
        this.chatStore.chatError = `Failed to edit post: ${error.message}`
        this.debugLogs.push({
          type: 'error',
          message: `Failed to edit post: ${error.message}`,
          timestamp: new Date().toLocaleString('ru-RU')
        })
      }
    },
    openCreateChatModal() {
      const parentMessageId = this.chatStore.history[this.chatStore.history.length - 1]?.id
      console.log('Opening create chat modal, parentMessageId:', parentMessageId)
      this.chatStore.openCreateChatModal(parentMessageId)
      this.newChatDescription = ''
      this.$refs.createChatModal.showModal()
    },
    handleSelectFile(fileId) {
      if (fileId) {
        this.newMessage += ` @attach#${fileId}`
        this.fileStore.pendingAttachment = this.fileStore.files.find(file => file.id === fileId) || null
      } else {
        console.error('Invalid fileId received:', fileId);
        this.fileStore.chatError = 'Invalid file selection';
        this.debugLogs.push({
          type: 'error',
          message: 'Invalid file selection',
          timestamp: new Date().toLocaleString('ru-RU')
        });
      }
      this.$refs.messageInput?.focus()
      this.$nextTick(() => {
        this.autoResize({ target: this.$refs.messageInput }, 'messageInput')
      })
    },
    autoResize(event, refName) {
      const textarea = refName === 'messageInput' ? this.$refs.messageInput : event.target
      if (!textarea) return
      textarea.style.height = 'auto'
      textarea.style.height = `${textarea.scrollHeight}px`
      console.log(`Auto-resized ${refName} to height: ${textarea.style.height}`)
    },
    formatMessage(message, fileNames, userName, timestamp) {
      let formatted = message ? message.replace(/</g, '<').replace(/>/g, '>') : '[Post deleted]'
      // Обработка @attached_file
      if (fileNames && fileNames.length) {
        fileNames.forEach(file => {
          const date = new Date(file.ts * 1000).toLocaleString('ru-RU', {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
          })
          const regex = new RegExp(`@attach#${file.file_id}\\b`, 'g')
          formatted = formatted.replace(regex, `<a href="#" @click.prevent="$emit('select-file', ${file.file_id})">File: ${file.file_name} (@attached_file#${file.file_id}, ${date})</a>`)
        })
      }
      // Обработка <code_patch>
      formatted = formatted.replace(
        /<code_patch file_id="(\d+)">([\s\S]*?)<\/code_patch>/g,
        (match, fileId, content) => {
          const lines = content.split('\n').map(line => {
            if (line.startsWith('-') && !line.startsWith('---')) {
              return `<span class="patch-removed">${line}</span>`
            } else if (line.startsWith('+')) {
              return `<span class="patch-added">${line}</span>`
            } else {
              return `<span class="patch-unchanged">${line}</span>`
            }
          }).join('\n')
          return `<pre class="code-patch">${lines}</pre>`
        }
      )
      // Обработка <shell_code>, <stdout>, <stderr>
      formatted = formatted.replace(
        /<shell_code(?:\s+[^>]*)?>([\s\S]*?)<\/shell_code>/g,
        (match, content) => `<pre class="shell-code">${content}</pre>`
      )
      formatted = formatted.replace(
        /<stdout>([\s\S]*?)<\/stdout>/g,
        (match, content) => `<pre class="stdout">${content}</pre>`
      )
      formatted = formatted.replace(
        /<stderr>([\s\S]*?)<\/stderr>/g,
        (match, content) => `<pre class="stderr">${content}</pre>`
      )
      const dateTime = new Date(timestamp * 1000).toLocaleString('ru-RU', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      })
      return `<strong>${userName}</strong> (${dateTime}): ${formatted}`
    },
    formatTimestamp(timestamp) {
      return new Date(timestamp * 1000).toLocaleString('ru-RU')
    }
  },
  watch: {
    'chatStore.selectedChatId': function(newChatId) {
      console.log('ChatContainer selectedChatId updated:', newChatId)
      this.stopPolling()
      if (newChatId !== null) {
        this.chatStore.waitChanges = false
        this.chatStore.fetchHistory()
        this.fileStore.fetchFiles()
        this.startPolling()
      }
    },
    'chatStore.history': {
      async handler(newHistory, oldHistory) {
        console.log('Deleted posts:', newHistory.filter(post => post.action === 'delete'))
        this.scrollToBottom()
        if (oldHistory) {
          const hasAttach = newHistory.some(post => post.message && post.message.includes('@attach#'))
          if (hasAttach) {
            console.log('Post with @attach# detected in history, fetching files')
            try {
              await this.fileStore.fetchFiles()
            } catch (error) {
              console.error('Error fetching files:', error)
              this.debugLogs.push({
                type: 'error',
                message: `Failed to fetch files: ${error.message}`,
                timestamp: new Date().toLocaleString('ru-RU')
              })
            }
          }
        }
      },
      deep: true,
      immediate: true
    }
  }
})
</script>

<style>
.chat-container {
  flex-grow: 1;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  min-width: 800px;
  background: #333;
}
@media (prefers-color-scheme: light) {
  .chat-container {
    background: #f0f0f0;
  }
}
.tabs {
  display: flex;
  margin: 10px;
}
.tabs button {
  padding: 5px 10px;
  margin-right: 5px;
  border: none;
  border-radius: 3px;
  background: #444;
  color: #eee;
  cursor: pointer;
}
.tabs button.active {
  background: #007bff;
}
@media (prefers-color-scheme: light) {
  .tabs button {
    background: #ccc;
    color: #333;
  }
  .tabs button.active {
    background: #0056b3;
    color: #fff;
  }
}
.messages {
  max-height: 80vh;
  overflow-y: auto;
  margin: 0 10px 10px 10px;
}
.debug-logs {
  max-height: 80vh;
  overflow-y: auto;
  margin: 0 10px 10px 10px;
  background: #222;
  padding: 10px;
  border-radius: 5px;
}
@media (prefers-color-scheme: light) {
  .debug-logs {
    background: #e0e0e0;
  }
}
.log-entry {
  margin: 5px 0;
}
.log-entry.error {
  color: red;
}
.log-entry.warn {
  color: orange;
}
.message {
  margin: 10px 0;
  display: flex;
  align-items: center;
  padding: 8px;
  background: #333;
}
.admin-message {
  background: #555;
}
.agent-message {
  margin-left: 20px;
}
@media (prefers-color-scheme: light) {
  .message {
    background: #f0f0f0;
  }
  .admin-message {
    background: #e0e0e0;
  }
  .agent-message {
    margin-left: 20px;
  }
}
.message p {
  margin: 0;
  flex-grow: 1;
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .message p {
    color: #333;
  }
}
.message-separator {
  border: 0;
  border-top: 1px solid #555;
  margin: 5px 0;
}
@media (prefers-color-scheme: light) {
  .message-separator {
    border-top: 1px solid #ccc;
  }
}
.edit-post, .delete-post {
  margin-left: 10px;
  padding: 2px 8px;
  font-size: 12px;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}
.edit-post {
  background: #007bff;
  color: #fff;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.edit-post:hover {
  background: #0056b3;
}
.delete-post {
  background: #ff4444;
  color: #fff;
}
.delete-post:hover {
  background: #cc0000;
}
.attachment-preview {
  margin: 10px;
  display: flex;
  align-items: center;
}
.attachment-preview p {
  margin: 0;
  flex-grow: 1;
}
.attachment-preview button {
  padding: 2px 8px;
  font-size: 12px;
}
input, textarea {
  margin: 10px;
  padding: 5px;
  width: calc(100% - 30px);
  border: 1px solid #ccc;
  border-radius: 3px;
  background: #444;
  color: #eee;
  font-family: inherit;
}
textarea {
  min-height: 80px;
  resize: vertical;
  overflow-y: auto;
}
@media (prefers-color-scheme: light) {
  input, textarea {
    background: #fff;
    color: #333;
    border: 1px solid #999;
  }
}
button {
  margin: 5px;
  padding: 5px 10px;
}
dialog {
  padding: 20px;
  border: 1px solid #ccc;
  border-radius: 5px;
}
dialog input, dialog textarea {
  width: 100%;
  margin-bottom: 10px;
}
.error {
  color: red;
  margin: 10px;
}
.processing-status {
  margin: 10px;
  color: #ccccaa; /* Нежный жёлтый для тёмной темы */
}
@media (prefers-color-scheme: light) {
  .processing-status {
    color: #666;
  }
}
.deleted-post {
  color: #888;
  font-style: italic;
}
.code-patch, .shell-code, .stdout, .stderr {
  background: #222;
  padding: 8px;
  border-radius: 5px;
  font-family: monospace;
  white-space: pre-wrap;
  border: 1px solid;
}
@media (prefers-color-scheme: light) {
  .code-patch, .shell-code, .stdout, .stderr {
    background: #f5f5f5;
  }
}
.code-patch {
  border-color: #555;
}
.shell-code {
  color: #ff8c00;
  border-color: #ff8c00;
}
@media (prefers-color-scheme: light) {
  .shell-code {
    color: #d2691e;
    border-color: #d2691e;
  }
}
.stdout {
  color: #d3d3d3;
  border-color: #d3d3d3;
}
@media (prefers-color-scheme: light) {
  .stdout {
    color: #666;
    border-color: #666;
  }
}
.stderr {
  color: #ff4444;
  border-color: #ff4444;
}
@media (prefers-color-scheme: light) {
  .stderr {
    color: #cc0000;
    border-color: #cc0000;
  }
}
.patch-removed {
  color: #ff4444;
}
.patch-added {
  color: #00cc00;
}
.patch-unchanged {
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .patch-unchanged {
    color: #333;
  }
}
</style>