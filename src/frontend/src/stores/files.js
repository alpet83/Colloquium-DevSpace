// /frontend/rtm/src/stores/files.js, updated 2025-07-22 17:00 EEST
import { defineStore } from 'pinia'
import { log_msg, log_error } from '../utils/debugging'

export const useFileStore = defineStore('files', {
  state: () => ({
    files: [],
    pendingAttachment: null,
    chatError: '',
    backendError: false,
    apiUrl: import.meta.env.VITE_API_URL || 'http://vps.vpn:8008/api'
  }),
  actions: {
    async fetchFiles(project_id = null) {
      try {
        log_msg('FILE', 'Fetching files:', this.apiUrl + '/chat/list_files', 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/list_files', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(this, new Error(`Server error: ${res.status}`), 'fetch files')
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.files = data
          log_msg('FILE', 'Fetched files count:', this.files.length)
          this.backendError = false
          this.chatError = ''
        } else {
          log_error(this, new Error(data.error || 'Failed to fetch files'), 'fetch files')
          this.chatError = data.error || 'Failed to fetch files'
        }
      } catch (e) {
        log_error(this, e, 'fetch files')
        this.chatError = 'Failed to fetch files'
      }
    },
    async deleteFile(fileId) {
      try {
        log_msg('FILE', 'Deleting file:', this.apiUrl + '/chat/delete_file', 'FileId:', fileId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/delete_file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file_id: fileId }),
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(this, new Error(`Server error: ${res.status}`), 'delete file')
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
        } else {
          log_error(this, new Error(data.error || 'Failed to delete file'), 'delete file')
          this.chatError = data.error || 'Failed to delete file'
        }
      } catch (e) {
        log_error(this, e, 'delete file')
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
        log_msg('FILE', 'Updating file:', this.apiUrl + '/chat/update_file', 'FileId:', fileId, 'File:', file.name, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/update_file', {
          method: 'POST',
          body: formData,
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(this, new Error(`Server error: ${res.status}`), 'update file')
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
        } else {
          log_error(this, new Error(data.error || 'Failed to update file'), 'update file')
          this.chatError = data.error || 'Failed to update file'
        }
      } catch (e) {
        log_error(this, e, 'update file')
        this.chatError = 'Failed to update file'
      }
    },
    async uploadFile(file, fileName, chatId) {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('chat_id', chatId)
      formData.append('file_name', fileName)
      try {
        log_msg('FILE', 'Uploading file:', this.apiUrl + '/chat/upload_file', 'File:', fileName, 'ChatId:', chatId, 'Cookies:', document.cookie)
        const res = await fetch(this.apiUrl + '/chat/upload_file', {
          method: 'POST',
          body: formData,
          credentials: 'include'
        })
        if (res.status === 500 || res.status === 502) {
          log_error(this, new Error(`Server error: ${res.status}`), 'upload file')
          this.backendError = true
          return
        }
        const data = await res.json()
        if (res.ok && !data.error) {
          this.pendingAttachment = { file_id: data.file_id, file_name: fileName }
          await this.fetchFiles()
          this.backendError = false
          this.chatError = ''
          return data
        } else {
          log_error(this, new Error(data.error || 'Failed to upload file'), 'upload file')
          this.chatError = data.error || 'Failed to upload file'
          throw new Error(data.error || 'Failed to upload file')
        }
      } catch (e) {
        log_error(this, e, 'upload file')
        this.chatError = 'Failed to upload file'
        throw e
      }
    },
    clearAttachment() {
      this.pendingAttachment = null
      log_msg('FILE', 'Cleared pending attachment')
    }
  }
})