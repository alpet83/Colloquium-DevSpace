// /frontend/rtm/src/main.js, updated 2025-07-16 15:34 EEST
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import mitt from 'mitt'
import App from './App.vue'

const app = createApp(App)
const pinia = createPinia()
const emitter = mitt()
app.use(pinia)
app.provide('mitt', emitter)
app.mount('#app')
console.log('App initialized with Pinia:', pinia)     