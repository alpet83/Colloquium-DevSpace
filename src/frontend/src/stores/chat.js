// /frontend/rtm/src/stores/chat.js, updated 2025-07-17 15:38 EEST
import { defineStore } from 'pinia'

export const useChatStore = defineStore('chat', {
  state: () => ({
    chats: [],
    selectedChatId: null,
    history: [],
    newChatDescription: '',
    newChatParentMessageId: null,
    chatError: '',
    backendError: false,
    apiUrl: import.meta.env.VITE_API_URL || 'http://vps.vpn:8008/api',
    waitChanges: false,
    stats: { tokens: null, num_sources_used: null },
    pollingInterval: null,
    isPolling: false
  }),
  actions: {
    async fetchChats() {
      try {
        console.log('Fetching chats:', this.apiUrl + '/chat/list', 'Cookies:', document.cookie)
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
        if (res.ok && !data.error) {
          this.chats = await this.buildChatTree(data)
          console.log('Fetched chats:', JSON.stringify(this.chats, null, 2))
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error fetching chats:', data)
          this.chatError = data.error || 'Failed to fetch chats'
        }
      } catch (e) {
        console.error('Error fetching chats:', e)
        this.chatError = 'Failed to fetch chats'
      }
    },
    async fetchHistory() {
      if (this.selectedChatId === null || this.isPolling) return
      this.isPolling = true
      try {
        const url = this.waitChanges
          ? `${this.apiUrl}/chat/get?chat_id=${this.selectedChatId}&wait_changes=1`
          : `${this.apiUrl}/chat/get?chat_id=${this.selectedChatId}`
        console.log('Fetching history:', url, 'Cookies:', document.cookie)
        const res = await fetch(url, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          if (data.chat_history === 'chat switch') {
            console.log('Chat switch detected, fetching full history')
            this.waitChanges = false
            await this.fetchHistory()
          } else if (data.chat_history !== 'no changes') {
            // Обновляем историю, обрабатывая добавления, изменения и удаления
            const existingIds = new Set(this.history.map(post => post.id))
            this.history = this.history.filter(post => !data.some(newPost => newPost.id === post.id && newPost.action === 'delete'))
            data.forEach(newPost => {
              if (newPost.action === 'delete') {
                this.history = this.history.filter(post => post.id !== newPost.id)
              } else if (!existingIds.has(newPost.id)) {
                this.history.push(newPost)
              } else {
                const index = this.history.findIndex(post => post.id === newPost.id)
                if (index !== -1) {
                  this.history[index] = newPost
                }
              }
            })
            this.history.sort((a, b) => a.id - b.id)            
            console.log('Deleted posts:', this.history.filter(post => post.action === 'delete'))
            console.log('Fetch history params:', { chat_id: this.selectedChatId, wait_changes: this.waitChanges })
            console.log('History response:', JSON.stringify(data, null, 2))
          } else {
            console.log('No changes in chat history')
          }
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error fetching history:', data)
          this.chatError = data.error || 'Failed to fetch chat history'
        }
      } catch (e) {
        console.error('Error fetching history:', e)
        this.chatError = 'Failed to fetch chat history'
      } finally {
        this.isPolling = false
      }
    },
    async fetchChatStats() {
      if (this.selectedChatId === null) return
      try {
        const res = await fetch(`${this.apiUrl}/chat/get_stats?chat_id=${this.selectedChatId}`, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.ok) {
          const data = await res.json()
          this.stats = {
            tokens: data.tokens,
            num_sources_used: data.num_sources_used
          }
          console.log('Fetched chat stats:', this.stats)
        } else {
          console.error('Error fetching chat stats:', await res.json())
        }
      } catch (e) {
        console.error('Error fetching chat stats:', e)
      }
    },
    async sendMessage(message) {
      if (this.selectedChatId === null) return
      try {
        console.log('Sending message:', { chat_id: this.selectedChatId, message }, 'URL:', this.apiUrl + '/chat/post', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: this.selectedChatId, message }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchHistory()
          await this.fetchChatStats()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error sending message:', data)
          this.chatError = data.error || 'Failed to send message'
        }
      } catch (e) {
        console.error('Error sending message:', e)
        this.chatError = 'Failed to send message'
      }
    },
    async editPost(postId, message) {
      try {
        console.log('Editing post:', this.apiUrl + '/chat/edit_post', 'PostId:', postId, 'Message:', message, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/edit_post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ post_id: postId, message }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchHistory()
          await this.fetchChatStats()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error editing post:', data)
          this.chatError = data.error || 'Failed to edit post'
        }
      } catch (e) {
        console.error('Error editing post:', e)
        this.chatError = 'Failed to edit post'
      }
    },
    async deletePost(postId, postUserId, userId, userRole) {
      if (userRole !== 'admin' && postUserId !== userId) {
        this.chatError = 'Only admins can delete posts by other users'
        return
      }
      try {
        console.log('Deleting post:', this.apiUrl + '/chat/delete_post', 'PostId:', postId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/delete_post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ post_id: postId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchHistory()
          await this.fetchChatStats()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error deleting post:', data)
          this.chatError = data.error || 'Failed to delete post'
        }
      } catch (e) {
        console.error('Error deleting post:', e)
        this.chatError = 'Failed to delete post'
      }
    },
    async createChat(description) {
      try {
        console.log('Creating chat:', this.apiUrl + '/chat/create', 'Description:', description, 'ParentMessageId:', this.newChatParentMessageId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ description, parent_msg_id: this.newChatParentMessageId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.newChatDescription = ''
          this.newChatParentMessageId = null
          await this.fetchChats()
          this.closeCreateChatModal()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error creating chat:', data)
          this.chatError = data.error || 'Failed to create chat'
        }
      } catch (e) {
        console.error('Error creating chat:', e)
        this.chatError = 'Failed to create chat'
      }
    },
    async deleteChat() {
      if (this.selectedChatId === null) return
      try {
        console.log('Deleting chat:', this.apiUrl + '/chat/delete', 'ChatId:', this.selectedChatId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: this.selectedChatId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.selectedChatId = null
          await this.fetchChats()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error deleting chat:', data)
          this.chatError = data.error || 'Failed to delete chat'
        }
      } catch (e) {
        console.error('Error deleting chat:', e)
        this.chatError = 'Failed to delete chat'
      }
    },
    async setChatId(chatId) {
      if (this.selectedChatId === chatId) return
      // Уведомляем бэкенд о смене чата
      try {
        console.log('Notifying chat switch:', this.apiUrl + '/chat/notify_switch', 'ChatId:', chatId, 'Cookies:', document.cookie)
        await fetch(this.apiUrl + '/chat/notify_switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chatId }),
          credentials: 'include'
        })
      } catch (e) {
        console.error('Error notifying chat switch:', e)
      }
      this.selectedChatId = chatId
      this.history = [] // Очищаем историю при переключении чата
      this.waitChanges = false // Полная загрузка истории при выборе чата
      console.log('Selected chat ID:', chatId)
      await this.fetchHistory()
      await this.fetchChatStats()
    },
    openCreateChatModal(parentMessageId) {
      console.log('openCreateChatModal called with parentMessageId:', parentMessageId)
      this.newChatDescription = ''
      this.newChatParentMessageId = parentMessageId
      const modal = document.getElementById('createChatModal')
      if (modal) {
        console.log('Found modal:', modal)
        modal.showModal()
      } else {
        console.error('Create chat modal not found')
        this.chatError = 'Failed to open create chat modal'
      }
    },
    closeCreateChatModal() {
      this.newChatDescription = ''
      this.newChatParentMessageId = null
      const modal = document.getElementById('createChatModal')
      if (modal) {
        modal.close()
      }
    },
    startPolling() {
      if (this.pollingInterval) {
        clearInterval(this.pollingInterval)
        console.log('Cleared previous polling interval')
      }
      this.pollingInterval = setInterval(() => {
        if (this.selectedChatId && !this.isPolling) {
          this.waitChanges = true // Пуллинг только для изменений
          this.fetchHistory()
        }
      }, 15000) // Пуллинг каждые 15 секунд
      console.log('Started polling with interval ID:', this.pollingInterval)
    },
    stopPolling() {
      if (this.pollingInterval) {
        clearInterval(this.pollingInterval)
        console.log('Stopped polling with interval ID:', this.pollingInterval)
        this.pollingInterval = null
      }
    },
    async buildChatTree(chats) {
      console.log('Building chat tree with:', JSON.stringify(chats, null, 2))
      const map = new Map()
      chats.forEach(chat => {
        map.set(chat.chat_id, { ...chat, children: [] })
        console.log(`Initialized chat ${chat.chat_id} with description: ${chat.description}`)
      })
      for (const chat of chats) {
        if (chat.parent_msg_id === null) {
          console.log(`Root chat: ${chat.chat_id} (${chat.description})`)
          continue
        }
        const parentMsg = await this.dbFetchParentMsg(chat.parent_msg_id)
        if (parentMsg && map.has(parentMsg.chat_id)) {
          console.log(`Adding chat_id: ${chat.chat_id} (${chat.description}) to parent chat_id: ${parentMsg.chat_id}`)
          map.get(parentMsg.chat_id).children.push(map.get(chat.chat_id))
        } else {
          console.warn(`No parent message found for chat_id: ${chat.chat_id}, parent_msg_id: ${chat.parent_msg_id}, setting as root`)
          map.get(chat.chat_id).parent_msg_id = null
        }
      }
      const tree = Array.from(map.values()).filter(chat => chat.parent_msg_id === null)
      console.log('Chat tree built:', JSON.stringify(tree, null, 2))
      return tree
    },
    async dbFetchParentMsg(parent_msg_id) {
      try {
        console.log(`Fetching parent message for post_id: ${parent_msg_id}`)
        const res = await fetch(this.apiUrl + `/chat/get_parent_msg?post_id=${parent_msg_id}`, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 404 || !res.ok) {
          console.warn('Parent message not found for post_id:', parent_msg_id)
          return null
        }
        const data = await res.json()
        console.log('Fetched parent message:', JSON.stringify(data, null, 2))
        return data
      } catch (e) {
        console.error('Error fetching parent message:', e)
        return null
      }
    }
  }
})