<template>
  <ul>
    <li v-for="chat in chats" :key="chat.chat_id" :class="{ active: selectedChatId === chat.chat_id }" :style="{ 'margin-left': level * 20 + 'px' }">
      <span @click="$emit('select-chat', chat.chat_id)">{{ chat.description }}</span>
      <ChatTree v-if="chat.children" :chats="chat.children" :selectedChatId="selectedChatId" :level="level + 1" @select-chat="$emit('select-chat', $event)" />
    </li>
  </ul>
</template>

<script>
export default {
  name: 'ChatTree',
  props: {
    chats: Array,
    selectedChatId: Number,
    level: { type: Number, default: 0 }
  },
  emits: ['select-chat']
}
</script>
