<!-- /frontend/rtm/src/components/LeftPanel.vue, updated 2025-07-22 17:30 EEST -->
<template>
  <div class="left-panel" :class="{ collapsed: isCollapsed }">
    <button class="toggle-btn" @click="toggleCollapse">
      {{ isCollapsed ? '▶' : '◄' }}
    </button>
    <div v-if="!isCollapsed" class="panel-content">
      <h3>Чаты</h3>
      <ChatTree :chats="chatStore.chats" :level="0" :selectedChatId="chatStore.selectedChatId" @select-chat="selectChat" />
      <div v-if="chatStore.selectedChatId" class="chat-stats">
        <p>Tokens: {{ chatStore.stats.tokens || 'N/A' }}</p>
        <p>Sources: {{ chatStore.stats.num_sources_used || 'N/A' }}</p>
      </div>
      <button @click="openCreateChatModal">Создать чат</button>
      <button v-if="chatStore.selectedChatId" @click="deleteChat">Удалить чат</button>
      <dialog ref="createChatModal" id="createChatModal">
        <h3>Создать чат</h3>
        <input v-model="chatStore.newChatDescription" placeholder="Описание чата" />
        <button @click="createChat">Создать</button>
        <button @click="chatStore.closeCreateChatModal">Отмена</button>
      </dialog>
    </div>
  </div>
</template>

<script>
import { defineComponent, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { log_msg } from '../utils/debugging'
import ChatTree from './ChatTree.vue'

export default defineComponent({
  name: 'LeftPanel',
  components: { ChatTree },
  setup() {
    const chatStore = useChatStore()
    const isCollapsed = ref(false)
    return { chatStore, isCollapsed }
  },
  mounted() {
    log_msg('UI', 'LeftPanel mounted, chats:', JSON.stringify(this.chatStore.chats, null, 2))
    this.chatStore.fetchChats()
    this.chatStore.startPolling()
  },
  beforeUnmount() {
    this.chatStore.stopPolling()
    log_msg('UI', 'LeftPanel unmounted, stopped polling')
  },
  watch: {
    'chatStore.chats': {
      handler(newChats) {
        log_msg('CHAT', 'Chats updated in LeftPanel:', JSON.stringify(newChats, null, 2))
      },
      immediate: true,
      deep: true
    },
    'chatStore.selectedChatId': {
      handler(newChatId) {
        log_msg('CHAT', 'LeftPanel selectedChatId updated:', newChatId)
        if (newChatId) {
          this.chatStore.fetchChatStats()
        }
      },
      immediate: true
    }
  },
  methods: {
    selectChat(chatId) {
      log_msg('ACTION', 'Selecting chat:', chatId)
      this.chatStore.setChatId(chatId)
    },
    async createChat() {
      if (this.chatStore.newChatDescription.trim()) {
        log_msg('ACTION', 'Creating chat with description:', this.chatStore.newChatDescription)
        await this.chatStore.createChat(this.chatStore.newChatDescription)
      }
    },
    async deleteChat() {
      log_msg('ACTION', 'Deleting chat:', this.chatStore.selectedChatId)
      await this.chatStore.deleteChat()
    },
    openCreateChatModal() {
      log_msg('ACTION', 'Opening create chat modal')
      this.chatStore.openCreateChatModal(null)
    },
    toggleCollapse() {
      this.isCollapsed = !this.isCollapsed
      log_msg('UI', 'Left panel collapsed:', this.isCollapsed)
    }
  }
})
</script>

<style>
.left-panel {
  width: 300px;
  padding: 10px;
  background: #333;
  transition: width 0.3s;
  color: #eee;
}
.left-panel.collapsed {
  width: 30px;
}
@media (prefers-color-scheme: light) {
  .left-panel {
    background: #f0f0f0;
    color: #333;
  }
}
.left-panel .toggle-btn {
  position: absolute;
  top: 10px;
  left: 10px;
  background: #444;
  color: #eee;
  border: none;
  cursor: pointer;
  padding: 5px;
}
@media (prefers-color-scheme: light) {
  .left-panel .toggle-btn {
    background: #d0d0d0;
    color: #333;
  }
}
.left-panel .panel-content {
  display: flex;
  flex-direction: column;
}
.left-panel.collapsed .panel-content {
  display: none;
}
.left-panel h3 {
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .left-panel h3 {
    color: #333;
  }
}
.left-panel button:not(.toggle-btn) {
  margin: 10px 0;
  padding: 5px;
}
.chat-stats {
  margin-top: 10px;
  color: #aaa;
}
@media (prefers-color-scheme: light) {
  .chat-stats {
    color: #666;
  }
}
.left-panel dialog {
  padding: 20px;
  border: 1px solid #ccc;
  border-radius: 5px;
}
.left-panel dialog input {
  width: 100%;
  margin-bottom: 10px;
}
</style>