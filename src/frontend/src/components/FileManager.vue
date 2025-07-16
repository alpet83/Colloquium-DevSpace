# /frontend/rtm/src/components/FileManager.vue, updated 2025-07-16 12:55 EEST
<template>
  <div class="file-manager">
    <h3>Файлы проекта</h3>
    <div v-if="fileTree && Object.keys(fileTree).length">
      <FileTree :tree="fileTree" :level="0" @select-file="handleSelectFile" />
    </div>
    <div v-else>
      <p>Файлы не найдены</p>
    </div>
  </div>
</template>

<script>
import { defineComponent, inject } from 'vue'
import FileTree from './FileTree.vue'

export default defineComponent({
  name: 'FileManager',
  components: { FileTree },
  props: {
    files: Array
  },
  emits: ['delete-file', 'update-file', 'select-file'],
  setup() {
    const mitt = inject('mitt')
    return { mitt }
  },
  computed: {
    fileTree() {
      console.log('Building fileTree from files:', JSON.stringify(this.files, null, 2))
      const tree = {}
      this.files.forEach(file => {
        // Удаляем ведущий слэш и нормализуем путь
        const normalizedPath = file.file_name.startsWith('/') ? file.file_name.slice(1) : file.file_name
        const parts = normalizedPath.split('/').filter(part => part)
        let current = tree
        parts.forEach((part, index) => {
          if (index === parts.length - 1) {
            current[part] = { type: 'file', id: file.id, ts: file.ts, project_id: file.project_id }
          } else {
            if (!current[part]) {
              current[part] = { type: 'directory', children: {}, expanded: false }
            }
            current = current[part].children
          }
        })
      })
      console.log('Built fileTree:', JSON.stringify(tree, null, 2))
      return tree
    }
  },
  methods: {
    handleSelectFile(fileId) {
      console.log('Emitting select-file:', fileId)
      this.mitt.emit('select-file', fileId)
      this.$emit('select-file', fileId)
    }
  }
})
</script>

<style>
.file-manager {
  width: 100%;
  padding: 10px;
  background: #333;
}
@media (prefers-color-scheme: light) {
  .file-manager {
    background: #f0f0f0;
  }
}
.file-manager h3 {
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .file-manager h3 {
    color: #333;
  }
}
</style>