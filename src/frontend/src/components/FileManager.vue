<template>
  <div>
    <h3>Управление файлами</h3>
    <table>
      <thead>
        <tr>
          <th>Имя файла</th>
          <th>Дата загрузки</th>
          <th>Действия</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="file in files" :key="file.id">
          <td>{{ file.file_name }}</td>
          <td>{{ new Date(file.ts * 1000).toLocaleString() }}</td>
          <td>
            <input type="file" @change="$emit('update-file', file.id, $event)" />
            <button @click="$emit('delete-file', file.id)">Удалить</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script>
export default {
  name: 'FileManager',
  props: {
    files: Array
  },
  emits: ['delete-file', 'update-file']
}
</script>

<style>
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  border: 1px solid #ccc;
  padding: 8px;
  text-align: left;
}
th {
  background: #f0f0f0;
}
@media (prefers-color-scheme: dark) {
  th {
    background: #333;
  }
  td {
    color: #fff;
  }
}
</style>
