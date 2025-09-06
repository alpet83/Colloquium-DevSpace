<!-- /frontend/rtm/src/components/RightPanel.vue, updated 2025-07-27 10:30 EEST -->
<template>
  <div class="right-panel" :class="{ collapsed: isCollapsed }">
    <button class="toggle-btn" @click="toggleCollapse">
      {{ isCollapsed ? '◄' : '▶' }}
    </button>
    <div v-if="!isCollapsed" class="panel-content">
      <button @click="openCreateProjectModal">Создать проект</button>
      <button v-if="selectedProject" @click="openEditProjectModal">Редактировать проект</button>
      <select v-model="selectedProject" @change="selectProject">
        <option value="">Все файлы</option>
        <option v-for="project in projects" :value="project.id" :key="project.id">{{ project.project_name }}</option>
      </select>
      <div class="search-settings" :class="{ collapsed: !isDisplayedSS }">
        <h3><u @click="toggleDisplaySS">Настройки поиска {{ isDisplayedSS ? '' : '>>' }}</u></h3>        
        <label>Режим поиска:</label>
        <select v-model="searchSettings.mode">
          <option value="off">Отключён</option>
          <option value="auto">Авто</option>
          <option value="on">Включён</option>
        </select>
        <label>Источники:</label>
        <table class="sources" width="100%">
          <tr>
            <td>Web</td>
            <td>X</td>
            <td>News</td>
            <td>Макс.</td>
          </tr>
          <tr>
            <td><input type="checkbox" v-model="searchSettings.sources" value="web" /></td>
            <td><input type="checkbox" v-model="searchSettings.sources" value="x" /></td>
            <td><input type="checkbox" v-model="searchSettings.sources" value="news" /></td>
            <td><input title="Макс. количество результатов" type="number" v-model="searchSettings.max_search_results" min="1" max="50"  />&nbsp;</td>
          </tr>
        </table>       
        
        <button @click="saveSearchSettings">Сохранить</button>
      </div>
      <dialog ref="createProjectModal">
        <h3>Создать проект</h3>
        <input v-model="newProject.project_name" placeholder="Название проекта" />
        <input v-model="newProject.description" placeholder="Описание" />
        <input v-model="newProject.local_git" placeholder="Локальный Git (опционально)" />
        <input v-model="newProject.public_git" placeholder="Публичный Git (опционально)" />
        <input v-model="newProject.dependencies" placeholder="Зависимости (опционально)" />
        <button @click="createProject">Создать</button>
        <button @click="closeCreateProjectModal">Отмена</button>
      </dialog>
      <dialog ref="editProjectModal">
        <h3>Редактировать проект</h3>
        <input v-model="editProject.project_name" placeholder="Название проекта" />
        <input v-model="editProject.description" placeholder="Описание" />
        <input v-model="editProject.local_git" placeholder="Локальный Git (опционально)" />
        <input v-model="editProject.public_git" placeholder="Публичный Git (опционально)" />
        <input v-model="editProject.dependencies" placeholder="Зависимости (опционально)" />
        <button @click="updateProject">Сохранить</button>
        <button @click="closeEditProjectModal">Отмена</button>
      </dialog>
      <div v-if="filteredFiles.length" class="file-tree">
        <FileManager :files="filteredFiles" @delete-file="fileStore.deleteFile" @update-file="fileStore.updateFile" />
      </div>
      <div v-else class="no-files">
        <p>Файлы не найдены</p>
      </div>
    </div>
  </div>
</template>

<script>
import { defineComponent, inject, computed, ref } from 'vue'
import { useFileStore } from '../stores/files'
import { useAuthStore } from '../stores/auth'
import { log_msg, log_error } from '../utils/debugging'
import FileManager from './FileManager.vue'

export default defineComponent({
  name: 'RightPanel',
  components: { FileManager },
  data() {
    return {
      projects: [],
      selectedProject: '',
      newProject: {
        project_name: '',
        description: '',
        local_git: '',
        public_git: '',
        dependencies: ''
      },
      editProject: {
        project_id: null,
        project_name: '',
        description: '',
        local_git: '',
        public_git: '',
        dependencies: ''
      },
      searchSettings: {
        mode: 'off',
        sources: ['web', 'x', 'news'],
        max_search_results: 20
      }
    }
  },
  setup() {
    const fileStore = useFileStore()
    const authStore = useAuthStore()
    const mitt = inject('mitt')
    const isCollapsed = ref(false)
    return { fileStore, authStore, mitt, isCollapsed }
  },
  computed: {
    filteredFiles() {
      // log_msg('FILE', 'Computing filteredFiles, fileStore.files:', JSON.stringify(this.fileStore.files, null, 2), 'selectedProject:', this.selectedProject)
      if (!this.selectedProject) {
        return this.fileStore.files          
          .map(file => ({
            ...file,
            file_name: file.file_name.startsWith('/') ? file.file_name.slice(1) : file.file_name
          }))
      }
      const projectId = parseInt(this.selectedProject)
      return this.fileStore.files
        .filter(file => file.project_id > 0 && file.project_id === projectId)
        .map(file => ({
          ...file,
          file_name: file.file_name.startsWith('/') ? file.file_name.slice(1) : file.file_name
        }))
    }
  },
  mounted() {
    this.fetchProjects()
    this.loadProjectFiles()
    this.loadSearchSettings()
    this.mitt.on('files-updated', () => {
      clearTimeout(this.debounceLoadFiles)
      this.debounceLoadFiles = setTimeout(() => this.loadProjectFiles(), 100)
    })
  },
  beforeUnmount() {
    this.mitt.off('files-updated')
    clearTimeout(this.debounceLoadFiles)
  },
  methods: {
    async fetchProjects() {
      try {
        log_msg('FILE', 'Fetching projects:', this.fileStore.apiUrl + '/project/list')
        const res = await fetch(this.fileStore.apiUrl + '/project/list', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.ok) {
          this.projects = (await res.json()).filter(project => project.id > 0) // Исключаем project_id=0 (.chat-meta)
          if (this.selectedProject === '0') this.selectedProject = '' // Сбрасываем выбор .chat-meta
          log_msg('FILE', 'Fetched projects:', JSON.stringify(this.projects, null, 2))
        } else {
          log_error(this, new Error('Failed to fetch projects'), 'fetch projects')
        }
      } catch (e) {
        log_error(this, e, 'fetch projects')
      }
    },
    async loadProjectFiles() {
      try {
        const project_id = this.selectedProject ? parseInt(this.selectedProject) : null
        log_msg('FILE', 'Loading files for project_id:', project_id)
        await this.fileStore.fetchFiles(project_id)
      } catch (e) {
        log_error(this, e, 'load files')
      }
    },
    async selectProject() {
      try {
        const project_id = this.selectedProject ? parseInt(this.selectedProject) : null
        log_msg('ACTION', 'Selecting project:', this.fileStore.apiUrl + '/project/select', 'ProjectId:', project_id)
        this.fileStore.setSelectedProject(project_id)
        await fetch(this.fileStore.apiUrl + '/project/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_id }),
          credentials: 'include'
        })
        await this.loadProjectFiles()
      } catch (e) {
        log_error(this, e, 'select project')
      }
    },
    async loadSearchSettings() {
      try {
        log_msg('UI', 'Loading search settings:', this.fileStore.apiUrl + '/user/settings')
        const res = await fetch(this.fileStore.apiUrl + '/user/settings', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.ok) {
          const settings = await res.json()
          this.searchSettings = {
            mode: settings.mode || 'off',
            sources: Array.isArray(settings.sources) ? settings.sources : ['web', 'x', 'news'],
            max_search_results: settings.max_search_results || 20
          }
          log_msg('UI', 'Loaded search settings:', this.searchSettings)
        } else {
          log_error(this, new Error('Failed to load search settings'), 'load search settings')
          this.searchSettings = { mode: 'off', sources: ['web', 'x', 'news'], max_search_results: 20 }
        }
      } catch (e) {
        log_error(this, e, 'load search settings')
        this.searchSettings = { mode: 'off', sources: ['web', 'x', 'news'], max_search_results: 20 }
      }
    },
    async saveSearchSettings() {
      try {
        log_msg('ACTION', 'Saving search settings:', this.searchSettings)
        const res = await fetch(this.fileStore.apiUrl + '/user/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.searchSettings),
          credentials: 'include'
        })
        if (res.ok) {
          log_msg('UI', 'Saved search settings:', this.searchSettings)
        } else {
          log_error(this, new Error('Failed to save search settings'), 'save search settings')
        }
      } catch (e) {
        log_error(this, e, 'save search settings')
      }
    },
    openCreateProjectModal() {
      this.newProject = { project_name: '', description: '', local_git: '', public_git: '', dependencies: '' }
      log_msg('ACTION', 'Opening create project modal')
      this.$refs.createProjectModal.showModal()
    },
    closeCreateProjectModal() {
      log_msg('ACTION', 'Closing create project modal')
      this.$refs.createProjectModal.close()
    },
    async createProject() {
      try {
        log_msg('ACTION', 'Creating project:', this.newProject)
        const res = await fetch(this.fileStore.apiUrl + '/project/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.newProject),
          credentials: 'include'
        })
        if (res.ok) {
          const data = await res.json()
          this.selectedProject = data.project_id
          this.fileStore.setSelectedProject(data.project_id)
          await this.fetchProjects()
          await this.loadProjectFiles()
          this.closeCreateProjectModal()
          log_msg('UI', 'Created project:', data)
        } else {
          log_error(this, new Error('Failed to create project'), 'create project')
        }
      } catch (e) {
        log_error(this, e, 'create project')
      }
    },
    openEditProjectModal() {
      const project = this.projects.find(p => p.id === parseInt(this.selectedProject))
      if (project) {
        this.editProject = { ...project }
        log_msg('ACTION', 'Opening edit project modal')
        this.$refs.editProjectModal.showModal()
      }
    },
    closeEditProjectModal() {
      log_msg('ACTION', 'Closing edit project modal')
      this.$refs.editProjectModal.close()
    },
    async updateProject() {
      try {
        log_msg('ACTION', 'Updating project:', this.editProject)
        const res = await fetch(this.fileStore.apiUrl + '/project/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.editProject),
          credentials: 'include'
        })
        if (res.ok) {
          await this.fetchProjects()
          await this.loadProjectFiles()
          this.closeEditProjectModal()
          log_msg('UI', 'Updated project:', this.editProject)
        } else {
          log_error(this, new Error('Failed to update project'), 'update project')
        }
      } catch (e) {
        log_error(this, e, 'update project')
      }
    },
    toggleCollapse() {
      this.isCollapsed = !this.isCollapsed
      log_msg('UI', 'Right panel collapsed:', this.isCollapsed)
    },
    toggleDisplaySS() {
      this.isDisplayedSS = !this.isDisplayedSS
    }
  }
})
</script>

<style>

.right-panel {
  display: flex;
  width: 330px;
  height: 100vh;
  padding: 10px;
  background: #333;
  transition: width 0.3s;
  overflow: revert;  
  flex-direction: column;
}

.right-panel.collapsed {
  width: 30px;
}
@media (prefers-color-scheme: light) {
  .right-panel {
    background: #f0f0f0;
  }
}
.right-panel .toggle-btn {
  position: absolute;
  top: 10px;
  right: 10px;
  background: #444;
  color: #eee;
  border: none;
  cursor: pointer;
  padding: 5px;
}
@media (prefers-color-scheme: light) {
  .right-panel .toggle-btn {
    background: #d0d0d0;
    color: #333;
  }
}
.panel-content {
  height: 98vh;
}

.right-panel .panel-content {
  display: flex;
  flex-direction: column;
}
.right-panel.collapsed .panel-content {
  display: none;
}

 
.right-panel select, .right-panel button:not(.toggle-btn) {
  display: inline-block;
  width: 120pt;
  margin: 10px 0;
  padding: 5px;
}
.right-panel input:not([type="checkbox"]), .right-panel textarea {
  width: calc(100% - 30px);
  margin: 10px;
  padding: 5px;
  border: 1px solid #ccc;
  border-radius: 3px;
  background: #444;
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .right-panel input:not([type="checkbox"]), .right-panel textarea {
    background: #fff;
    color: #333;
    border: 1px solid #999;
  }
}
.right-panel dialog {
  padding: 20px;
  border: 1px solid #ccc;
  border-radius: 5px;
}
.right-panel dialog input {
  width: 100%;
  margin-bottom: 10px;
}
.file-tree {
  flex: 1 1 auto;
  margin-top: 10px;
  color: #eee;  
  overflow: auto;    
}
@media (prefers-color-scheme: light) {
  .file-tree {
    color: #333;
  }
}
.no-files {
  margin-top: 10px;
  text-align: center;
  color: #aaa;
}
@media (prefers-color-scheme: light) {
  .no-files {
    color: #666;
  }
}
.search-settings {
  margin-top: 20px;
  flex: 0 0 auto;
}
.search-settings h3 {
  display: block;
  margin-top: 10px;
  color: #eee;
}
.search-settings label {
  display: block;
  margin-top: 10px;
  color: #ccccaa;
}
@media (prefers-color-scheme: light) {
  .search-settings h3, .search-settings label {
    color: #333;
  }
}

.search-settings.collapsed {
  height: 30px;
  overflow: hidden;
  position: relative;
}

.search-settings select {
  width: 90pt;
  padding: 5px;
}

.search-settings input[type="number"] {
  width: 40px;
  padding: 5px;
}
.search-settings .sources {
  width: 80%;
  color: #cccc01;
}
.search-settings .sources td {
  width: 33.33%;
  text-align: center;
  padding: 5px;
}
.search-settings .sources input[type="checkbox"] {
  margin: 0 5px 0 0;
  width: auto;
  display: inline-block;
  vertical-align: middle;
}
.search-settings .sources label {
  margin: 0;
  color: #cccccc;
  display: inline;
}
@media (prefers-color-scheme: light) {
  .search-settings .sources {
    color: #333;
  }
  .search-settings .sources label {
    color: #333;
  }
}
</style>