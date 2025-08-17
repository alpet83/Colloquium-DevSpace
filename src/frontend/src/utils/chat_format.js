//frontend/rtm/src/utils/chat_format.js, updated 2025-07-27 12:45 EEST
import { log_msg, log_error } from './debugging'
import { formatDateTime } from '../utils/common'

export function escapeHtml(text) {
  const map = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
  }
  return text.replace(/[&<>"']/g, char => map[char])
}

export function int_attr(attrs, name, defaultValue = -1) {
  let pattern = new RegExp(name + '="(\\d+)"')
  return pattern.exec(attrs)?.[1] || defaultValue
}

export function text_attr(attrs, name, defaultValue = false) {
  let pattern = new RegExp(name + '="([^"]+)"')
  return pattern.exec(attrs)?.[1] || defaultValue
}

export function lang_class(files, fileId) {
    const file = files.find(f => f.id === parseInt(fileId))
    if (file) {
      const extension = file.file_name.match(/\.(\w+)$/)?.[1] || 'text'
      const as_is = ['json', 'html', 'css', 'java', 'cpp', 'c']      
      for (const ext of as_is) {
        if (extension === ext) return ` language-${ext}`
      }

      const langMap = { 'py': 'python', 'js': 'javascript', 'rs': 'rust', 'md': 'markdown', 'sh': 'bash' }
      let contentType = langMap[extension] || ''
      log_msg('UI', `Mapped file extension ${extension} to content type ${contentType}`)
      return contentType ? ` language-${contentType}` : ''
    }       
    return ''     
}

export function formatMessage(message, userName, timestamp, quotes, files, postId, context) {  
  let formatted = message || '[Post deleted]'
  // Replace @attach/@attached_file#ID with clickable file links
  formatted = formatted.replace(/@(attach|attached_file)#(\d+)/g, (match, type, fileId) => {
    const file = files.find(f => f.id === parseInt(fileId))
    if (file) {      
      return `<span class="file-link" data-file-id="${file.id}">File: ${file.file_name} ` +
             `(@attached_file#${file.id}, ${formatDateTime(file.ts)})</span>`
    }
    if (!(fileId in context.awaited_files)) {
      context.awaited_files[fileId] = 3
    }
    checkAwaitedFiles(context)
    return `<span class="file-unavailable">Файл ${fileId} удалён или недоступен</span>`
  })
  // Replace @quote#ID with formatted quote blocks
  if (quotes && typeof quotes === 'object') {
    Object.entries(quotes).forEach(([quoteId, quote]) => {
      if (!quote || !quote.message) return
      const regex = new RegExp(`@quote#${quoteId}\\b`, 'g')
      const quoteText = quote.message || '[Quote deleted]'
      const quoteUser = quote.user_name || 'unknown'
      log_msg('UI', `Processed quote#${quoteId}`)
      formatted = formatted.replace(regex,
                                   `<pre class="quote"><strong>${quoteUser}</strong> (${formatDateTime(quote.timestamp)}): ${quoteText}</pre>`)
    })
  }
  // Format code-related tags with HTML escaping for code content
  try {
    formatted = formatted.replace(
      /<(code_patch|shell_code|stdout|stderr|mismatch|traceback)((?:\s+[\w-]+="[^"]*")*)\s*>\s*([\s\S]*?)\s*<\/\1>/g,
      (match, tag, attributes, content) => {
        
        const escapedContent = escapeHtml(content)
        if (tag === 'code_patch') {
          const lines = escapedContent.split('\n').map(line => {
            if (line.startsWith('-') && !line.startsWith('---')) {
              return `<span class="patch-removed">${line}</span>`
            } else if (line.startsWith('+')) {
              return `<span class="patch-added">${line}</span>`
            } else {
              return `<span class="patch-unchanged">${line}</span>`
            }
          }).join('\n')
          // log_msg('UI', `Formatted code_patch tag`)
          return `<pre class="code-patch">${lines}</pre>`
        }
        // log_msg('UI', `Formatted ${tag} tag`)
        return `<pre class="${tag}">${escapedContent}</pre>`
      }
    )
  } catch (error) {
    log_error(context, error, `Failed to format tag`)
    return formatted
  }
  // Escape content in <td> tags within tables with class="code-lines"
  try {
    formatted = formatted.replace(
      /<table\s+class="code-lines"[^>]*>([\s\S]*?)<\/table>/g,
      (match, tableContent) => {
        const escapedTableContent = tableContent.replace(/<td>([\s\S]*?)<\/td>/g, (tdMatch, tdContent) => {
          const escapedTdContent = escapeHtml(tdContent)
          return `<td>${escapedTdContent}</td>`
        })
        //log_msg('UI', `Formatted code-lines table`)
        return `<table class="code-lines">${escapedTableContent}</table>`
      }
    )
  } catch (error) {
    log_error(context, error, `Failed to format code-lines table`)
    return formatted
  }
  // Format undo_file tag with file restoration info
  formatted = formatted.replace(
    /<undo_file\s+file_id="(\d+)"\s*\/>/g,
    (match, fileId) => {
      const file = files.find(f => f.id === parseInt(fileId))
      if (file) {        
        return `<span class="file-link" data-file-id="${file.id}">File: ${file.file_name} ` +
               `(@attached_file#${file.id}, ${formatDateTime(file.ts)})</span>`
      }
      if (!(fileId in context.awaited_files)) {
        context.awaited_files[fileId] = 3
      }
      checkAwaitedFiles(context)
      // log_msg('UI', `Processed undo_file tag for file_id: ${fileId}`)
      return `<span class="undo-file">восстановлен файл @attached_file#${fileId} (Файл ${fileId} недоступен)</span>`
    }
  )
  // Format replace tag with file replacement info
  formatted = formatted.replace(
    /<replace\s+file_id="(\d+)"\s+find="([^"]*)"(?:\s+to="([^"]*)")?\s*>/g,
    (match, fileId, pattern, replaceTo = '') => {
      const file = files.find(f => f.id === parseInt(fileId))
      if (file) {        
        return `<span class="file-link" data-file-id="${file.id}">File: ${file.file_name} ` +
               `(@attached_file#${file.id}, ${formatDateTime(file.ts)})</span>`
      }
      if (!(fileId in context.awaited_files)) {
        context.awaited_files[fileId] = 3
      }
      checkAwaitedFiles(context)
      const text = replaceTo
        ? `Замена в файле @attached_file#${fileId} (Файл ${fileId} недоступен) текста '${pattern}' на '${replaceTo}'`
        : `Удаление из файла @attached_file#${fileId} (Файл ${fileId} недоступен) текста '${pattern}'`
      
      return `<span class="replace">${text}</span>`
    }
  )
  // Format move_file tag with file move info
  formatted = formatted.replace(
    /<move_file\s+file_id="(\d+)"\s+new_name="([^"]*)"(?:\s+overwrite="(true|false)")?\s*\/>/g,
    (match, fileId, newName) => {
      const file = files.find(f => f.id === parseInt(fileId))
      if (file) {        
        return `<span class="file-link" data-file-id="${file.id}">File: ${file.file_name} ` +
               `(@attached_file#${file.id}, ${formatDateTime(file.ts)})</span>`
      }
      if (!(fileId in context.awaited_files)) {
        context.awaited_files[fileId] = 3
      }
      checkAwaitedFiles(context)      
      return `<span class="move-file">Перемещение файла @attached_file#${fileId} (Файл ${fileId} недоступен), ` +
             `новое имя ${newName}</span>`
    }
  )
  
  formatted = formatted.replace(/<(lookup_span|lookup_entity)\s+([^>]+)\/>/g, (match, tag, attrs) => {
      let fileId = int_attr(attrs, 'file_id')
      let hash = text_attr(attrs, 'hash')
      let name = text_attr(attrs, 'name')
      let start = int_attr(attrs, 'start')
      let end = int_attr(attrs, 'end')
      let formattedContent = ""
      let _class = tag.replace('_', '-')
      tag = tag.replace('_', ' ').toUpperCase()
      let result = `<pre class="${_class}">${tag} `
      if (fileId >= 0) {
        result += `<span class="file-link" data-file-id="${fileId}">@attached_file#${fileId}</span> `
      }
      if (hash) {
        result += `@span#${hash} `
      }
      if (start >= 0 && end >= 0) {
        result += `Lines ${start}-${end}: `
      }
      if (name) {
        result += `Name: ${name} `
      }
      result += `\n${formattedContent}</pre>`      
      return result    
    }
  )
  formatted = formatted.replace(/<(replace_span|project_scan)\s*([^>]*)>([\s\S]+)<\/\1>/g, (match, tag, attrs, content) => {
    let fileId = int_attr(attrs, 'file_id')
    let hash = text_attr(attrs, 'hash')
    let start = int_attr(attrs, 'start')
    let end = int_attr(attrs, 'end')
    let formattedContent = escapeHtml(content)
    let _class = tag.replace('_', '-')
    let result = `<pre class="${_class}">`
    result += tag.replace('_', ' ').toUpperCase()
    let classes = 'framed-code'

    if (tag === 'replace_span' && fileId >= 0) {
      classes += lang_class(files, fileId)
    }

    if (fileId >= 0) {
      result += `<span class="file-link" data-file-id="${fileId}">@attached_file#${fileId}</span> `
    }
    if (hash) {
      result += `@span#${hash} `
    }
    if (start >= 0 && end >= 0) {
      result += `Lines ${start}-${end}: `
    }
    result += `\n<code class="${classes}">${formattedContent}</code></pre>`
    log_msg('UI', `Formatted <${tag}> with file_id=${fileId || 'none'}, hash=${hash || 'none'}`)
    return result
  })

  return formatted
}

export function reformatMessages(context) {
  if (Object.keys(context.awaited_files).length === 0) return
  context.formattedMessages = Object.values(context.chatStore.history)
    .filter(post => post.action !== 'delete')
    .sort((a, b) => a.id - b.id)
    .map(msg => ({
      ...msg,
      formatted: formatMessage(msg.message, msg.user_name, msg.timestamp, context.chatStore.quotes,
                              context.fileStore.files, msg.id, context)
    }))
  Object.keys(context.awaited_files).forEach(fileId => {
    if (!context.fileStore.files.some(f => f.id === parseInt(fileId)) && context.awaited_files[fileId] > 0) {
      context.awaited_files[fileId]--
    }
  })
  log_msg('UI', `Reformatted messages, updated awaited_files: ${JSON.stringify(context.awaited_files)}`)
}

export function checkAwaitedFiles(context) {
  const activeFiles = Object.entries(context.awaited_files)
    .filter(([_, retries]) => retries > 0)
    .map(([fileId]) => fileId)
  if (activeFiles.length > 0) {
    context.fileStore.fetchFilesAndNotify(null, activeFiles)
    log_msg('UI', `Requested file list for awaited_files: ${activeFiles}`)
  }
}