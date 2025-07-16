# /frontend/rtm/src/components/RightPanel.vue, updated 2025-07-16 15:55 EEST
<template>
  <div class="right-panel" :class="{ collapsed: isCollapsed }">
    <button class="toggle-button" @click="toggleCollapse">
      {{ isCollapsed ? '▶' : '◄' }}
    </button>
    <div v-if="!isCollapsed" class="panel-content">
      <button @click="openCreateProjectModal">Создать проект</button>
      <button v-if="selectedProject" @click="openEditProjectModal">Редактировать проект</button>
      <select v-model="selectedProject" @change="loadProjectFiles">
        <option value="">Все файлы</option>
        <option v-for="project in projects" :value="project.id" :key="project.id">{{ project.project_name }}</option>
      </select>
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
      }
    }
  },
  setup() {
    const fileStore = useFileStore()
    const mitt = inject('mitt')
    const isCollapsed = ref(false)
    return { fileStore, mitt, isCollapsed }
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
.right-panel .toggle-button {
  width: 100%;
  padding: 5px;
  text-align: center;
}
.right-panel .panel-content {
  display: flex;
  flex-direction: column;
}
.right-panel.collapsed .panel-content {
  display: none;
}
.right-panel select, .right-panel button:not(.toggle-button) {
  display: block;
  width: 100%;
  margin: 10px 0;
  padding: 5px;
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
</style>