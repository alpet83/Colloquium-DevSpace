<template>
  <div class="chat-container">
    <dialog ref="createChatModal">
      <h3>Создать новый чат</h3>
      <input v-model="newChatDescription" placeholder="Описание чата" />
      <button @click="store.createChat(newChatDescription)">Создать</button>
      <button @click="store.closeCreateChatModal">Отмена</button>
    </dialog>
    <dialog ref="fileConfirmModal">
      <h3>Подтверждение имени файла</h3>
      <input v-model="pendingFileName" placeholder="Полное имя файла (например, /src/test.rs)" list="fileSuggestions" />
      <datalist id="fileSuggestions">
        <option v-for="file in store.sandwichFiles" :value="file" :key="file" />
      </datalist>
      <button @click="confirmFileUpload">Загрузить</button>
      <button @click="closeFileConfirmModal">Отмена</button>
    </dialog>
    <p v-if="store.chatError" class="error">{{ store.chatError }}</p>
    <div v-for="msg in store.history" :key="msg.id" class="message">
      <p v-html="formatMessage(msg.message, msg.file_names, msg.user_name)"></p>
      <button class="delete-post" @click="store.deletePost(msg.id)">X</button>
    </div>
    <input v-model="newMessage" @keyup.enter="sendMessage" placeholder="Сообщение" />
    <div v-if="store.pendingAttachment" class="attachment-preview">
      <p>Прикреплён: {{ store.pendingAttachment.file_name }}</p>
      <button @click="store.clearAttachment">Очистить</button>
    </div>
    <input type="file" @change="openFileConfirmModal" />
    <button @click="store.openCreateChatModal(store.history[store.history.length - 1]?.id)">Ответвление</button>
  </div>
</template>

<script>
import { defineComponent } from 'vue'
import { useChatStore } from '../store'

export default defineComponent({
  name: 'ChatContainer',
  data() {
    return {
      newMessage: '',
      newChatDescription: '',
      pendingFile: null,
      pendingFileName: ''
    }
  },
  setup() {
    const store = useChatStore()
    return { store }
  },
  methods: {
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
      await this.store.uploadFile(this.pendingFile, this.pendingFileName, this.store.selectedChatId)
      this.closeFileConfirmModal()
    },
    async sendMessage() {
      if (!this.newMessage && !this.store.pendingAttachment) return
      await this.store.sendMessage(this.newMessage)
      this.newMessage = ''
    },
    formatMessage(message, fileNames, userName) {
      let formatted = message.replace(/</g, '<').replace(/>/g, '>')
      if (fileNames && fileNames.length) {
        fileNames.forEach(file => {
          const date = new Date(file.ts * 1000).toLocaleString()
          formatted = formatted.replace(`@attach#${file.file_id}`, `File: ${file.file_name} (${date})`)
        })
      }
      return `${userName}: ${formatted}`
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
}
.message {
  margin: 10px 0;
  display: flex;
  align-items: center;
}
.message p {
  margin: 0;
  flex-grow: 1;
}
.delete-post {
  margin-left: 10px;
  padding: 2px 8px;
  font-size: 12px;
  background: #ff4444;
  color: #fff;
  border: none;
  border-radius: 3px;
  cursor: pointer;
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
