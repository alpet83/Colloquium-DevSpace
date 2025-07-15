# /opt/docker/mcp-server/frontend/rtm/src/components/Login.vue, updated 2025-07-14 14:25 EEST
<template>
  <div class="login">
    <h1>Многопользовательский чат</h1>
    <input v-model="username" placeholder="Имя пользователя" @input="logInput('username', $event)" />
    <input v-model="password" type="password" placeholder="Пароль" @input="logInput('password', $event)" />
    <p v-if="store.loginError" class="error">{{ store.loginError }}</p>
    <button @click="store.login(username, password)">Войти</button>
  </div>
</template>

<script>
import { defineComponent } from 'vue'
import { useChatStore } from '../store'

export default defineComponent({
  name: 'Login',
  data() {
    return {
      username: '',
      password: ''
    }
  },
  setup() {
    const store = useChatStore()
    return { store }
  },
  methods: {
    logInput(field, event) {
      console.log(`Input ${field}:`, event.target.value)
    }
  }
})
</script>

<style>
.login {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 20px;
}
.error {
  color: red;
  margin: 10px;
}
</style>
