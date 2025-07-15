# /opt/docker/mcp-server/frontend/rtm/src/App.vue, updated 2025-07-14 14:25 EEST
<template>
  <div class="container">
    <Login v-if="!store.isLoggedIn" />
    <div v-else-if="store.backendError" class="backend-error">
      <h2>Бэкэнд неисправен</h2>
      <p>Произошла ошибка сервера. Пожалуйста, попробуйте позже.</p>
    </div>
    <div v-else class="app">
      <Sidebar />
      <ChatContainer v-if="store.currentTab === 'chat' && store.selectedChatId !== null" />
      <FileManager v-else-if="store.currentTab === 'files'" :files="store.files" @delete-file="store.deleteFile" @update-file="store.updateFile" />
      <div v-else class="no-chat">
        <p>Выберите чат или вкладку Файлы</p>
      </div>
    </div>
  </div>
</template>

<script>
import { defineComponent } from 'vue'
import Login from './components/Login.vue'
import Sidebar from './components/Sidebar.vue'
import ChatContainer from './components/ChatContainer.vue'
import FileManager from './components/FileManager.vue'
import { useChatStore } from './store'

export default defineComponent({
  name: 'App',
  components: { Login, Sidebar, ChatContainer, FileManager },
  setup() {
    const store = useChatStore()
    console.log('App mounted, apiUrl:', store.apiUrl, 'Version: 2025-07-14 14:25 EEST')
    store.checkSession()
    store.fetchSandwichFiles()
    return { store }
  }
})
</script>

<style>
.container {
  display: flex;
  height: 100vh;
}
.backend-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100vh;
  color: #ff4444;
}
.app {
  display: flex;
  width: 100%;
}
.no-chat {
  text-align: center;
  color: #666;
}
@media (prefers-color-scheme: dark) {
  .no-chat {
    color: #aaa;
  }
}
</style>
