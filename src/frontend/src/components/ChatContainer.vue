<!-- /frontend/rtm/src/components/ChatContainer.vue, updated 2025-07-22 18:15 EEST -->
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
      <button @click="doModal('createChatModal', false)">Отмена</button>
    </dialog>
    <dialog ref="fileConfirmModal" id="fileConfirmModal">
      <h3>Подтверждение имени файла</h3>
      <input v-model="pendingFileName" placeholder="Полное имя файла (например, trade_report/src/test.rs)" list="fileSuggestions" />
      <datalist id="fileSuggestions">
        <option v-for="file in fileStore.files" :value="file.file_name" :key="file.id" />
      </datalist>
      <button @click="confirmFileUpload">Загрузить</button>
      <button @click="doModal('fileConfirmModal', false)">Отмена</button>
    </dialog>
    <dialog ref="editPostModal" id="editPostModal">
      <h3>Редактировать сообщение</h3>
      <textarea
        v-model="editMessageContent"
        placeholder="Новое сообщение"
        rows="4"
        wrap="soft"
        @input="autoResize($event, 'editMessageContent')"
      ></textarea>
      <button @click="editPost">Сохранить</button>
      <button @click="doModal('editPostModal', false)">Отмена</button>
    </dialog>
    <dialog ref="filePreviewModal" id="filePreviewModal" class="file-preview-modal">
      <h3>Предпросмотр файла</h3>
      <pre class="file-preview">{{ filePreviewContent }}</pre>
      <button @click="doModal('filePreviewModal', false)">Закрыть</button>
    </dialog>
    <p v-if="chatStore.chatError || fileStore.chatError || authStore.backendError" class="error">
      {{ chatStore.chatError || fileStore.chatError || authStore.backendError }}
    </p>
    <div v-if="activeTab === 'chat'" class="messages" ref="messagesContainer" @click="handleMessageClick">
      <div v-for="(msg, index) in formattedMessages" :key="msg.id" :class="['message', { 'admin-message': msg.user_id === 1, 'agent-message': msg.user_id === 2 }]" :id="'post_' + msg.id">
        <div class="message-header">
          <div v-if="authStore.userId === msg.user_id || authStore.userRole === 'admin'" class="message-actions">
            <button class="edit-post" @click="doModal('editPostModal', true, { editMessageId: msg.id, editMessageContent: msg.message }, (comp) => { const textarea = comp.$refs.editPostModal.querySelector('textarea'); if (textarea) comp.autoResize({ target: textarea }, 'editMessageContent') })">✎</button>
            <button class="delete-post" @click="chatStore.deletePost(msg.id, msg.user_id, authStore.userId, authStore.userRole)">X</button>
          </div>
        </div>
        <div class="message-content">
          <pre v-html="msg.formatted"></pre>
        </div>
        <hr v-if="index < formattedMessages.length - 1" class="message-separator" />
      </div>
    </div>
    <div v-if="activeTab === 'debug'" class="debug-logs">
      <h3>Логи отладки</h3>
      <div>
        <label for="logFilters">Фильтры логов (через запятую, например: CHAT,FILE,ACTION,ERROR,UI):</label>
        <input id="logFilters" v-model="logFilters" placeholder="CHAT,FILE,ACTION,ERROR,UI" @input="updateLogFilters" />
      </div>
      <div v-for="(log, index) in debugLogs" :key="index" :class="['log-entry', log.type]">
        <p>{{ log.message }} ({{ log.timestamp }})</p>
      </div>
    </div>
    <div v-if="activeTab === 'chat'" class="message-input">
      <p v-if="chatStore.status.status === 'busy'" class="processing-status">
        Идёт обработка запроса {{ chatStore.status.actor || 'unknown' }}, прошло {{ chatStore.status.elapsed || 0 }} секунд...
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
      <input type="file" @change="doModal('fileConfirmModal', true, { pendingFile: $event.target.files[0], pendingFileName: $event.target.files[0]?.name || '' })" />
      <button @click="doModal('createChatModal', true, { newChatDescription: '' }, (comp) => comp.chatStore.openCreateChatModal(Object.values(comp.chatStore.history).sort((a, b) => a.id - b.id)[Object.values(comp.chatStore.history).length - 1]?.id))">Ответвление</button>
    </div>
  </div>
</template>

<script>
import { defineComponent, inject, nextTick, computed } from 'vue'
import { useChatStore } from '../stores/chat'
import { useFileStore } from '../stores/files'
import { useAuthStore } from '../stores/auth'
import { log_msg, log_error, set_show_logs } from '../utils/debugging'
import { handleModal, sendMessage, editPost, confirmFileUpload, showFilePreview, handleSelectFile } from '../utils/chat_actions'

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
      debugLogs: [],
      filePreviewContent: '',
      logFilters: ''
    }
  },
  setup() {
    const chatStore = useChatStore()
    const fileStore = useFileStore()
    const authStore = useAuthStore()
    const mitt = inject('mitt')
    return { chatStore, fileStore, authStore, mitt }
  },
  computed: {
    formattedMessages() {
      return Object.values(this.chatStore.history)
        .filter(post => post.action !== 'delete')
        .sort((a, b) => a.id - b.id)
        .map(msg => ({
          ...msg,
          formatted: this.formatMessage(msg.message, msg.user_name, msg.timestamp, this.chatStore.quotes, this.fileStore.files)
        }))
    }
  },
  mounted() {
    this.startPolling()
    this.mitt.on('select-file', (fileId) => this.handleSelectFile(fileId))
    this.$nextTick(() => {
      this.autoResize({ target: this.$refs.messageInput }, 'messageInput')
      log_msg('UI', 'Loaded chat.css styles')
    })
    this.overrideConsole()
    this.fetchBackendLogs()
    this.backendLogInterval = setInterval(this.fetchBackendLogs, 30000)
    this.logFilters = JSON.parse(localStorage.getItem('show_logs'))?.join(',') || 'CHAT,FILE,ACTION,ERROR,UI'
  },
  beforeUnmount() {
    this.stopPolling()
    this.mitt.off('select-file')
    if (this.backendLogInterval) {
      clearInterval(this.backendLogInterval)
    }
  },
  methods: {
    overrideConsole() {
      const originalError = console.error
      const originalWarn = console.warn
      console.error = (...args) => {
        const timeStr = new Date().toTimeString().split(' ')[0] + `.${new Date().getMilliseconds().toString().padStart(3, '0')}`
        this.debugLogs.push({
          type: 'error',
          message: args.join(' '),
          timestamp: timeStr
        })
        originalError.apply(console, args)
      }
      console.warn = (...args) => {
        const timeStr = new Date().toTimeString().split(' ')[0] + `.${new Date().getMilliseconds().toString().padStart(3, '0')}`
        this.debugLogs.push({
          type: 'warn',
          message: args.join(' '),
          timestamp: timeStr
        })
        originalWarn.apply(console, args)
      }
    },
    async fetchBackendLogs() {
      try {
        log_msg('UI', 'Fetching backend logs:', this.chatStore.apiUrl + '/chat/logs')
        const res = await fetch(`${this.chatStore.apiUrl}/chat/logs`, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.ok) {
          const data = await res.json()
          this.debugLogs.push(...data.logs.map(log => ({
            type: log.level.toLowerCase(),
            message: log.message,
            timestamp: new Date(log.timestamp * 1000).toTimeString().split(' ')[0] + `.${new Date(log.timestamp * 1000).getMilliseconds().toString().padStart(3, '0')}`
          })))
          log_msg('UI', 'Fetched backend logs:', data)
        } else {
          log_error(this, new Error('Invalid response'), 'fetch backend logs')
        }
      } catch (error) {
        log_error(this, error, 'fetch backend logs')
      }
    },
    startPolling() {
      if (this.chatStore.selectedChatId !== null) {
        log_msg('CHAT', 'Starting polling for chat_id:', this.chatStore.selectedChatId)
        this.pollInterval = setInterval(() => {
          if (this.chatStore.selectedChatId && !this.chatStore.isPolling) {
            this.chatStore.waitChanges = !this.chatStore.need_full_history
            this.chatStore.fetchHistory()
          }
        }, 1000)
      }
    },
    stopPolling() {
      if (this.pollInterval) {
        clearInterval(this.pollInterval)
        this.pollInterval = null
        log_msg('CHAT', 'Stopped polling')
      }
    },
    handleMessageClick(event) {
      const target = event.target.closest('.file-link')
      if (target) {
        const fileId = target.getAttribute('data-file-id')
        if (fileId) {
          this.showFilePreview(fileId)
        }
      }
    },
    autoResize(event, refName) {
      const textarea = refName === 'messageInput' ? this.$refs.messageInput : event.target
      if (!textarea) return
      textarea.style.height = 'auto'
      textarea.style.height = `${textarea.scrollHeight}px`
    },
    updateLogFilters() {
      set_show_logs(this.logFilters)
      log_msg('UI', 'Updated log filters:', this.logFilters)
    },
    doModal(modalRef, open, stateUpdates, callback) {
      log_msg('ACTION', `Called doModal for ${modalRef}`)
      handleModal(this, modalRef, open, stateUpdates, callback)
    },
    sendMessage(event) {
      log_msg('ACTION', 'Called sendMessage')
      sendMessage(this, event)
    },
    editPost() {
      log_msg('ACTION', 'Called editPost')
      editPost(this)
    },
    confirmFileUpload() {
      log_msg('ACTION', 'Called confirmFileUpload')
      confirmFileUpload(this)
    },
    showFilePreview(fileId) {
      log_msg('ACTION', 'Called showFilePreview:', fileId)
      showFilePreview(this, fileId)
    },
    handleSelectFile(fileId) {
      log_msg('ACTION', 'Called handleSelectFile:', fileId)
      handleSelectFile(this, fileId)
    },
    escapeHtml(text) {
      const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
      }
      return text.replace(/[&<>"']/g, char => map[char])
    },
    formatMessage(message, userName, timestamp, quotes, files) {
      log_msg('UI', 'Formatting message with fileStore.files:', files ? files.length : 0)
      let formatted = message || '[Post deleted]'
      if (!message) return `<strong>${userName}</strong> (${new Date(timestamp * 1000).toLocaleString()}: ${formatted}`
      formatted = formatted.replace(/@(attach|attached_file)#(\d+)/g, (match, type, fileId) => {
        const file = files.find(f => f.id === parseInt(fileId))
        if (file) {
          const date = new Date(file.ts * 1000).toLocaleString(undefined, {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
          })
          log_msg('UI', `Formatted file link for file_id: ${fileId}`)
          return `<span class="file-link" data-file-id="${file.id}">File: ${file.file_name} (@attached_file#${file.id}, ${date})</span>`
        }
        return `<span class="file-unavailable">Файл ${fileId} удалён или недоступен</span>`
      })
      if (quotes && typeof quotes === 'object') {
        Object.entries(quotes).forEach(([quoteId, quote]) => {
          if (!quote || !quote.message) return
          const regex = new RegExp(`@quote#${quoteId}\\b`, 'g')
          const quoteText = quote.message || '[Quote deleted]'
          const quoteUser = quote.user_name || 'unknown'
          const quoteDate = new Date(quote.timestamp * 1000).toLocaleString(undefined, {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
          })
          formatted = formatted.replace(regex, `<pre class="quote"><strong>${quoteUser}</strong> (${quoteDate}): ${quoteText}</pre>`)
          log_msg('UI', `Processed quote#${quoteId}`)
        })
      }
      formatted = formatted.replace(
        /<(code_patch|shell_code|stdout|stderr|mismatch)(?:\s+file_id="(\d+)")?>([\s\S]*?)<\/\1>/g,
        (match, tag, fileId, content) => {
          if (tag === 'code_patch' && fileId) {
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
          const escapedContent = this.escapeHtml(content)
          log_msg('UI', `Processed ${tag} tag`)
          return `<pre class="${tag}">${escapedContent}</pre>`
        }
      )
      const dateTime = new Date(timestamp * 1000).toLocaleString(undefined, {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      })
      return `<strong>${userName}</strong> (${dateTime}): ${formatted}`
    }
  }
})
</script>

<style src="../styles/chat.css"></style>
