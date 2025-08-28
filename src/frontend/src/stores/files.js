// /frontend/rtm/src/stores/files.js, updated 2025-07-26 19:45 EEST
import { defineStore } from 'pinia'
import { log_msg, log_error } from '../utils/debugging'
import { makeRequest } from '../utils/requests'
import mitt from '../utils/mitt'


export const useFileStore = defineStore('files', {
  state: () => ({
    files: [],
    pendingAttachment: null,
    chatError: '',
    backendError: false,
    apiUrl: './api',
    pendingRequests: new Map(),
    lastRequestTimes: new Map(),
    minUpdateDelay: new Map([
      ['fetchFiles', 5000],
      ['deleteFile', 5000],
      ['updateFile', 5000],
      ['uploadFile', 5000]
    ]),
    selectedProject: null // Текущий выбранный проект
  }),
  actions: {
    setSelectedProject(project_id) {
      this.selectedProject = project_id ? parseInt(project_id) : null;
      log_msg('FILE', 'Selected project in store:', this.selectedProject);
    },
    async fetchFiles(project_id = null) {
      // Используем selectedProject, если project_id не передан
      const effectiveProjectId = project_id !== null ? project_id : this.selectedProject;
      const url = effectiveProjectId !== null
        ? `/chat/list_files?project_id=${effectiveProjectId}`
        : '/chat/list_files';
      const requestKey = `fetchFiles_${effectiveProjectId || 'all'}`;
      const data = await makeRequest(this, url, { method: 'GET' }, requestKey, this.minUpdateDelay.get('fetchFiles'));
      if (data) {
        this.files = data;
        log_msg('FILE', 'Fetched files count:', this.files.length, 'project_id:', effectiveProjectId);
      }
    },
    async fetchFilesAndNotify(project_id = null, activeFiles = []) {
      await this.fetchFiles(project_id);
      if (mitt) {
        mitt.emit('files-updated', activeFiles);
        log_msg('FILE', `Emitted files-updated event for: ${activeFiles}`);
      } else {
        log_error(null, new Error('mitt is undefined, cannot emit files-updated'), 'fetchFilesAndNotify');
      }
    },
    async deleteFile(fileId) {
      const data = await makeRequest(this, '/chat/delete_file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_id: fileId })
      }, `deleteFile_${fileId}`, this.minUpdateDelay.get('deleteFile'));
      if (data) {
        await this.fetchFilesAndNotify(null, []);
      }
    },
    async updateFile(fileId, event) {
      const file = event.target.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append('file', file);
      formData.append('file_id', fileId);
      formData.append('file_name', file.name);
      const data = await makeRequest(this, '/chat/update_file', {
        method: 'POST',
        body: formData
      }, `updateFile_${fileId}`, this.minUpdateDelay.get('updateFile'));
      if (data) {
        await this.fetchFilesAndNotify(null, []);
      }
    },
    async uploadFile(file, fileName, chatId) {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('chat_id', chatId);
      formData.append('file_name', fileName);
      const data = await makeRequest(this, '/chat/upload_file', {
        method: 'POST',
        body: formData
      }, `uploadFile_${fileName}_${chatId}`, this.minUpdateDelay.get('uploadFile'));
      if (data) {
        this.pendingAttachment = { file_id: data.file_id, file_name: fileName };
        await this.fetchFilesAndNotify(null, [data.file_id]);
      }
    },
    clearAttachment() {
      this.pendingAttachment = null;
      log_msg('FILE', 'Cleared pending attachment');
    } 
  }
})