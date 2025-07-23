<!-- /frontend/rtm/src/components/ChatTree.vue, updated 2025-07-22 17:30 EEST -->
<template>
  <ul>
    <li v-for="chat in chats" :key="chat.chat_id" :style="{ 'margin-left': `${level * 20}px` }" :class="{ 'selected': chat.chat_id === selectedChatId }">
      <span @click="selectChat(chat.chat_id)" class="chat-item">
        {{ chat.description }}
      </span>
      <ChatTree v-if="chat.children && chat.children.length" :chats="chat.children" :selectedChatId="selectedChatId" @select-chat="selectChat" :level="level + 1" />
    </li>
  </ul>
</template>

<script>
import { defineComponent } from 'vue'
import { useChatStore } from '@/stores/chat'
import { log_msg } from '@/utils/debugging'

export default defineComponent({
  name: 'ChatTree',
  props: {
    chats: {
      type: Array,
      required: true
    },
    selectedChatId: {
      type: [Number, String],
      default: null
    },
    level: {
      type: Number,
      default: 0
    }
  },
  setup() {
    const chatStore = useChatStore()
    return { chatStore }
  },
  mounted() {
    log_msg('UI', 'ChatTree mounted, chats:', JSON.stringify(this.chats, null, 2), 'level:', this.level, 'selectedChatId:', this.selectedChatId)
  },
  watch: {
    selectedChatId(newId) {
      log_msg('CHAT', 'ChatTree selectedChatId updated:', newId)
    }
  },
  methods: {
    selectChat(chatId) {
      log_msg('ACTION', 'ChatTree selectChat:', chatId)
      this.chatStore.setChatId(chatId)
      this.$emit('select-chat', chatId)
    }
  }
})
</script>

<style>
.chat-item {
  cursor: pointer;
  padding: 5px;
  display: block;
  color: #eee;
}
.chat-item:hover {
  background: #444;
}
@media (prefers-color-scheme: light) {
  .chat-item {
    color: #333;
  }
  .chat-item:hover {
    background: #e0e0e0;
  }
}
.selected > .chat-item {
  background: #555;
  border: 2px solid #007bff !important;
  border-radius: 3px;
}
@media (prefers-color-scheme: light) {
  .selected > .chat-item {
    background: #d0d0d0;
    border: 2px solid #0056b3 !important;
  }
}
</style>
