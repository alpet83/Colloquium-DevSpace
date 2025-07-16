// /frontend/rtm/src/stores/auth.js, created 2025-07-16 15:55 EEST
import { defineStore } from 'pinia'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    isLoggedIn: false,
    username: '',
    password: '',
    loginError: '',
    backendError: false,
    isCheckingSession: false,
    userRole: null,
    userId: null,
    apiUrl: import.meta.env.VITE_API_URL || 'http://vps.vpn:8008/api'
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
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        console.log('Received chats:', data)
        if (res.ok && !data.error) {
          this.isLoggedIn = true
          this.backendError = false
          const userRes = await fetch(this.apiUrl + '/user/info', {
            method: 'GET',
            credentials: 'include'
          })
          const userData = await userRes.json()
          if (userRes.ok && !userData.error) {
            this.userRole = userData.role || 'developer'
            this.userId = userData.user_id
          } else {
            console.error('Error fetching user info:', userData)
            this.userRole = null
            this.userId = null
          }
        } else {
          console.error('Session check error:', data)
          this.isLoggedIn = false
          this.loginError = data.error || 'Session error'
        }
      } catch (e) {
        console.error('Session check failed:', e)
        this.backendError = true
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
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          console.log('Login successful, Cookies:', document.cookie)
          this.isLoggedIn = true
          this.backendError = false
          this.username = ''
          this.password = ''
          const userRes = await fetch(this.apiUrl + '/user/info', {
            method: 'GET',
            credentials: 'include'
          })
          const userData = await userRes.json()
          if (userRes.ok && !userData.error) {
            this.userRole = userData.role || 'developer'
            this.userId = userData.user_id
          } else {
            console.error('Error fetching user info:', userData)
            this.userRole = null
            this.userId = null
          }
        } else {
          console.error('Login error:', data)
          this.isLoggedIn = false
          this.loginError = data.error || 'Invalid username or password'
        }
      } catch (e) {
        console.error('Login failed:', e)
        this.backendError = true
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
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        this.isLoggedIn = false
        this.loginError = ''
        this.userRole = null
        this.userId = null
      } catch (e) {
        console.error('Logout failed:', e)
        this.backendError = true
      }
    }
  }
})