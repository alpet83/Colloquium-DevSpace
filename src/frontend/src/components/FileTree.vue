<!-- /frontend/rtm/src/components/FileTree.vue, updated 2025-08-19 -->
 
<template>  
  <div v-for="(tree, index) in trees" :key="index">
    <ul>
      <li v-for="(node, name) in tree.nodes" :key="name" :style="{ 'margin-left': level * 20 + 'px' }">
        <div v-if="node.type === 'directory'" class="directory">
          <span class="toggle" @click="toggleExpand(name, index)">
            {{ expanded[index] && expanded[index][name] ? '▼' : '▶' }}  
          </span>
          <span  @click="selectDir(node, name)">{{ name }}</span>
        </div>
        <div v-else class="file">
          <span @click="selectFile(node.id)">{{ name }}</span>
          <span v-if="authStore.userRole === 'admin' || authStore.userId === node.user_id" class="delete-file" @click="deleteFile(node.id)">[x]</span>
        </div>
        <FileTree v-if="node.type === 'directory' && expanded[index] && expanded[index][name]"
                  :trees="[{ nodes: node.children }]"
                  :level="level + 1"
                  @select-dir="$emit('select-dir', $event)"
                  @select-file="$emit('select-file', $event)"
                  @delete-file="$emit('delete-file', $event)" />
      </li>
    </ul>
  </div> 
</template>

<script>
import { defineComponent, reactive, inject } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useFileStore } from '../stores/files'
import { log_msg, log_error } from '../utils/debugging'

export default defineComponent({
  name: 'FileTree',
  props: {
    trees: Array,
    level: {
      type: Number,
      default: 0
    }
  },
  emits: ['select-dir', 'select-file', 'delete-file'],
  setup() {
    const authStore = useAuthStore()
    const fileStore = useFileStore()
    const expanded = reactive([])
    const mitt = inject('mitt')      
    return { authStore, fileStore, expanded, mitt }
  },
  methods: {
    toggleExpand(nodeName, treeIndex) {
      if (!this.expanded[treeIndex]) {
        this.expanded[treeIndex] = reactive({})
      }
      this.expanded[treeIndex][nodeName] = !this.expanded[treeIndex][nodeName]
      log_msg('FILE', 'Toggled expand for: %s, Tree %s, New state: %s', nodeName, treeIndex, this.expanded[treeIndex][nodeName])
    }, 
    selectDir(node, name) {            
      const dirPath = node.path
      log_msg('FILE', 'Selecting directory: %s, path: %s', name, dirPath)
      this.mitt.emit('select-dir', dirPath)
    },
    selectFile(fileId) {
      log_msg('FILE', 'Selecting file: %s', fileId)
      this.mitt.emit('select-file', fileId)        
    },
    deleteFile(fileId) {
      log_msg('FILE', 'Deleting file: %s', fileId);
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
.file-tree-container {  
  padding: 10px;  
}

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
  color: #ff0000;
  cursor: pointer;
  font-size: 12px;
}
.delete-file:hover {
  color: #cc0000;
}
</style>