// /frontend/rtm/src/stores/auth.js, created 2025-07-16 15:55 EEST
import { defineStore } from 'pinia'
import { readFetchJsonOrText, formatHttpFailureMessage, isServerOrGatewayFailure } from '../utils/apiErrors'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    isLoggedIn: false,
    username: '',
    password: '',
    loginError: '',
    backendError: false,
    /** Текст для оверлея / ошибки (nginx-router JSON, FastAPI detail и т.д.) */
    backendErrorDetail: '',
    isCheckingSession: false,
    userRole: null,
    userId: null,
    apiUrl: import.meta.env.VITE_API_URL || './api'
  }),
  actions: {
    async checkSession() {
      if (this.isCheckingSession) return
      this.isCheckingSession = true
      try {
        console.log('Checking session:', this.apiUrl + '/chat/list', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/list', {
          method: 'GET',
          credentials: 'include'
        })
        const body = await readFetchJsonOrText(res)
        if (isServerOrGatewayFailure(res)) {
          console.error('Server / gateway error:', res.status, body.raw?.slice(0, 200))
          this.backendError = true
          this.backendErrorDetail = formatHttpFailureMessage(res, body)
          return
        }
        const data = body.json
        console.log('Received chats:', data)
        if (res.ok && data && !data.error) {
          this.isLoggedIn = true
          this.backendError = false
          this.backendErrorDetail = ''
          const userRes = await fetch(this.apiUrl + '/user/info', {
            method: 'GET',
            credentials: 'include'
          })
          const ubody = await readFetchJsonOrText(userRes)
          if (isServerOrGatewayFailure(userRes)) {
            this.backendError = true
            this.backendErrorDetail = formatHttpFailureMessage(userRes, ubody)
          } else if (userRes.ok && ubody.json && !ubody.json.error) {
            const userData = ubody.json
            this.userRole = userData.role || 'developer'
            this.userId = userData.user_id
          } else {
            console.error('Error fetching user info:', ubody.json)
            this.userRole = null
            this.userId = null
          }
        } else {
          console.error('Session check error:', data)
          this.isLoggedIn = false
          this.loginError = (data && (data.error || data.detail)) || 'Session error'
        }
      } catch (e) {
        console.error('Session check failed:', e)
        this.backendError = true
        this.backendErrorDetail = e?.message || 'Сеть или неизвестная ошибка при проверке сессии'
      } finally {
        this.isCheckingSession = false
      }
    },
    async login(username, password) {
      console.log('Login attempt:', { username }, 'URL:', this.apiUrl + '/login', 'Cookies:', document.cookie)
      try {
        this.loginError = ''
        const res = await fetch(this.apiUrl + '/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
          credentials: 'include'
        })
        const body = await readFetchJsonOrText(res)
        if (isServerOrGatewayFailure(res)) {
          console.error('Server / gateway error:', res.status)
          this.backendError = true
          this.backendErrorDetail = formatHttpFailureMessage(res, body)
          return
        }
        const data = body.json
        if (res.ok && data && !data.error) {
          console.log('Login successful, Cookies:', document.cookie)
          this.isLoggedIn = true
          this.backendError = false
          this.backendErrorDetail = ''
          this.username = ''
          this.password = ''
          const userRes = await fetch(this.apiUrl + '/user/info', {
            method: 'GET',
            credentials: 'include'
          })
          const ubody = await readFetchJsonOrText(userRes)
          if (isServerOrGatewayFailure(userRes)) {
            this.backendError = true
            this.backendErrorDetail = formatHttpFailureMessage(userRes, ubody)
          } else if (userRes.ok && ubody.json && !ubody.json.error) {
            const userData = ubody.json
            this.userRole = userData.role || 'developer'
            this.userId = userData.user_id
          } else {
            console.error('Error fetching user info:', ubody.json)
            this.userRole = null
            this.userId = null
          }
        } else {
          console.error('Login error:', data)
          this.isLoggedIn = false
          this.loginError = (data && (data.error || data.detail)) || 'Invalid username or password'
        }
      } catch (e) {
        console.error('Login failed:', e)
        this.backendError = true
        this.backendErrorDetail = e?.message || 'Сеть или неизвестная ошибка при входе'
      }
    },
    async logout() {
      try {
        console.log('Logging out:', this.apiUrl + '/logout', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/logout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include'
        })
        const body = await readFetchJsonOrText(res)
        if (isServerOrGatewayFailure(res)) {
          console.error('Server / gateway error:', res.status)
          this.backendError = true
          this.backendErrorDetail = formatHttpFailureMessage(res, body)
          return
        }
        this.isLoggedIn = false
        this.loginError = ''
        this.userRole = null
        this.userId = null
        this.backendErrorDetail = ''
      } catch (e) {
        console.error('Logout failed:', e)
        this.backendError = true
        this.backendErrorDetail = e?.message || 'Сеть или неизвестная ошибка при выходе'
      }
    }
  }
})