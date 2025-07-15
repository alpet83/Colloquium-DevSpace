// /frontend/rtm/src/main.js, updated 2025-07-14 14:48 EEST

import './assets/main.css'
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)           
app.mount('#app')         

