# /frontend/rtm/src/components/Login.vue, updated 2025-07-16 15:55 EEST
<template>
  <div class="login">
    <h1>Многопользовательский чат</h1>
    <input v-model="username" placeholder="Имя пользователя" @input="logInput('username', $event)" />
    <input v-model="password" type="password" placeholder="Пароль" @input="logInput('password', $event)" />
    <p v-if="authStore.loginError" class="error">{{ authStore.loginError }}</p>
    <button @click="authStore.login(username, password)">Войти</button>
  </div>
</template>

<script>
import { defineComponent } from 'vue'
import { useAuthStore } from '../stores/auth'

export default defineComponent({
  name: 'Login',
  data() {
    return {
      username: '',
      password: ''
    }
  },
  setup() {
    const authStore = useAuthStore()
    return { authStore }
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