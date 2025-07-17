<!-- /frontend/rtm/src/components/RightPanel.vue, updated 2025-07-17 22:35 EEST -->
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
      <div class="search-settings">
        <h3>Настройки поиска</h3>
        <label>Режим поиска:</label>
        <select v-model="searchSettings.mode">
          <option value="off">Отключён</option>
          <option value="auto">Авто</option>
          <option value="on">Включён</option>
        </select>
        <label>Источники:</label>
        <table class="sources" width="100%">
          <tr>
            <td><input type="checkbox" v-model="searchSettings.sources" value="web" /> Web</td>
            <td><input type="checkbox" v-model="searchSettings.sources" value="x" /> X</td>
            <td><input type="checkbox" v-model="searchSettings.sources" value="news" /> News</td>
          </tr>
        </table>
        <label>Макс. источников:</label>
        <input type="number" v-model="searchSettings.max_search_results" min="1" max="50" />
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
      console.log('Computing filteredFiles, fileStore.files:', JSON.stringify(this.fileStore.files, null, 2), 'selectedProject:', this.selectedProject)
      if (!this.selectedProject) {
        return this.fileStore.files.map(file => ({
          ...file,
          file_name: file.file_name.startsWith('/') ? file.file_name.slice(1) : file.file_name
        }))
      }
      const projectId = parseInt(this.selectedProject)
      return this.fileStore.files
        .filter(file => file.project_id === projectId)
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
  },
  methods: {
    async fetchProjects() {
      try {
        const res = await fetch(this.fileStore.apiUrl + '/project/list', {
          method: 'GET',
          credentials: 'include'
        })
        if (res.ok) {
          this.projects = await res.json()
          console.log('Fetched projects:', JSON.stringify(this.projects, null, 2))
        } else {
          console.error('Error fetching projects:', await res.json())
        }
      } catch (e) {
        console.error('Error fetching projects:', e)
      }
    },
    async loadProjectFiles() {
      try {
        const url = this.selectedProject
          ? `${this.fileStore.apiUrl}/chat/list_files?project_id=${this.selectedProject}`
          : `${this.fileStore.apiUrl}/chat/list_files`
        console.log('Loading files from:', url)
        const res = await fetch(url, {
          method: 'GET',
          credentials: 'include'
        })
        if (res.ok) {
          this.fileStore.files = await res.json()
          console.log('Loaded files:', JSON.stringify(this.fileStore.files, null, 2))
        } else {
          console.error('Error loading files:', await res.json())
        }
      } catch (e) {
        console.error('Error loading files:', e)
      }
    },
    async selectProject() {
      try {
        const project_id = this.selectedProject ? parseInt(this.selectedProject) : null
        console.log('Selecting project:', this.fileStore.apiUrl + '/project/select', 'ProjectId:', project_id)
        await fetch(this.fileStore.apiUrl + '/project/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_id }),
          credentials: 'include'
        })
        await this.loadProjectFiles()
      } catch (e) {
        console.error('Error selecting project:', e)
      }
    },
    async loadSearchSettings() {
      try {
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
          console.log('Loaded search settings:', this.searchSettings)
        } else {
          console.error('Error loading search settings:', await res.json())
          this.searchSettings = { mode: 'off', sources: ['web', 'x', 'news'], max_search_results: 20 }
        }
      } catch (e) {
        console.error('Error loading search settings:', e)
        this.searchSettings = { mode: 'off', sources: ['web', 'x', 'news'], max_search_results: 20 }
      }
    },
    async saveSearchSettings() {
      try {
        const res = await fetch(this.fileStore.apiUrl + '/user/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.searchSettings),
          credentials: 'include'
        })
        if (res.ok) {
          console.log('Saved search settings:', this.searchSettings)
        } else {
          console.error('Error saving search settings:', await res.json())
        }
      } catch (e) {
        console.error('Error saving search settings:', e)
      }
    },
    openCreateProjectModal() {
      this.newProject = { project_name: '', description: '', local_git: '', public_git: '', dependencies: '' }
      this.$refs.createProjectModal.showModal()
    },
    closeCreateProjectModal() {
      this.$refs.createProjectModal.close()
    },
    async createProject() {
      try {
        const res = await fetch(this.fileStore.apiUrl + '/project/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.newProject),
          credentials: 'include'
        })
        if (res.ok) {
          const data = await res.json()
          this.selectedProject = data.project_id
          await this.fetchProjects()
          await this.loadProjectFiles()
          this.closeCreateProjectModal()
        } else {
          console.error('Error creating project:', await res.json())
        }
      } catch (e) {
        console.error('Error creating project:', e)
      }
    },
    openEditProjectModal() {
      const project = this.projects.find(p => p.id === parseInt(this.selectedProject))
      if (project) {
        this.editProject = { ...project }
        this.$refs.editProjectModal.showModal()
      }
    },
    closeEditProjectModal() {
      this.$refs.editProjectModal.close()
    },
    async updateProject() {
      try {
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
        } else {
          console.error('Error updating project:', await res.json())
        }
      } catch (e) {
        console.error('Error updating project:', e)
      }
    },
    toggleCollapse() {
      this.isCollapsed = !this.isCollapsed
      console.log('Right panel collapsed:', this.isCollapsed)
    }
  }
})
</script>

<style>
.right-panel {
  width: 300px;
  padding: 10px;
  background: #333;
  transition: width 0.3s;
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
.right-panel .panel-content {
  display: flex;
  flex-direction: column;
}
.right-panel.collapsed .panel-content {
  display: none;
}
.right-panel select, .right-panel button:not(.toggle-btn) {
  display: block;
  width: 100%;
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
  margin-top: 10px;
  color: #eee;
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
}
.search-settings h3 {
  display: block;
  margin-top: 10px;
  color: #eee;
}
.search-settings label {
  display: block;
  margin-top: 10px;
  color: #ccccaa; /* Нежный жёлтый для тёмной темы */
}
@media (prefers-color-scheme: light) {
  .search-settings h3, .search-settings label {
    color: #333;
  }
}
.search-settings select, .search-settings input[type="number"] {
  width: 100%;
  padding: 5px;
}
.search-settings .sources {
  width: 100%;
  color: #cccc01; /* Нежный жёлтый для тёмной темы */
}
.search-settings .sources td {
  width: 33.33%; /* Равномерное распределение столбцов */
  text-align: center; /* Центрирование содержимого */
  padding: 5px;
}
.search-settings .sources input[type="checkbox"] {
  margin: 0 5px 0 0;
  width: auto; /* Отменяем calc(100% - 30px) для чекбоксов */
  vertical-align: middle;
}
.search-settings .sources label {
  margin: 0;
  color: #cccccc; /* Цвет меток в тёмной теме */
  display: inline; /* Для корректного выравнивания с чекбоксом */
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