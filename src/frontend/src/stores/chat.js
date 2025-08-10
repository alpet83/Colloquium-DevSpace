// /frontend/rtm/src/stores/chat.js, updated 2025-07-26 15:15 EEST
import { defineStore } from 'pinia'
import { log_msg, log_error } from '../utils/debugging'

export const useChatStore = defineStore('chat', {
  state: () => ({
    chats: [],
    selectedChatId: null,
    history: {},
    quotes: {},
    newChatDescription: '',
    newChatParentMessageId: null,
    chatError: '',
    backendError: false,
    apiUrl: import.meta.env.VITE_API_URL || 'http://vps.vpn:8008/api',
    waitChanges: false,
    need_full_history: false,
    stats: { tokens: null, num_sources_used: null },
    status: { status: 'free', actor: null, elapsed: 0 },
    pollingInterval: null,
    isPolling: false,
    awaited_to_del: []
  }),
  actions: {
    async fetchChats() {
      try {
        log_msg('CHAT', 'Fetching chats:', this.apiUrl + '/chat/list')
        const res = await fetch(this.apiUrl + '/chat/list', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(null, new Error(`Server error: ${res.status}`), 'fetch chats')
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.chats = await this.buildChatTree(data)
          log_msg('CHAT', 'Fetched chats:', JSON.stringify(this.chats, null, 2))
          const activeChat = this.chats.find(chat => chat.active)
          if (activeChat && this.selectedChatId !== activeChat.chat_id) {
            this.selectedChatId = activeChat.chat_id
            this.need_full_history = true
            log_msg('CHAT', 'Selected active chat:', activeChat.chat_id)
            await this.fetchHistory()
          }
          this.backendError = false
          this.chatError = ''
        } else {
          log_error(null, new Error(data.error || 'Failed to fetch chats'), 'fetch chats')
          this.chatError = data.error || 'Failed to fetch chats'
        }
      } catch (e) {
        log_error(null, e, 'fetch chats')
        this.chatError = 'Failed to fetch chats'
      }
    },
    async fetchHistory() {
      if (this.selectedChatId === null || this.isPolling) return
      this.isPolling = true
      const controller = new AbortController()
      const timeoutId = setTimeout(() => {
        controller.abort()
        log_error(null, new Error('Fetch history timeout after 25s'), 'fetch history')
        this.chatError = 'Fetch history timeout'
      }, 25000)
      try {
        const url = this.need_full_history
          ? `${this.apiUrl}/chat/get?chat_id=${this.selectedChatId}`
          : `${this.apiUrl}/chat/get?chat_id=${this.selectedChatId}&wait_changes=1`
        log_msg('CHAT', 'Polling with wait_changes:', !this.need_full_history)
        log_msg('CHAT', 'Fetching history:', url)
        const res = await fetch(url, {
          method: 'GET',
          credentials: 'include',
          signal: controller.signal
        })
        clearTimeout(timeoutId)
        if (res.status === 500 || res.status === 502) {
          log_error(null, new Error(`Server error: ${res.status}`), 'fetch history')
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          if (data.chat_id !== this.selectedChatId) {
            log_msg('CHAT', 'Ignoring history response for outdated chat_id:', data.chat_id, 'Current:', this.selectedChatId)
            return
          }
          if (data.posts && data.posts.chat_history === 'chat switch') {
            log_msg('CHAT', 'Chat switch detected, fetching full history')
            this.waitChanges = false
            this.need_full_history = true
            this.status = data.status
            await this.fetchHistory()
          } else if (data.posts && data.posts.chat_history === 'no changes') {
            if (this.awaited_to_del.length > 0) {
              log_msg('CHAT', 'No changes received, but posts awaiting deletion:', this.awaited_to_del)
              this.chatError = 'Posts awaiting deletion not confirmed, fetching full history'
            } else {
              this.need_full_history = false
            }
            this.status = data.status
            log_msg('CHAT', 'No changes in chat history, status:', this.status)
          } else {
            const newHistory = {}
            const deletedIds = new Set(Object.values(data.posts)
                .filter(post => post.action === 'delete')
                .map(post => post.id))
            for (const [postId, post] of Object.entries(data.posts)) {
              if (post.action !== 'delete') {
                newHistory[postId] = post
                log_msg('CHAT', `Have changes, added post ${postId}, refreshing UI`)
              }
            }
            for (const postId of deletedIds) {
              delete newHistory[postId]
            }
            this.$patch({
              history: newHistory,
              quotes: data.quotes || {}
            })
            log_msg('CHAT', 'Fetched quotes:', JSON.stringify(data.quotes || {}, null, 2))
            this.awaited_to_del = this.awaited_to_del.filter(postId => !deletedIds.has(postId))
            if (this.awaited_to_del.length > 0) {
              log_msg('CHAT', 'Posts not deleted:', this.awaited_to_del)
            } else if (deletedIds.size > 0) {
              log_msg('CHAT', 'Deleted posts processed:', Array.from(deletedIds))
              this.awaited_to_del = []
            }
            this.need_full_history = false
            this.status.status = 'free'
            log_msg('CHAT', 'Reset status to free after fetchHistory')
            if (deletedIds.size === 0) {
              await this.scrollToBottom()
            } else {
              log_msg('CHAT', 'Skipped scrollToBottom due to deleted posts:', Array.from(deletedIds))
            }
          }
          this.backendError = false
          this.chatError = ''
        } else {
          log_error(null, new Error(data.error || 'Failed to fetch chat history'), 'fetch history')
          this.chatError = data.error || 'Failed to fetch chat history'
        }
      } catch (e) {
        if (e.name === 'AbortError') {
          log_error(null, e, 'fetch history aborted due to timeout')
          this.chatError = 'Fetch history timeout'
        } else {
          log_error(null, e, 'fetch history')
          this.chatError = 'Failed to fetch chat history'
        }
      } finally {
        this.isPolling = false
        clearTimeout(timeoutId)
      }
    },
    async scrollToBottom() {
      await new Promise(resolve => setTimeout(resolve, 0))
      const container = document.querySelector('.messages')
      if (container) {
        container.scrollTop = container.scrollHeight
        log_msg('UI', 'Scrolled to bottom')
      }
    },
    async fetchChatStats() {
      if (this.selectedChatId === null) return
      try {
        log_msg('CHAT', 'Fetching chat stats:', this.apiUrl + '/chat/get_stats?chat_id=' + this.selectedChatId)
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
          log_msg('CHAT', 'Fetched chat stats:', this.stats)
        } else {
          log_error(null, new Error('Failed to fetch chat stats'), 'fetch chat stats')
          this.chatError = 'Failed to fetch chat stats'
        }
      } catch (e) {
        log_error(null, e, 'fetch chat stats')
        this.chatError = 'Failed to fetch chat stats'
      }
    },
    async sendMessage(message) {
      if (this.selectedChatId === null) return
      if (!message) {
        log_error(null, new Error('No message provided'), 'send message')
        this.chatError = 'No message provided'
        return
      }
      try {
        log_msg('CHAT', 'Sending message to backend:', { chat_id: this.selectedChatId, message })
        const res = await fetch(this.apiUrl + '/chat/post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: this.selectedChatId, message }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(null, new Error(`Server error: ${res.status}`), 'send message')
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
          log_error(null, new Error(data.error || 'Failed to send message'), 'send message')
          this.chatError = data.error || 'Failed to send message'
        }
      } catch (e) {
        log_error(null, e, 'send message')
        this.chatError = 'Failed to send message'
      }
    },
    async editPost(postId, message) {
      if (!postId || !message) {
        log_error(null, new Error('No postId or message provided'), 'edit post')
        this.chatError = 'No postId or message provided'
        return
      }
      try {
        log_msg('CHAT', 'Editing post on backend:', this.apiUrl + '/chat/edit_post', 'PostId:', postId, 'Message:', message)
        const res = await fetch(this.apiUrl + '/chat/edit_post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ post_id: postId, message }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(null, new Error(`Server error: ${res.status}`), 'edit post')
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
          log_error(null, new Error(data.error || 'Failed to edit post'), 'edit post')
          this.chatError = data.error || 'Failed to edit post'
        }
      } catch (e) {
        log_error(null, e, 'edit post')
        this.chatError = 'Failed to edit post'
      }
    },
    async deletePost(postId, postUserId, userId, userRole) {
      if (userRole !== 'admin' && postUserId !== userId) {
        log_error(null, new Error('Only admins can delete posts by other users'), 'delete post')
        this.chatError = 'Only admins can delete posts by other users'
        return
      }
      try {
        log_msg('CHAT', 'Deleting post:', this.apiUrl + '/chat/delete_post', 'PostId:', postId)
        this.awaited_to_del.push(postId)
        log_msg('CHAT', 'Added post_id to awaited_to_del:', postId, 'Current awaited_to_del:', this.awaited_to_del)
        const res = await fetch(this.apiUrl + '/chat/delete_post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ post_id: postId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(null, new Error(`Server error: ${res.status}`), 'delete post')
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          log_msg('CHAT', 'Post deletion requested, fetching updated history')
          const postElement = document.getElementById(`post_${postId}`)
          if (postElement) {
            postElement.remove()
            log_msg('UI', `Removed post_${postId} from DOM`)
          }
          await this.fetchHistory()
          await this.fetchChatStats()
          this.backendError = false
          this.chatError = ''
        } else {
          log_error(null, new Error(data.error || 'Failed to delete post'), 'delete post')
          this.chatError = data.error || 'Failed to delete post'
          this.awaited_to_del = this.awaited_to_del.filter(id => id !== postId)
          log_msg('CHAT', 'Removed post_id from awaited_to_del due to error:', postId, 'Current awaited_to_del:', this.awaited_to_del)
        }
      } catch (e) {
        log_error(null, e, 'delete post')
        this.chatError = 'Failed to delete post'
        this.awaited_to_del = this.awaited_to_del.filter(id => id !== postId)
        log_msg('CHAT', 'Removed post_id from awaited_to_del due to exception:', postId, 'Current awaited_to_del:', this.awaited_to_del)
      }
    },
    async createChat(description) {
      try {
        log_msg('CHAT', 'Creating chat:', this.apiUrl + '/chat/create', 'Description:', description, 'ParentMessageId:', this.newChatParentMessageId)
        const res = await fetch(this.apiUrl + '/chat/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ description, parent_msg_id: this.newChatParentMessageId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(null, new Error(`Server error: ${res.status}`), 'create chat')
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
          log_error(null, new Error(data.error || 'Failed to create chat'), 'create chat')
          this.chatError = data.error || 'Failed to create chat'
        }
      } catch (e) {
        log_error(null, e, 'create chat')
        this.chatError = 'Failed to create chat'
      }
    },
    async setChatId(chatId) {
      if (this.selectedChatId === chatId) return
      try {
        log_msg('CHAT', 'Notifying chat switch:', this.apiUrl + '/chat/notify_switch', 'ChatId:', chatId)
        await fetch(this.apiUrl + '/chat/notify_switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chatId }),
          credentials: 'include'
        })
      } catch (e) {
        log_error(null, e, 'notify chat switch')
      }
      this.selectedChatId = chatId
      this.history = {}
      this.quotes = {}
      this.waitChanges = false
      this.need_full_history = true
      log_msg('CHAT', 'Selected chat ID:', chatId)
      await this.fetchHistory()
      await this.fetchChatStats()
    },
    openCreateChatModal(parentMessageId) {
      log_msg('ACTION', 'openCreateChatModal called with parentMessageId:', parentMessageId)
      this.newChatDescription = ''
      this.newChatParentMessageId = parentMessageId
      const modal = document.getElementById('createChatModal')
      if (modal) {
        log_msg('UI', 'Found modal:', modal)
        modal.showModal()
      } else {
        log_error(null, new Error('Create chat modal not found'), 'open create chat modal')
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
        log_msg('CHAT', 'Cleared previous polling interval')
      }
      this.pollingInterval = setInterval(() => {
        if (this.selectedChatId && !this.isPolling) {
          this.waitChanges = !this.need_full_history
          this.fetchHistory()
        }
      }, 1000)
      log_msg('CHAT', 'Started polling with interval ID:', this.pollingInterval)
    },
    stopPolling() {
      if (this.pollingInterval) {
        clearInterval(this.pollingInterval)
        log_msg('CHAT', 'Stopped polling with interval ID:', this.pollingInterval)
        this.pollingInterval = null
      }
    },
    async buildChatTree(chats) {
      log_msg('CHAT', 'Building chat tree with:', JSON.stringify(chats, null, 2))
      const map = new Map()
      chats.forEach(chat => {
        map.set(chat.chat_id, { ...chat, children: [] })
        log_msg('CHAT', `Initialized chat ${chat.chat_id} with description: ${chat.description}`)
      })
      for (const chat of chats) {
        if (chat.parent_msg_id === null) {
          log_msg('CHAT', `Root chat: ${chat.chat_id} (${chat.description})`)
          continue
        }
        const parentMsg = await this.dbFetchParentMsg(chat.parent_msg_id)
        if (parentMsg && map.has(parentMsg.chat_id)) {
          log_msg('CHAT', `Adding chat_id: ${chat.chat_id} (${chat.description}) to parent chat_id: ${parentMsg.chat_id}`)
          map.get(parentMsg.chat_id).children.push(map.get(chat.chat_id))
        } else {
          log_msg('CHAT', `No parent message found for chat_id: ${chat.chat_id}, parent_msg_id: ${chat.parent_msg_id}, setting as root`)
          map.get(chat.chat_id).parent_msg_id = null
        }
      }
      const tree = Array.from(map.values()).filter(chat => chat.parent_msg_id === null)
      log_msg('CHAT', 'Chat tree built:', JSON.stringify(tree, null, 2))
      return tree
    },
    async dbFetchParentMsg(parent_msg_id) {
      try {
        log_msg('CHAT', `Fetching parent message for post_id: ${parent_msg_id}`)
        const res = await fetch(this.apiUrl + `/chat/get_parent_msg?post_id=${parent_msg_id}`, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 404 || !res.ok) {
          log_msg('CHAT', 'Parent message not found for post_id:', parent_msg_id)
          return null
        }
        const data = await res.json()
        log_msg('CHAT', 'Fetched parent message:', JSON.stringify(data, null, 2))
        return data
      } catch (e) {
        log_error(null, e, 'fetch parent message')
        return null
      }
    }
  }
})