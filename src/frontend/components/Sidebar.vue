# /frontend/rtm/src/components/Sidebar.vue, updated 2025-07-14 14:37 EEST
<template>
  <div class="sidebar">
    <h2>Чаты</h2>
    <ChatTree :chats="store.chats" :selectedChatId="store.selectedChatId" @select-chat="store.selectChat" />
    <button @click="store.openCreateChatModal(null)">Создать чат</button>
    <button v-if="store.selectedChatId !== null" @click="store.deleteChat">Удалить чат</button>
    <button @click="store.currentTab = 'files'">Файлы</button>
    <button @click="store.logout">Выйти</button>
    <p class="version">Обновлено: 2025-07-14 14:37 EEST (v0.0.0)</p>
  </div>
</template>

<script>
import { defineComponent } from 'vue'
import ChatTree from './ChatTree.vue'
import { useChatStore } from '../store'

export default defineComponent({
  name: 'Sidebar',
  components: { ChatTree },
  setup() {
    const store = useChatStore()
    return { store }
  }
})
</script>

<style>
.sidebar {
  width: 250px;
  padding: 20px;
  background: #f0f0f0;
  border-right: 1px solid #ccc;
  color: #333;
}
@media (prefers-color-scheme: dark) {
  .sidebar {
    background: #333;
    border-right: 1px solid #555;
    color: #fff;
  }
  .sidebar li.active {
    background: #444;
  }
}
.sidebar h2 {
  margin-top: 0;
}
.sidebar ul {
  list-style: none;
  padding: 0;
}
.sidebar li {
  padding: 10px;
  cursor: pointer;
}
.sidebar li.active {
  background: #ddd;
}
.sidebar button {
  display: block;
  width: 100%;
  margin: 10px 0;
}
.sidebar .version {
  font-size: 12px;
  color: #666;
  margin-top: 20px;
}
@media (prefers-color-scheme: dark) {
  .sidebar .version {
    color: #aaa;
  }
}
</style>
