<!-- /frontend/rtm/src/components/ChatContainer.vue, updated 2025-07-27 12:30 EEST -->
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
      <input v-model="pendingFileName"
             placeholder="Полное имя файла (например, trade_report/src/test.rs)"
             list="fileSuggestions" />
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
      <div class="file-preview"><code id="file-preview-code" class="framed-code" @click="handleCodeSelection">{{ filePreviewContent }}</code></div>
      <button @click="doModal('filePreviewModal', false)">Закрыть</button><span id="file-preview-info">Showing preview </span>
    </dialog>
    <p v-if="chatStore.chatError || fileStore.chatError || authStore.backendError" class="error">
      {{ chatStore.chatError || fileStore.chatError || authStore.backendError }}
    </p>
    <div v-if="activeTab === 'chat'" class="messages" ref="messagesContainer" @click="handleMessageClick">
      <div v-for="(msg, index) in formattedMessages" :key="msg.id"
           :class="['message', { 'admin-message': msg.user_id === 1, 'agent-message': msg.user_id === 2, 'reply-message': msg.reply_to }]"
           :id="'post_' + msg.id">
        <div class="message-header">
          <span class="message-title">{{ msg.user_name }} #[{{ msg.id }}] ({{ formatDateTime(msg.timestamp) }})
            <span v-if="msg.elapsed > 1" class="elapsed-time">&nbsp;думал {{ msg.elapsed.toFixed(1) }} секунд</span>
          </span>
          
          <div v-if="authStore.userId === msg.user_id || authStore.userRole === 'admin'" class="message-actions">
            <button class="edit-post"
                    @click="doModal('editPostModal', true, { editMessageId: msg.id, editMessageContent: msg.message },
                    (comp) => { const textarea = comp.$refs.editPostModal.querySelector('textarea');
                    if (textarea) comp.autoResize({ target: textarea }, 'editMessageContent') })">✎</button>
            <button class="delete-post"
                    @click="chatStore.deletePost(msg.id, msg.user_id, authStore.userId, authStore.userRole)">X</button>
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
      <input type="file"
             @change="doModal('fileConfirmModal', true, { pendingFile: $event.target.files[0],
             pendingFileName: $event.target.files[0]?.name || '' })" />
      <button @click="doModal('createChatModal', true, { newChatDescription: '' },
              (comp) => comp.chatStore.openCreateChatModal(Object.values(comp.chatStore.history)
              .sort((a, b) => a.id - b.id)[Object.values(comp.chatStore.history).length - 1]?.id))">Ответвление</button>
    </div>
  </div>
</template>

<script>
  import { defineComponent, inject, nextTick, computed } from 'vue'
  import { useChatStore } from '../stores/chat'
  import { useFileStore } from '../stores/files'
  import { useAuthStore } from '../stores/auth'
  import { log_msg, log_error, set_show_logs } from '../utils/debugging'
  import { handleModal, sendMessage, editPost, confirmFileUpload, showFilePreview, handleSelectFile, handleSelectDir } from '../utils/chat_actions'
  import { formatDateTime } from '../utils/common'
  import { formatMessage, reformatMessages, checkAwaitedFiles } from '../utils/chat_format'
  
  function lineFromOffset(lines, offset) {
    let line = 1;
    for (let i = 0; i < lines.length; i++) {
      if (offset <= lines[i].length) {
        return line;
      }
      offset -= (lines[i].length + 1); // +1 for \n
      line++;
    }
    return line;
  }

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
        logFilters: '',
        awaited_files: {}
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
            formatted: formatMessage(msg.message, msg.user_name, msg.timestamp, this.chatStore.quotes,
                                    this.fileStore.files, msg.id, this)
          }))
      }
    },
    mounted() {
      this.startPolling()
      this.mitt.on('select-dir', (dirPath) => this.handleSelectDir(dirPath))
      this.mitt.on('select-file', (fileId) => this.handleSelectFile(fileId))
      this.mitt.on('files-updated', () => reformatMessages(this))
      this.$nextTick(() => {
        this.autoResize({ target: this.$refs.messageInput }, 'messageInput')        
        this.applyHighlightJS()
      })
      this.overrideConsole()
      this.fetchBackendLogs()
      this.backendLogInterval = setInterval(this.fetchBackendLogs, 30000)
      this.logFilters = JSON.parse(localStorage.getItem('show_logs'))?.join(',') || 'CHAT,FILE,ACTION,ERROR,UI'      
    },
    beforeUnmount() {
      this.stopPolling()
      this.mitt.off('select-dir')
      this.mitt.off('select-file')
      this.mitt.off('files-updated')
      if (this.backendLogInterval) {
        clearInterval(this.backendLogInterval)
      }
    },
    watch: {
      'chatStore.history': {
        handler() {
          this.$nextTick(() => {
            this.applyHighlightJS();
          });
        },
        deep: true
      }
    },
    methods: {
      formatDateTime,
      applyHighlightJS() {
        if (window.hljs) {
          document.querySelectorAll('.framed-code').forEach(block => {
            window.hljs.highlightElement(block);
          });
          log_msg('UI', 'Applied Highlight.js to framed-code blocks');
        } else {
          log_error(null, new Error('Highlight.js not loaded'), 'highlight init');
        }
      },
      overrideConsole() {
        const originalError = console.error
        const originalWarn = console.warn
        console.error = (...args) => {
          const timeStr = new Date().toTimeString().split(' ')[0] +
                          `.${new Date().getMilliseconds().toString().padStart(3, '0')}`
          this.debugLogs.push({
            type: 'error',
            message: args.join(' '),
            timestamp: timeStr
          })
          originalError.apply(console, args)
        }
        console.warn = (...args) => {
          const timeStr = new Date().toTimeString().split(' ')[0] +
                          `.${new Date().getMilliseconds().toString().padStart(3, '0')}`
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
              timestamp: new Date(log.timestamp * 1000).toTimeString().split(' ')[0] +
                        `.${new Date(log.timestamp * 1000).getMilliseconds().toString().padStart(3, '0')}`
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
              this.chatStore.fetchHistory().then(() => checkAwaitedFiles(this))
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
        const target = event.target.closest('.file-link, .post-link')
        if (target) {
          if (target.classList.contains('file-link')) {
            const fileId = target.getAttribute('data-file-id')
            if (fileId) {
              this.showFilePreview(fileId)
            }
          } else if (target.classList.contains('post-link')) {
            const postId = target.getAttribute('data-post-id')
            if (postId) {
              const postElement = document.getElementById(`post_${postId}`)
              if (postElement) {
                postElement.scrollIntoView({ behavior: 'smooth', block: 'start' })
                log_msg('UI', `Scrolled to post_id: ${postId}`)
              } else {
                log_error(this, new Error(`Post ${postId} not found`), 'scroll to post')
              }
            }
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
      handleSelectDir(dirPath) {
        log_msg('ACTION', 'Called handleSelectDir: %s', dirPath)
        handleSelectDir(this, dirPath);
      },
      handleSelectFile(fileId) {
        log_msg('ACTION', 'Called handleSelectFile: %d', fileId)
        handleSelectFile(this, fileId)
      },
      handleCodeSelection(event) {
        const selection = window.getSelection();
        if (!selection.rangeCount) return;
        const range = selection.getRangeAt(0);
        // const codeElement = document.getElementById('file-preview-code');
        const selectedText = range.toString();
        const text = this.filePreviewContent;
        const lines = text.split('\n');
        const startPos = text.indexOf(selectedText);
        const endPos = startPos + selectedText.length;
        let startLine = lineFromOffset(lines, startPos);
        let endLine = lineFromOffset(lines, endPos);        

        // Update file-preview-info
        const infoElement = document.getElementById('file-preview-info');
        if (startLine === endLine) {
          
          infoElement.textContent = `Line ${startLine}, chars ${startPos}-${endPos}`;
        } else {
          infoElement.textContent = `Lines ${startLine}-${endLine}`;
        }

        log_msg('UI', `Selected code lines: ${startLine}-${endLine}`);
      }    
    }
  })
</script>

<style src="../styles/chat.css"></style>
