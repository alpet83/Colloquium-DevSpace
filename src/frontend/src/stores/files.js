// /frontend/rtm/src/stores/files.js, updated 2025-07-26 17:45 EEST
import { defineStore } from 'pinia'
import { log_msg, log_error } from '../utils/debugging'

export const useFileStore = defineStore('files', {
  state: () => ({
    files: [],
    pendingAttachment: null,
    chatError: '',
    backendError: false,
    apiUrl: import.meta.env.VITE_API_URL || 'http://vps.vpn:8008/api',
    pendingRequests: new Map(), // Словарь активных запросов
    lastRequestTimes: new Map(), // Словарь времени последнего вызова
    minUpdateDelay: new Map([ // Ограничения частоты запросов (мс)
      ['fetchFiles', 5000],
      ['deleteFile', 5000],
      ['updateFile', 5000],
      ['uploadFile', 5000]
    ])
  }),
  actions: {
    async fetchFiles(project_id = null) {
      const requestKey = `fetchFiles_${project_id || 'all'}`;
      const now = Date.now();
      const lastRequestTime = this.lastRequestTimes.get(requestKey) || 0;
      if (now - lastRequestTime < this.minUpdateDelay.get('fetchFiles')) {
        log_msg('FILE', `Request ${requestKey} throttled, waiting ${this.minUpdateDelay.get('fetchFiles') - (now - lastRequestTime)}ms`);
        await new Promise(resolve => setTimeout(resolve, this.minUpdateDelay.get('fetchFiles') - (now - lastRequestTime)));
      }
      if (this.pendingRequests.has(requestKey)) {
        log_msg('FILE', `Request ${requestKey} already pending, awaiting result`);
        return this.pendingRequests.get(requestKey);
      }

      const controller = new AbortController();
      const promise = new Promise(async (resolve, reject) => {
        try {
          log_msg('FILE', 'Fetching files:', this.apiUrl + '/chat/list_files', 'Cookies:', document.cookie);
          const res = await fetch(
            project_id
              ? `${this.apiUrl}/chat/list_files?project_id=${project_id}`
              : `${this.apiUrl}/chat/list_files`,
            {
              method: 'GET',
              credentials: 'include',
              signal: controller.signal
            }
          );
          if (res.status === 500 || res.status === 502) {
            log_error(this, new Error(`Server error: ${res.status}`), 'fetch files');
            this.backendError = true;
            reject(new Error(`Server error: ${res.status}`));
            return;
          }
          const data = await res.json();
          if (res.ok && !data.error) {
            this.files = data;
            log_msg('FILE', 'Fetched files count:', this.files.length);
            this.backendError = false;
            this.chatError = '';
            resolve(data);
          } else {
            log_error(this, new Error(data.error || 'Failed to fetch files'), 'fetch files');
            this.chatError = data.error || 'Failed to fetch files';
            reject(new Error(data.error || 'Failed to fetch files'));
          }
        } catch (e) {
          log_error(this, e, 'fetch files');
          this.chatError = 'Failed to fetch files';
          reject(e);
        } finally {
          this.pendingRequests.delete(requestKey);
          this.lastRequestTimes.set(requestKey, Date.now());
        }
      });

      this.pendingRequests.set(requestKey, promise);
      return promise;
    },
    async fetchFilesAndNotify(project_id = null, activeFiles = []) {
      await this.fetchFiles(project_id);
      this.$mitt.emit('files-updated', activeFiles);
      log_msg('FILE', `Emitted files-updated event for: ${activeFiles}`);
    },
    async deleteFile(fileId) {
      const requestKey = `deleteFile_${fileId}`;
      const now = Date.now();
      const lastRequestTime = this.lastRequestTimes.get(requestKey) || 0;
      if (now - lastRequestTime < this.minUpdateDelay.get('deleteFile')) {
        log_msg('FILE', `Request ${requestKey} throttled, waiting ${this.minUpdateDelay.get('deleteFile') - (now - lastRequestTime)}ms`);
        await new Promise(resolve => setTimeout(resolve, this.minUpdateDelay.get('deleteFile') - (now - lastRequestTime)));
      }
      if (this.pendingRequests.has(requestKey)) {
        log_msg('FILE', `Request ${requestKey} already pending, awaiting result`);
        return this.pendingRequests.get(requestKey);
      }

      const controller = new AbortController();
      const promise = new Promise(async (resolve, reject) => {
        try {
          log_msg('FILE', 'Deleting file:', this.apiUrl + '/chat/delete_file', 'FileId:', fileId, 'Cookies:', document.cookie);
          const res = await fetch(this.apiUrl + '/chat/delete_file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_id: fileId }),
            credentials: 'include',
            signal: controller.signal
          });
          if (res.status === 500 || res.status === 502) {
            log_error(this, new Error(`Server error: ${res.status}`), 'delete file');
            this.backendError = true;
            reject(new Error(`Server error: ${res.status}`));
            return;
          }
          const data = await res.json();
          if (res.ok && !data.error) {
            await this.fetchFilesAndNotify(null, []);
            this.backendError = false;
            this.chatError = '';
            resolve(data);
          } else {
            log_error(this, new Error(data.error || 'Failed to delete file'), 'delete file');
            this.chatError = data.error || 'Failed to delete file';
            reject(new Error(data.error || 'Failed to delete file'));
          }
        } catch (e) {
          log_error(this, e, 'delete file');
          this.chatError = 'Failed to delete file';
          reject(e);
        } finally {
          this.pendingRequests.delete(requestKey);
          this.lastRequestTimes.set(requestKey, Date.now());
        }
      });

      this.pendingRequests.set(requestKey, promise);
      return promise;
    },
    async updateFile(fileId, event) {
      const requestKey = `updateFile_${fileId}`;
      const now = Date.now();
      const lastRequestTime = this.lastRequestTimes.get(requestKey) || 0;
      if (now - lastRequestTime < this.minUpdateDelay.get('updateFile')) {
        log_msg('FILE', `Request ${requestKey} throttled, waiting ${this.minUpdateDelay.get('updateFile') - (now - lastRequestTime)}ms`);
        await new Promise(resolve => setTimeout(resolve, this.minUpdateDelay.get('updateFile') - (now - lastRequestTime)));
      }
      if (this.pendingRequests.has(requestKey)) {
        log_msg('FILE', `Request ${requestKey} already pending, awaiting result`);
        return this.pendingRequests.get(requestKey);
      }

      const file = event.target.files[0];
      if (!file) return;

      const formData = new FormData();
      formData.append('file', file);
      formData.append('file_id', fileId);
      formData.append('file_name', file.name);

      const controller = new AbortController();
      const promise = new Promise(async (resolve, reject) => {
        try {
          log_msg('FILE', 'Updating file:', this.apiUrl + '/chat/update_file', 'FileId:', fileId, 'File:', file.name, 'Cookies:', document.cookie);
          const res = await fetch(this.apiUrl + '/chat/update_file', {
            method: 'POST',
            body: formData,
            credentials: 'include',
            signal: controller.signal
          });
          if (res.status === 500 || res.status === 502) {
            log_error(this, new Error(`Server error: ${res.status}`), 'update file');
            this.backendError = true;
            reject(new Error(`Server error: ${res.status}`));
            return;
          }
          const data = await res.json();
          if (res.ok && !data.error) {
            await this.fetchFilesAndNotify(null, []);
            this.backendError = false;
            this.chatError = '';
            resolve(data);
          } else {
            log_error(this, new Error(data.error || 'Failed to update file'), 'update file');
            this.chatError = data.error || 'Failed to update file';
            reject(new Error(data.error || 'Failed to update file'));
          }
        } catch (e) {
          log_error(this, e, 'update file');
          this.chatError = 'Failed to update file';
          reject(e);
        } finally {
          this.pendingRequests.delete(requestKey);
          this.lastRequestTimes.set(requestKey, Date.now());
        }
      });

      this.pendingRequests.set(requestKey, promise);
      return promise;
    },
    async uploadFile(file, fileName, chatId) {
      const requestKey = `uploadFile_${fileName}_${chatId}`;
      const now = Date.now();
      const lastRequestTime = this.lastRequestTimes.get(requestKey) || 0;
      if (now - lastRequestTime < this.minUpdateDelay.get('uploadFile')) {
        log_msg('FILE', `Request ${requestKey} throttled, waiting ${this.minUpdateDelay.get('uploadFile') - (now - lastRequestTime)}ms`);
        await new Promise(resolve => setTimeout(resolve, this.minUpdateDelay.get('uploadFile') - (now - lastRequestTime)));
      }
      if (this.pendingRequests.has(requestKey)) {
        log_msg('FILE', `Request ${requestKey} already pending, awaiting result`);
        return this.pendingRequests.get(requestKey);
      }

      const formData = new FormData();
      formData.append('file', file);
      formData.append('chat_id', chatId);
      formData.append('file_name', fileName);

      const controller = new AbortController();
      const promise = new Promise(async (resolve, reject) => {
        try {
          log_msg('FILE', 'Uploading file:', this.apiUrl + '/chat/upload_file', 'File:', fileName, 'ChatId:', chatId, 'Cookies:', document.cookie);
          const res = await fetch(this.apiUrl + '/chat/upload_file', {
            method: 'POST',
            body: formData,
            credentials: 'include',
            signal: controller.signal
          });
          if (res.status === 500 || res.status === 502) {
            log_error(this, new Error(`Server error: ${res.status}`), 'upload file');
            this.backendError = true;
            reject(new Error(`Server error: ${res.status}`));
            return;
          }
          const data = await res.json();
          if (res.ok && !data.error) {
            this.pendingAttachment = { file_id: data.file_id, file_name: fileName };
            await this.fetchFilesAndNotify(null, [data.file_id]);
            this.backendError = false;
            this.chatError = '';
            resolve(data);
          } else {
            log_error(this, new Error(data.error || 'Failed to upload file'), 'upload file');
            this.chatError = data.error || 'Failed to upload file';
            reject(new Error(data.error || 'Failed to upload file'));
          }
        } catch (e) {
          log_error(this, e, 'upload file');
          this.chatError = 'Failed to upload file';
          reject(e);
        } finally {
          this.pendingRequests.delete(requestKey);
          this.lastRequestTimes.set(requestKey, Date.now());
        }
      });

      this.pendingRequests.set(requestKey, promise);
      return promise;
    },
    clearAttachment() {
      this.pendingAttachment = null;
      log_msg('FILE', 'Cleared pending attachment');
    }
  }
})