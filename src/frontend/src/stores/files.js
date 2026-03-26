// /frontend/rtm/src/stores/files.js, updated 2025-07-26 19:45 EEST
import { defineStore } from 'pinia'
import { log_msg, log_error } from '../utils/debugging'
import { makeRequest } from '../utils/requests'
import mitt from '../utils/mitt'


export const useFileStore = defineStore('files', {
  state: () => ({
    files: [],
    fileMetaById: {},
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
  getters: {
    knownFiles(state) {
      return Object.values(state.fileMetaById)
    }
  },
  actions: {
    mergeFileMetadata(entries = []) {
      if (!Array.isArray(entries) || entries.length === 0) return
      const nextMeta = { ...this.fileMetaById }
      entries.forEach(entry => {
        if (!entry || !entry.id) return
        nextMeta[entry.id] = {
          ...(nextMeta[entry.id] || {}),
          ...entry
        }
      })
      this.fileMetaById = nextMeta
    },
    async fetchFileTree(project_id = null, path = '', depth = 3) {
      const params = new URLSearchParams()
      if (project_id !== null && project_id !== undefined) {
        params.set('project_id', String(project_id))
      }
      if (path) {
        params.set('path', path)
      }
      params.set('depth', String(depth))

      const requestKey = `fetchFileTree_${project_id || 'all'}_${path || 'root'}_${depth}`
      const data = await makeRequest(
        this,
        `/project/file_tree?${params.toString()}`,
        { method: 'GET' },
        requestKey,
        0
      )
      return data || { trees: [] }
    },
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
        this.mergeFileMetadata(data);
        log_msg('FILE', 'Fetched files count:', this.files.length, 'project_id:', effectiveProjectId);
      }
    },
    async fetchFileMetadata(fileIds = [], project_id = null) {
      const normalizedIds = [...new Set(
        (Array.isArray(fileIds) ? fileIds : [])
          .map(fileId => parseInt(fileId, 10))
          .filter(fileId => Number.isInteger(fileId) && fileId > 0)
      )]
      if (normalizedIds.length === 0) return []

      const params = new URLSearchParams()
      params.set('file_ids', normalizedIds.join(','))
      if (project_id !== null && project_id !== undefined) {
        params.set('project_id', String(project_id))
      }

      const requestKey = `fetchFileMetadata_${project_id || 'all'}_${normalizedIds.join('_')}`
      const data = await makeRequest(
        this,
        `/project/file_index?${params.toString()}`,
        { method: 'GET' },
        requestKey,
        0
      )
      if (data) {
        this.mergeFileMetadata(data)
        if (mitt) {
          mitt.emit('file-meta-updated', normalizedIds)
        }
        log_msg('FILE', 'Fetched file metadata count:', data.length, 'requested ids:', normalizedIds)
        return data
      }
      return []
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
        const nextMeta = { ...this.fileMetaById }
        delete nextMeta[fileId]
        this.fileMetaById = nextMeta
        this.files = this.files.filter(file => file.id !== fileId)
        if (mitt) {
          mitt.emit('file-meta-updated', [fileId])
          mitt.emit('file-tree-changed', { action: 'delete', file_ids: [fileId], project_id: this.selectedProject })
        }
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
        await this.fetchFileMetadata([fileId], this.selectedProject)
        if (mitt) {
          mitt.emit('file-tree-changed', { action: 'update', file_ids: [fileId], project_id: this.selectedProject })
        }
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
        await this.fetchFileMetadata([data.file_id], this.selectedProject)
        if (mitt) {
          mitt.emit('file-tree-changed', { action: 'upload', file_ids: [data.file_id], project_id: this.selectedProject })
        }
      }
    },
    clearAttachment() {
      this.pendingAttachment = null;
      log_msg('FILE', 'Cleared pending attachment');
    } 
  }
})