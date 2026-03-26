<!-- /frontend/rtm/src/components/FileManager.vue, updated 2025-07-16 12:55 EEST -->
<template>
  <div class="file-manager">
    <h3>Файлы проекта</h3>
    <div v-if="trees && trees.length">
      <div v-for="tree in trees" :key="`${tree.project_id ?? 'global'}:${tree.path || ''}`" class="tree-group">
        <h4 v-if="showProjectTitle(tree)" class="tree-title">{{ tree.project_name }}</h4>
        <FileTree :trees="[tree]" :level="0" />
      </div>
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
      trees: {
        type: Array,
        default: () => []
      }
    },
    emits: ['delete-file', 'update-file'],
    setup() {
      const mitt = inject('mitt')
      return { mitt }
    },
    methods: {
      showProjectTitle(tree) {
        return (this.trees?.length || 0) > 1 && !!tree?.project_name
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
  .tree-title {
    margin: 10px 0 4px;
    color: #bbb;
    font-size: 13px;
    text-transform: uppercase;
  }
  @media (prefers-color-scheme: light) {
    .file-manager h3 {
      color: #333;
    }
    .tree-title {
      color: #666;
    }
  }
</style>