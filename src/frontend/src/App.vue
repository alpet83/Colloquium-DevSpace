# /frontend/rtm/src/App.vue, updated 2025-07-17 10:52 EEST
<template>
  <div class="app-container">
    <div v-if="authStore.backendError" class="error-overlay">
      <p>Бэкэнд неисправен. Произошла ошибка сервера. Пожалуйста, попробуйте позже.</p>
    </div>
    <div v-if="!authStore.isLoggedIn" class="login-form">
      <Login />
    </div>
    <div v-else class="main-content">
      <LeftPanel :chats="chatStore.chats" :selectedChatId="chatStore.selectedChatId" @select-chat="chatStore.selectChat" @delete-chat="chatStore.deleteChat" />
      <ChatContainer class="chat-container" />
      <RightPanel class="right-panel" />
    </div>
  </div>
</template>

<script>
import { defineComponent } from 'vue'
import { useAuthStore } from './stores/auth'
import { useChatStore } from './stores/chat'
import Login from './components/Login.vue'
import LeftPanel from './components/LeftPanel.vue'
import ChatContainer from './components/ChatContainer.vue'
import RightPanel from './components/RightPanel.vue'

export default defineComponent({
  name: 'App',
  components: {
    Login,
    LeftPanel,
    ChatContainer,
    RightPanel
  },
  setup() {
    const authStore = useAuthStore()
    const chatStore = useChatStore()
    return { authStore, chatStore }
  },
  mounted() {
    this.authStore.checkSession().then(() => {
      console.log('App mounted, isLoggedIn:', this.authStore.isLoggedIn, 'chats:', JSON.stringify(this.chatStore.chats, null, 2))
    })
  }
})
</script>

<style>
body {
  margin: 0;
  background: #222;
}
@media (prefers-color-scheme: light) {
  body {
    background: #f0f0f0;
  }
}
.app-container {
  display: flex;
  flex-direction: column;
  height: 95vh;
  background: #333;
}
@media (prefers-color-scheme: light) {
  .app-container {
    background: #f0f0f0;
  }
}
.login-form {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
}
.main-content {
  display: flex;
  flex: 1;
  min-width: 0;
}
.chat-container {
  flex: 1;
  min-width: 800px;
}
.right-panel {
  flex: 0 0 300px;
  min-width: 30px;
  max-width: 300px;
  overflow-y: auto;
  background: #333;
}
@media (prefers-color-scheme: light) {
  .right-panel {
    background: #f0f0f0;
  }
}
.error-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.8);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.error {
  color: red;
}
</style>