# /frontend/rtm/src/components/FileTree.vue, created 2025-07-16 12:55 EEST
<template>
  <ul>
    <li v-for="(node, name) in tree" :key="name" :style="{ 'margin-left': level * 20 + 'px' }">
      <div v-if="node.type === 'directory'" class="directory">
        <span class="toggle" @click="toggleExpand(name)">
          {{ expanded[name] ? '▼' : '▶' }}
        </span>
        <span>{{ name }}</span>
      </div>
      <div v-else class="file" @click="selectFile(node.id)">
        {{ name }}
      </div>
      <FileTree v-if="node.type === 'directory' && expanded[name]"
                :tree="node.children"
                :level="level + 1"
                @select-file="$emit('select-file', $event)" />
    </li>
  </ul>
</template>

<script>
import { defineComponent, reactive } from 'vue'

export default defineComponent({
  name: 'FileTree',
  props: {
    tree: Object,
    level: Number
  },
  emits: ['select-file'],
  setup() {
    const expanded = reactive({})
    return { expanded }
  },
  methods: {
    toggleExpand(nodeName) {
      this.expanded[nodeName] = !this.expanded[nodeName]
      console.log('Toggled expand for:', nodeName, 'New state:', this.expanded[nodeName])
    },
    selectFile(fileId) {
      console.log('Selecting file:', fileId)
      this.$emit('select-file', fileId)
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
  cursor: pointer;
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
</style>