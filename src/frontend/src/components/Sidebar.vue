# /frontend/rtm/src/components/SideBar.vue, updated 2025-07-16 15:55 EEST
<template>
  <div class="sidebar" :style="{ width: isCollapsed ? '30px' : '300px' }">
    <div v-if="!isCollapsed" class="sidebar-content">
      <h3>Чаты</h3>
      <div v-if="chats.length">
        <ul>
          <li v-for="chat in chats" :key="chat.chat_id" @click="selectChat(chat.chat_id)" :class="{ 'selected': chat.chat_id === selectedChat }">
            {{ chat.description }}
          </li>
        </ul>
      </div>
      <div v-else>
        <p>Чаты не найдены</p>
      </div>
      <div class="stats">
        <p v-if="selectedChat && stats.tokens">Токенов: {{ stats.tokens }}</p>
        <p v-else>Статистика недоступна</p>
      </div>
    </div>
    <button class="toggle-btn" @click="toggleSidebar">
      {{ isCollapsed ? '◄' : '▶' }}
    </button>
  </div>
</template>

<script>
import { defineComponent, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'

export default defineComponent({
  name: 'SideBar',
  setup() {
    const store = useChatStore()
    const isCollapsed = ref(false)
    const chats = ref([])
    const selectedChat = ref(null)
    const stats = ref({ tokens: 0 })

    const fetchChats = async () => {
      try {
        const response = await fetch('/api/chat/list', {
          credentials: 'include'
        })
        if (!response.ok) throw new Error('Failed to fetch chats')
        chats.value = await response.json()
        console.log('Fetched chats:', chats.value)
      } catch (error) {
        console.error('Error fetching chats:', error)
      }
    }

    const fetchStats = async (chatId) => {
      try {
        const response = await fetch(`/api/chat/get_stats?chat_id=${chatId}`, {
          credentials: 'include'
        })
        if (!response.ok) throw new Error('Failed to fetch stats')
        stats.value = await response.json()
        console.log('Fetched stats:', stats.value)
      } catch (error) {
        console.error('Error fetching stats:', error)
        stats.value = { tokens: 0 }
      }
    }

    const selectChat = (chatId) => {
      selectedChat.value = chatId
      store.setChatId(chatId)
      fetchStats(chatId)
      console.log('Selected chat:', chatId)
    }

    watch(() => store.selectedChatId, (newChatId) => {
      if (newChatId) {
        selectedChat.value = newChatId
        fetchStats(newChatId)
      }
    })

    fetchChats()

    const toggleSidebar = () => {
      isCollapsed.value = !isCollapsed.value
    }

    return { chats, selectedChat, stats, selectChat, toggleSidebar, isCollapsed }
  }
})
</script>

<style>
.sidebar {
  height: 100vh;
  background: #333;
  transition: width 0.3s;
  position: relative;
}
@media (prefers-color-scheme: light) {
  .sidebar {
    background: #f0f0f0;
  }
}
.sidebar-content {
  padding: 10px;
}
.sidebar h3 {
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .sidebar h3 {
    color: #333;
  }
}
ul {
  list-style: none;
  padding: 0;
}
li {
  padding: 5px;
  cursor: pointer;
  color: #eee;
}
@media (prefers-color-scheme: light) {
  li {
    color: #333;
  }
}
li:hover {
  background: #444;
}
@media (prefers-color-scheme: light) {
  li:hover {
    background: #e0e0e0;
  }
}
.selected {
  background: #555;
}
@media (prefers-color-scheme: light) {
  .selected {
    background: #d0d0d0;
  }
}
.toggle-btn {
  position: absolute;
  top: 10px;
  right: 10px;
  background: #444;
  color: #eee;
  border: none;
  cursor: pointer;
}
@media (prefers-color-scheme: light) {
  .toggle-btn {
    background: #d0d0d0;
    color: #333;
  }
}
.stats {
  position: absolute;
  bottom: 10px;
  left: 10px;
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .stats {
    color: #333;
  }
}
</style>