<!-- /frontend/rtm/src/components/FileManager.vue, updated 2025-07-16 12:55 EEST -->
<template>
  <div class="file-manager">
    <h3>Файлы проекта</h3>
    <div v-if="buildFileTrees && buildFileTrees.length">
      <FileTree :trees="buildFileTrees" :level="0" />
    </div>
    <div v-else>
      <p>Файлы не найдены</p>
    </div>
  </div>
</template>

<script>
  import { defineComponent, inject } from 'vue'
  import { log_msg, log_error } from '../utils/debugging'
  import FileTree from './FileTree.vue'  
  

  export default defineComponent({
    name: 'FileManager',
    components: { FileTree },
    props: {
      files: Array
    },
    emits: ['delete-file', 'update-file'],
    setup() {
      const mitt = inject('mitt')
      return { mitt }
    },
    computed: {
      buildFileTrees() {
        const files = this.files;
        const trees = [];
        const projectMap = new Map();
        const globalFiles = { project_name: null, project_id: null, nodes: {} };

        files.forEach(file => {
          log_msg('FILE', 'Processing file: %s, project %s', file.file_name, file.project_id);
          const parts = file.file_name.split('/');
          const projectId = file.project_id;
          const root = projectId ? `project_${projectId}` : 'global';
          let current = projectId ? projectMap.get(projectId) || { project_name: parts[0], project_id: projectId, nodes: {} } : globalFiles;

          let node = current.nodes;
          let parent = null
          let path = '';
          for (let i = 0; i < parts.length - 1; i++) {
            const part = parts[i];
            path += part + '/';
            if (!node[part]) {
              node[part] = { type: 'directory', children: {}, parent: parent, path: path };
            }
            parent = node;
            node = node[part].children;
          }
          const fileName = parts[parts.length - 1];
          node[fileName] = { type: 'file', id: file.id, user_id: file.user_id, parent: node, path: path + fileName };

          if (projectId) {
            projectMap.set(projectId, current);
          }
        });

        projectMap.forEach(project => trees.push(project));
        if (Object.keys(globalFiles.nodes).length > 0) {
          trees.push(globalFiles);
        }

        return trees;
      }    
    },  // computed properties
    methods: {
                
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