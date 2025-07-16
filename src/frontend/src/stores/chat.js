// /frontend/rtm/src/stores/chat.js, created 2025-07-16 15:55 EEST
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
    apiUrl: import.meta.env.VITE_API_URL || 'http://vps.vpn:8008/api'
  }),
  actions: {
    async fetchHistory() {
      if (this.selectedChatId === null) return
      try {
        console.log('Fetching history:', this.apiUrl + `/chat/get?chat_id=${this.selectedChatId}`, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + `/chat/get?chat_id=${this.selectedChatId}`, {
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
          this.history = data
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error fetching history:', data)
          this.chatError = data.error || 'Failed to fetch chat history'
        }
      } catch (e) {
        console.error('Error fetching history:', e)
        this.chatError = 'Failed to fetch chat history'
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
          const chatsData = await (await fetch(this.apiUrl + '/chat/list', { credentials: 'include' })).json()
          this.chats = await this.buildChatTree(chatsData)
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
          this.chats = await this.buildChatTree(await (await fetch(this.apiUrl + '/chat/list', { credentials: 'include' })).json())
          this.selectedChatId = null
          this.history = []
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
    setChatId(chatId) {
      this.selectedChatId = chatId
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
      this.newChatParentMessageId = null
      const modal = document.getElementById('createChatModal')
      if (modal) {
        modal.close()
      }
    },
    async buildChatTree(chats) {
      console.log('Building chat tree with:', chats)
      const map = new Map()
      chats.forEach(chat => {
        console.log('Processing chat:', chat)
        map.set(chat.chat_id, { ...chat, children: [] })
      })
      for (const chat of chats) {
        if (chat.parent_msg_id === null) {
          map.get(chat.chat_id).children = []
        } else {
          try {
            console.log('Fetching messages for chat_id:', chat.chat_id, 'Parent_msg_id:', chat.parent_msg_id, 'Cookies:', document.cookie)
            const res = await fetch(this.apiUrl + `/chat/get?chat_id=${chat.chat_id}`, {
              method: 'GET',
              credentials: 'include'
            })
            if (res.status === 401) {
              console.error('Unauthorized access for chat_id:', chat.chat_id, 'Response:', res.status)
              this.chatError = 'Session expired'
              continue
            }
            if (res.status === 500 || res.status === 502) {
              console.error('Server error fetching messages for chat_id:', chat.chat_id, 'Status:', res.status)
              this.backendError = true
              continue
            }
            const messages = await res.json()
            console.log('Messages for chat_id:', chat.chat_id, 'Data:', messages)
            if (Array.isArray(messages)) {
              map.get(chat.chat_id).messages = messages
              const parentMsg = messages.find(msg => msg.id === chat.parent_msg_id)
              if (parentMsg && map.get(parentMsg.chat_id)) {
                console.log('Adding chat_id:', chat.chat_id, 'to parent chat_id:', parentMsg.chat_id)
                map.get(parentMsg.chat_id).children.push(map.get(chat.chat_id))
              } else {
                console.warn('No parent message found for chat_id:', chat.chat_id, 'Parent_msg_id:', chat.parent_msg_id)
                map.get(chat.chat_id).children = []
              }
            } else {
              console.warn('Invalid data for chat_id:', chat.chat_id, 'Data:', messages)
              this.chatError = messages.error || 'Failed to fetch messages for chat ' + chat.chat_id
            }
          } catch (e) {
            console.error('Error fetching messages for chat_id:', chat.chat_id, 'Error:', e)
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