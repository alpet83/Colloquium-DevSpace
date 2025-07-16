# /frontend/rtm/src/components/ChatTree.vue, updated 2025-07-16 09:45 EEST
<template>
  <ul>
    <li v-for="chat in chats" :key="chat.chat_id" :class="{ active: selectedChatId === chat.chat_id }" :style="{ 'margin-left': level * 20 + 'px' }">
      <div class="chat-item">
        <span class="toggle" v-if="chat.children && chat.children.length" @click="toggleExpand(chat.chat_id)">
          {{ expanded[chat.chat_id] ? '▼' : '▶' }}
        </span>
        <span class="chat-title" @click="$emit('select-chat', chat.chat_id)">{{ chat.description }}</span>
      </div>
      <ChatTree v-if="chat.children && chat.children.length && expanded[chat.chat_id]"
                :chats="chat.children"
                :selectedChatId="selectedChatId"
                :level="level + 1"
                @select-chat="$emit('select-chat', $event)" />
    </li>
  </ul>
</template>

<script>
import { defineComponent, reactive } from 'vue'

export default defineComponent({
  name: 'ChatTree',
  props: {
    chats: Array,
    selectedChatId: Number,
    level: { type: Number, default: 0 }
  },
  setup() {
    const expanded = reactive({})
    return { expanded }
  },
  emits: ['select-chat'],
  methods: {
    toggleExpand(chatId) {
      this.expanded[chatId] = !this.expanded[chatId]
    }
  }
})
</script>

<style>
ul {
  list-style: none;
  padding: 0;
}
li {
  padding: 8px;
  cursor: pointer;
  background: #3a3a3a;
}
li.active {
  background: #444;
  color: #eee;
}
.chat-item {
  display: flex;
  align-items: center;
}
.toggle {
  width: 20px;
  text-align: center;
  cursor: pointer;
  margin-right: 5px;
}
.chat-title {
  flex-grow: 1;
}
@media (prefers-color-scheme: light) {
  li {
    background: #ddd;
  }
  li.active {
    background: #ccc;
    color: #333;
  }
}
</style>