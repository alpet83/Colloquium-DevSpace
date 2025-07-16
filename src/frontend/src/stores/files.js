// /frontend/rtm/src/stores/files.js, created 2025-07-16 15:55 EEST
import { defineStore } from 'pinia'

export const useFileStore = defineStore('files', {
  state: () => ({
    files: [],
    pendingAttachment: null,
    chatError: '',
    backendError: false,
    apiUrl: import.meta.env.VITE_API_URL || 'http://vps.vpn:8008/api'
  }),
  actions: {
    async fetchFiles() {
      try {
        console.log('Fetching files:', this.apiUrl + '/chat/list_files', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/list_files', {
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
          this.files = data
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error fetching files:', data)
          this.chatError = data.error || 'Failed to fetch files'
        }
      } catch (e) {
        console.error('Error fetching files:', e)
        this.chatError = 'Failed to fetch files'
      }
    },
    async deleteFile(fileId) {
      try {
        console.log('Deleting file:', this.apiUrl + '/chat/delete_file', 'FileId:', fileId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/delete_file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file_id: fileId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error deleting file:', data)
          this.chatError = data.error || 'Failed to delete file'
        }
      } catch (e) {
        console.error('Error deleting file:', e)
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
        console.log('Updating file:', this.apiUrl + '/chat/update_file', 'FileId:', fileId, 'File:', file.name, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/update_file', {
          method: 'POST',
          body: formData,
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
        } else {
          console.error('Error updating file:', data)
          this.chatError = data.error || 'Failed to update file'
        }
      } catch (e) {
        console.error('Error updating file:', e)
        this.chatError = 'Failed to update file'
      }
    },
    async uploadFile(file, fileName, chatId) {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('chat_id', chatId)
      formData.append('file_name', fileName)
      try {
        console.log('Uploading file:', this.apiUrl + '/chat/upload_file', 'File:', fileName, 'ChatId:', chatId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/upload_file', {
          method: 'POST',
          body: formData,
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          console.error('Server error:', res.status)
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
          console.error('Error uploading file:', data)
          this.chatError = data.error || 'Failed to upload file'
        }
      } catch (e) {
        console.error('Error uploading file:', e)
        this.chatError = 'Failed to upload file'
      }
    },
    clearAttachment() {
      this.pendingAttachment = null
    }
  }
})