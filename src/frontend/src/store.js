// /frontend/rtm/src/store.js, updated 2025-07-14 17:44 EEST
import { defineStore } from 'pinia'

export const useChatStore = defineStore('chat', {
  state: () => ({
    isLoggedIn: false,
    username: '',
    password: '',
    loginError: '',
    chatError: '',
    backendError: false,
    chats: [],
    selectedChatId: null,
    history: [],
    files: [],
    sandwichFiles: [],
    newChatDescription: '',
    newChatParentMessageId: null,
    currentTab: 'chat',
    pendingAttachment: null,
    apiUrl: import.meta.env.VITE_API_URL || '/api',
    isCheckingSession: false,
    version: '0.0.0'
  }),
  actions: {
    async checkSession() {
      if (this.isCheckingSession) return
      this.isCheckingSession = true
      try {
        console.log('Проверка сессии:', this.apiUrl + '/chat/list', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/list', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        console.log('Получены чаты:', data)
        if (res.ok && !data.error) {
          this.isLoggedIn = true
          this.backendError = false
          this.chatError = ''
          this.chats = await this.buildChatTree(data)
          console.log('Построено дерево чатов:', this.chats)
          await this.fetchFiles()
        } else {
          console.error('Ошибка проверки сессии:', data)
          this.isLoggedIn = false
          this.chats = []
          this.history = []
          this.files = []
          this.sandwichFiles = []
          this.pendingAttachment = null
          this.loginError = data.error || 'Session error'
        }
      } catch (e) {
        console.error('Ошибка проверки сессии:', e)
        this.backendError = true
      } finally {
        this.isCheckingSession = false
      }
    },
    async login(username, password) {
      console.log('Клик на Войти')
      const payload = { username, password }
      console.log('Отправка логина:', payload, 'URL:', this.apiUrl + '/login', 'Cookies:', document.cookie)
      try {
        this.loginError = ''
        this.chatError = ''
        const res = await fetch(this.apiUrl + '/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          console.log('Логин успешен, Cookies:', document.cookie)
          this.isLoggedIn = true
          this.backendError = false
          this.chatError = ''
          this.username = ''
          this.password = ''
          const chatsData = await (await fetch(this.apiUrl + '/chat/list', { credentials: 'include' })).json()
          console.log('Получены чаты после логина:', chatsData)
          this.chats = await this.buildChatTree(chatsData)
          console.log('Построено дерево чатов после логина:', this.chats)
          await this.fetchFiles()
          this.selectedChatId = null
        } else {
          console.error('Ошибка логина:', data)
          this.isLoggedIn = false
          this.loginError = data.error || 'Invalid username or password'
        }
      } catch (e) {
        console.error('Ошибка отправки логина:', e)
        this.backendError = true
      }
    },
    async logout() {
      try {
        console.log('Отправка выхода:', this.apiUrl + '/logout', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/logout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        this.isLoggedIn = false
        this.chats = []
        this.history = []
        this.files = []
        this.sandwichFiles = []
        this.pendingAttachment = null
        this.selectedChatId = null
        this.currentTab = 'chat'
        this.loginError = ''
        this.chatError = ''
      } catch (e) {
        console.error('Ошибка выхода:', e)
        this.backendError = true
      }
    },
    async fetchHistory() {
      if (this.selectedChatId === null) return
      try {
        console.log('Получение истории:', this.apiUrl + `/chat/get?chat_id=${this.selectedChatId}`, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + `/chat/get?chat_id=${this.selectedChatId}`, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.history = data
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка получения истории:', data)
          this.chatError = data.error || 'Failed to fetch chat history'
        }
      } catch (e) {
        console.error('Ошибка получения истории:', e)
        this.chatError = 'Failed to fetch chat history'
      }
    },
    async fetchFiles() {
      try {
        console.log('Получение файлов:', this.apiUrl + '/chat/list_files', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/list_files', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.files = data
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка получения файлов:', data)
          this.chatError = data.error || 'Failed to fetch files'
        }
      } catch (e) {
        console.error('Ошибка получения файлов:', e)
        this.chatError = 'Failed to fetch files'
      }
    },
    async fetchSandwichFiles() {
      try {
        console.log('Получение индекса сэндвичей:', this.apiUrl + '/chat/get_sandwiches_index', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/get_sandwiches_index', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.sandwichFiles = data.files
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка получения индекса сэндвичей:', data)
          this.chatError = data.error || 'Failed to fetch sandwiches index'
        }
      } catch (e) {
        console.error('Ошибка получения индекса сэндвичей:', e)
        this.chatError = 'Failed to fetch sandwiches index'
      }
    },
    async sendMessage(message) {
      if (this.selectedChatId === null) return
      let finalMessage = message
      if (this.pendingAttachment) {
        finalMessage = `${message} @attach#${this.pendingAttachment.file_id}`.trim()
      }
      try {
        console.log('Отправка сообщения:', { chat_id: this.selectedChatId, user_id: 1, message: finalMessage }, 'URL:', this.apiUrl + '/chat/post', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: this.selectedChatId, user_id: 1, message: finalMessage }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.pendingAttachment = null
          await this.fetchHistory()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка отправки сообщения:', data)
          this.chatError = data.error || 'Failed to send message'
        }
      } catch (e) {
        console.error('Ошибка отправки сообщения:', e)
        this.chatError = 'Failed to send message'
      }
    },
    async uploadFile(file, fileName, chatId) {
      if (!file || !fileName) return
      const formData = new FormData()
      formData.append('file', file)
      formData.append('chat_id', chatId)
      formData.append('file_name', fileName)
      try {
        console.log('Загрузка файла:', this.apiUrl + '/chat/upload_file', 'File:', fileName, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/upload_file', {
          method: 'POST',
          body: formData,
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.pendingAttachment = { file_id: data.file_id, file_name: fileName }
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка загрузки файла:', data)
          this.chatError = data.error || 'Failed to upload file'
        }
      } catch (e) {
        console.error('Ошибка загрузки файла:', e)
        this.chatError = 'Failed to upload file'
      }
    },
    async createChat(description) {
      try {
        console.log('Создание чата:', this.apiUrl + '/chat/create', 'Описание:', description, 'ParentMessageId:', this.newChatParentMessageId, 'Cookies:', document.cookie)
        const payload = {
          description: description || 'New Chat',
          parent_msg_id: this.newChatParentMessageId
        }
        const res = await fetch(this.apiUrl + '/chat/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          const { chat_id } = data
          const chatsData = await (await fetch(this.apiUrl + '/chat/list', { credentials: 'include' })).json()
          console.log('Получены чаты после создания:', chatsData)
          this.chats = await this.buildChatTree(chatsData)
          console.log('Построено дерево чатов после создания:', this.chats)
          this.selectedChatId = chat_id
          this.newChatDescription = ''
          this.newChatParentMessageId = null
          await this.fetchHistory()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка создания чата:', data)
          this.chatError = data.error || 'Failed to create chat'
        }
      } catch (e) {
        console.error('Ошибка создания чата:', e)
        this.chatError = 'Failed to create chat'
      }
    },
    async deletePost(postId) {
      try {
        console.log('Удаление сообщения:', this.apiUrl + '/chat/delete_post', 'PostId:', postId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/delete_post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ post_id: postId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchHistory()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка удаления сообщения:', data)
          this.chatError = data.error || 'Failed to delete post'
        }
      } catch (e) {
        console.error('Ошибка удаления сообщения:', e)
        this.chatError = 'Failed to delete post'
      }
    },
    async deleteChat() {
      if (this.selectedChatId === null) return
      try {
        console.log('Удаление чата:', this.apiUrl + '/chat/delete', 'ChatId:', this.selectedChatId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: this.selectedChatId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.chats = await this.buildChatTree(await (await fetch(this.apiUrl + '/chat/list', { credentials: 'include' })).json())
          this.selectedChatId = null
          this.history = []
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка удаления чата:', data)
          this.chatError = data.error || 'Failed to delete chat'
        }
      } catch (e) {
        console.error('Ошибка удаления чата:', e)
        this.chatError = 'Failed to delete chat'
      }
    },
    async deleteFile(fileId) {
      try {
        console.log('Удаление файла:', this.apiUrl + '/chat/delete_file', 'FileId:', fileId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/delete_file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file_id: fileId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка удаления файла:', data)
          this.chatError = data.error || 'Failed to delete file'
        }
      } catch (e) {
        console.error('Ошибка удаления файла:', e)
        this.chatError = 'Failed to delete file'
      }
    },
    async updateFile(fileId, event) {
      const file = event.target.files[0]
      if (!file) return
      const formData = new FormData()
      formData.append('file', file)
      formData.append('file_id', fileId)
      formData.append('file_name', file.name)
      try {
        console.log('Обновление файла:', this.apiUrl + '/chat/update_file', 'FileId:', fileId, 'File:', file.name, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/update_file', {
          method: 'POST',
          body: formData,
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Ошибка сервера:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Ошибка обновления файла:', data)
          this.chatError = data.error || 'Failed to update file'
        }
      } catch (e) {
        console.error('Ошибка обновления файла:', e)
        this.chatError = 'Failed to update file'
      }
    },
    async selectChat(chatId) {
      this.selectedChatId = chatId
      this.currentTab = 'chat'
      await this.fetchHistory()
    },
    openCreateChatModal(parentMessageId) {
      this.newChatDescription = ''
      this.newChatParentMessageId = parentMessageId
      const modal = document.querySelector('dialog[ref="createChatModal"]')
      if (modal) modal.showModal()
    },
    closeCreateChatModal() {
      this.newChatParentMessageId = null
      const modal = document.querySelector('dialog[ref="createChatModal"]')
      if (modal) modal.close()
    },
    clearAttachment() {
      this.pendingAttachment = null
    },
    async buildChatTree(chats) {
      console.log('Building chat tree with:', chats)
      const map = new Map()
      chats.forEach(chat => map.set(chat.chat_id, { ...chat, children: [] }))
      for (const chat of chats) {
        if (chat.parent_msg_id === null) {
          map.get(chat.chat_id).children = []
        } else {
          try {
            console.log('Fetching messages for chat_id:', chat.chat_id)
            const res = await fetch(this.apiUrl + `/chat/get?chat_id=${chat.chat_id}`, {
              method: 'GET',
              credentials: 'include'
            })
            if (res.status === 500 || res.status === 502) {
              console.error('Ошибка сервера при получении сообщений:', res.status)
              this.backendError = true
              continue
            }
            const messages = await res.json()
            if (Array.isArray(messages)) {
              map.get(chat.chat_id).messages = messages
              const parentMsg = messages.find(msg => msg.id === chat.parent_msg_id)
              if (parentMsg && map.get(parentMsg.chat_id)) {
                map.get(parentMsg.chat_id).children.push(map.get(chat.chat_id))
              } else {
                map.get(chat.chat_id).children = []
              }
            } else {
              console.warn('Получены некорректные данные для chat_id:', chat.chat_id, messages)
              this.chatError = messages.error || 'Failed to fetch messages for chat ' + chat.chat_id
            }
          } catch (e) {
            console.error('Ошибка получения сообщений для parent_msg_id:', e)
            this.chatError = 'Failed to build chat tree'
          }
        }
      }
      const tree = Array.from(map.values()).filter(chat => chat.parent_msg_id === null)
      console.log('Chat tree built:', tree)
      return tree
    }
  }
})
