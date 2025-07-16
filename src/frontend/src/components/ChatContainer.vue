# /frontend/rtm/src/components/ChatContainer.vue, updated 2025-07-16 15:55 EEST
<template>
  <div class="chat-container">
    <dialog id="createChatModal" ref="createChatModal">
      <h3>Создать новый чат</h3>
      <input v-model="newChatDescription" placeholder="Описание чата" />
      <button @click="chatStore.createChat(newChatDescription)">Создать</button>
      <button @click="chatStore.closeCreateChatModal">Отмена</button>
    </dialog>
    <dialog ref="fileConfirmModal">
      <h3>Подтверждение имени файла</h3>
      <input v-model="pendingFileName" placeholder="Полное имя файла (например, /src/test.rs)" list="fileSuggestions" />
      <datalist id="fileSuggestions">
        <option v-for="file in fileStore.files" :value="file.file_name" :key="file.id" />
      </datalist>
      <button @click="confirmFileUpload">Загрузить</button>
      <button @click="closeFileConfirmModal">Отмена</button>
    </dialog>
    <dialog ref="editPostModal">
      <h3>Редактировать сообщение</h3>
      <input v-model="editMessageContent" placeholder="Новое сообщение" />
      <button @click="editPost">Сохранить</button>
      <button @click="closeEditPostModal">Отмена</button>
    </dialog>
    <p v-if="chatStore.chatError || fileStore.chatError" class="error">{{ chatStore.chatError || fileStore.chatError }}</p>
    <div class="messages" ref="messagesContainer">
      <div v-for="(msg, index) in chatStore.history" :key="msg.id" :class="['message', { 'admin-message': msg.user_id === 1 }]">
        <p v-html="formatMessage(msg.message, msg.file_names, msg.user_name, msg.timestamp)"></p>
        <button class="edit-post" @click="openEditPostModal(msg.id, msg.message)">✎</button>
        <button class="delete-post" @click="chatStore.deletePost(msg.id, msg.user_id, authStore.userId, authStore.userRole)">X</button>
        <hr v-if="index < chatStore.history.length - 1" class="message-separator" />
      </div>
    </div>
    <input v-model="newMessage" ref="messageInput" @keyup.enter="sendMessage" placeholder="Сообщение" />
    <div v-if="fileStore.pendingAttachment" class="attachment-preview">
      <p>Прикреплён: {{ fileStore.pendingAttachment.file_name }} (@attached_file#{{ fileStore.pendingAttachment.file_id }})</p>
      <button @click="fileStore.clearAttachment">Очистить</button>
    </div>
    <input type="file" @change="openFileConfirmModal" />
    <button @click="openCreateChatModal">Ответвление</button>
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
      pollInterval: null
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
  },
  beforeUnmount() {
    this.stopPolling()
    this.mitt.off('select-file', this.handleSelectFile)
  },
  updated() {
    this.scrollToBottom()
  },
  methods: {
    startPolling() {
      if (this.chatStore.selectedChatId !== null) {
        this.pollInterval = setInterval(() => {
          this.chatStore.fetchHistory()
        }, 5000)
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
      await this.fileStore.uploadFile(this.pendingFile, this.pendingFileName, this.chatStore.selectedChatId)
      this.closeFileConfirmModal()
    },
    async sendMessage() {
      if (!this.newMessage && !this.fileStore.pendingAttachment) return
      let finalMessage = this.newMessage
      if (this.fileStore.pendingAttachment) {
        finalMessage = `${this.newMessage} @attach#${this.fileStore.pendingAttachment.file_id}`.trim()
      }
      await this.chatStore.sendMessage(finalMessage)
      this.newMessage = ''
      this.startPolling()
    },
    openEditPostModal(postId, message) {
      this.editMessageId = postId
      this.editMessageContent = message
      this.$refs.editPostModal.showModal()
    },
    closeEditPostModal() {
      this.editMessageId = null
      this.editMessageContent = ''
      this.$refs.editPostModal.close()
    },
    async editPost() {
      if (!this.editMessageId || !this.editMessageContent) return
      await this.chatStore.editPost(this.editMessageId, this.editMessageContent)
      this.closeEditPostModal()
    },
    openCreateChatModal() {
      const parentMessageId = this.chatStore.history[this.chatStore.history.length - 1]?.id
      console.log('Opening create chat modal, parentMessageId:', parentMessageId)
      this.chatStore.openCreateChatModal(parentMessageId)
    },
    handleSelectFile(fileId) {
      this.newMessage += ` @attach#${fileId}`
      this.$refs.messageInput?.focus()
    },
    formatMessage(message, fileNames, userName, timestamp) {
      let formatted = message.replace(/</g, '<').replace(/>/g, '>')
      if (fileNames && fileNames.length) {
        fileNames.forEach(file => {
          const date = new Date(file.ts * 1000).toLocaleString()
          formatted = formatted.replace(`@attach#${file.file_id}`, `File: ${file.file_name} (@attached_file#${file.file_id}, ${date})`)
        })
      }
      const dateTime = new Date(timestamp * 1000).toLocaleString('ru-RU', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      })
      return `${dateTime} ${userName}: ${formatted}`
    }
  },
  watch: {
    'chatStore.selectedChatId': function(newChatId) {
      this.stopPolling()
      if (newChatId !== null) {
        this.startPolling()
      }
    },
    'chatStore.history': function() {
      this.scrollToBottom()
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
.messages {
  max-height: 85vh;
  overflow-y: auto;
  margin: 0 10px 10px 10px;
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
@media (prefers-color-scheme: light) {
  .message {
    background: #f0f0f0;
  }
  .admin-message {
    background: #e0e0e0;
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
input {
  margin: 10px;
  padding: 5px;
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
dialog input {
  width: 100%;
  margin-bottom: 10px;
}
.error {
  color: red;
  margin: 10px;
}
</style>