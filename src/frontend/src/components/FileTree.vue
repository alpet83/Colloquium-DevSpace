<!-- /frontend/rtm/src/components/FileTree.vue, updated 2025-07-17 22:45 EEST -->
<template>
  <ul>
    <li v-for="(node, name) in tree" :key="name" :style="{ 'margin-left': level * 20 + 'px' }">
      <div v-if="node.type === 'directory'" class="directory">
        <span class="toggle" @click="toggleExpand(name)">
          {{ expanded[name] ? '▼' : '▶' }}
        </span>
        <span>{{ name }}</span>
      </div>
      <div v-else class="file">
        <span @click="selectFile(node.id)">{{ name }}</span>
        <span v-if="authStore.userRole === 'admin' || authStore.userId === node.user_id" class="delete-file" @click="deleteFile(node.id)">[x]</span>
      </div>
      <FileTree v-if="node.type === 'directory' && expanded[name]"
                :tree="node.children"
                :level="level + 1"
                @select-file="$emit('select-file', $event)"
                @delete-file="$emit('delete-file', $event)" />
    </li>
  </ul>
</template>

<script>
import { defineComponent, reactive } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useFileStore } from '../stores/files'

export default defineComponent({
  name: 'FileTree',
  props: {
    tree: Object,
    level: Number
  },
  emits: ['select-file', 'delete-file'],
  setup() {
    const authStore = useAuthStore()
    const fileStore = useFileStore()
    const expanded = reactive({})
    return { authStore, fileStore, expanded }
  },
  methods: {
    toggleExpand(nodeName) {
      this.expanded[nodeName] = !this.expanded[nodeName]
      console.log('Toggled expand for:', nodeName, 'New state:', this.expanded[nodeName])
    },
    selectFile(fileId) {
      console.log('Selecting file:', fileId)
      this.$emit('select-file', fileId)
    },
    deleteFile(fileId) {
      console.log('Deleting file:', fileId)
      this.fileStore.deleteFile(fileId).then(() => {
        this.$emit('delete-file', fileId)
      }).catch(error => {
        console.error('Error deleting file:', error)
        this.fileStore.chatError = `Failed to delete file: ${error.message}`
      })
    }
  }
})
</script>

<style>
ul {
  list-style: none;
  padding: 0;
}
li {
  padding: 5px 0;
}
.directory {
  cursor: pointer;
  display: flex;
  align-items: center;
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .directory {
    color: #333;
  }
}
.toggle {
  width: 20px;
  text-align: center;
}
.file {
  display: inline-flex;
  align-items: center;
  color: #eee;
}
@media (prefers-color-scheme: light) {
  .file {
    color: #333;
  }
}
.file:hover {
  background: #444;
}
@media (prefers-color-scheme: light) {
  .file:hover {
    background: #e0e0e0;
  }
}
.delete-file {
  margin-left: 8px;
  color: #ff0000; /* Красный шрифт для текста [x] */
  cursor: pointer;
  font-size: 12px;
}
.delete-file:hover {
  color: #cc0000; /* Темнее при наведении */
}
</style>