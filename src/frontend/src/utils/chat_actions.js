/* /frontend/rtm/src/utils/chat_actions.js, updated 2025-07-26 12:37 EEST */
import { log_msg, log_error } from './debugging'
import { lang_class } from './chat_format'

export function handleModal(component, modalRef, open, stateUpdates = {}, callback = null) {
  log_msg('ACTION', `Received modalRef: ${modalRef}`)
  const modal = component.$refs[modalRef] || document.getElementById(modalRef)
  if (modal) {
    if (open) {
      modal.showModal()
    } else {
      modal.close()
    }
    Object.assign(component, stateUpdates)
    if (callback) callback(component)
    log_msg('ACTION', `Action executed: ${open ? 'Open' : 'Close'} modal ${modalRef}`)
  } else {
    log_error(component, new Error(`Modal ${modalRef} not found`), `toggle ${open ? 'open' : 'close'} modal ${modalRef}`)
    component.chatStore.chatError = `Failed to ${open ? 'open' : 'close'} modal ${modalRef}`
  }
}

export async function sendMessage(component, event) {
  log_msg('ACTION', 'Triggered sendMessage')
  if (event.shiftKey) return
  if (!component.newMessage && !component.fileStore.pendingAttachment) return
  log_msg('CHAT', 'Checking chatStore.status:', component.chatStore.status)
  if (!component.chatStore.status || typeof component.chatStore.status.status !== 'string') {
    component.chatStore.status = { status: 'free', actor: null, elapsed: 0 }
    log_msg('CHAT', 'Initialized chatStore.status to default')
  }
  if (component.chatStore.status.status === 'busy') {
    log_msg('CHAT', 'Отправка заблокирована: идёт обработка запроса', component.chatStore.status)
    component.chatStore.chatError = 'Отправка заблокирована: идёт обработка запроса'
    component.debugLogs.push({
      type: 'warn',
      message: `Отправка заблокирована: идёт обработка запроса ${component.chatStore.status.actor || 'unknown'} (${component.chatStore.status.elapsed || 0} секунд)`,
      timestamp: new Date().toTimeString().split(' ')[0] + `.${new Date().getMilliseconds().toString().padStart(3, '0')}`
    })
    return
  }
  let finalMessage = component.newMessage.trim().replace(/@attach#(\d+)/g, '@attached_file#$1')
  if (component.fileStore.pendingAttachment && component.fileStore.pendingAttachment.file_id) {
    finalMessage += ` @attached_file#${component.fileStore.pendingAttachment.file_id}`
  }
  try {
    component.last_post = finalMessage
    component.newMessage = ''
    log_msg('CHAT', `Saved message to last_post: ${finalMessage}`)
    await component.chatStore.sendMessage(finalMessage)
    component.fileStore.clearAttachment()
    component.chatStore.status.status = 'free'
    log_msg('CHAT', 'Reset status to free after action: send message')
    component.$nextTick(() => {
      component.autoResize({ target: component.$refs.messageInput }, 'messageInput')
    })
    log_msg('ACTION', 'Action executed: send message')
  } catch (error) {
    log_error(component, error, 'send message')
    component.chatStore.chatError = `Failed to send message: ${error.message}`
  }
}

export async function editPost(component) {
  log_msg('ACTION', 'Triggered editPost')
  if (!component.editMessageId || !component.editMessageContent) return
  log_msg('CHAT', 'Checking chatStore.status:', component.chatStore.status)
  if (!component.chatStore.status || typeof component.chatStore.status.status !== 'string') {
    component.chatStore.status = { status: 'free', actor: null, elapsed: 0 }
    log_msg('CHAT', 'Initialized chatStore.status to default')
  }
  if (component.chatStore.status.status === 'busy') {
    log_msg('CHAT', 'Редактирование заблокировано: идёт обработка запроса', component.chatStore.status)
    component.chatStore.chatError = 'Редактирование заблокирована: идёт обработка запроса'
    component.debugLogs.push({
      type: 'warn',
      message: `Редактирование заблокировано: идёт обработка запроса ${component.chatStore.status.actor || 'unknown'} (${component.chatStore.status.elapsed || 0} секунд)`,
      timestamp: new Date().toTimeString().split(' ')[0] + `.${new Date().getMilliseconds().toString().padStart(3, '0')}`
    })
    return
  }
  try {
    const finalMessage = component.editMessageContent.replace(/@attach#(\d+)/g, '@attached_file#$1')
    log_msg('CHAT', `Attempting to edit post: ${finalMessage}`)
    await component.chatStore.editPost(component.editMessageId, finalMessage)
    handleModal(component, 'editPostModal', false, { editMessageId: null, editMessageContent: '' })
    component.chatStore.status.status = 'free'
    log_msg('CHAT', 'Reset status to free after action: edit post')
    log_msg('ACTION', 'Action executed: edit post')
  } catch (error) {
    log_error(component, error, 'edit post')
    component.chatStore.chatError = `Failed to edit post: ${error.message}`
  }
}

export async function confirmFileUpload(component) {
  log_msg('ACTION', 'Triggered confirmFileUpload')
  if (!component.pendingFile || !component.pendingFileName) return
  try {
    const response = await component.fileStore.uploadFile(component.pendingFile, component.pendingFileName, component.chatStore.selectedChatId)
    log_msg('FILE', 'Upload response:', JSON.stringify(response))
    if (response && response.status === 'ok' && response.file_id) {
      component.newMessage += ` @attached_file#${response.file_id}`
      component.fileStore.pendingAttachment = { file_id: response.file_id, file_name: component.pendingFileName }
    } else {
      log_error(component, new Error('Invalid upload response'), 'upload file')
      component.fileStore.chatError = 'Failed to upload file: Invalid response'
    }
    handleModal(component, 'fileConfirmModal', false, { pendingFile: null, pendingFileName: '' })
    log_msg('ACTION', 'Action executed: confirm file upload')
  } catch (error) {
    log_error(component, error, 'upload file')
    component.fileStore.chatError = `Failed to upload file: ${error.message}`
  }
}

export async function showFilePreview(component, fileId) {
  log_msg('ACTION', 'Triggered showFilePreview')
  try {
    const res = await fetch(`${component.chatStore.apiUrl}/chat/file_contents?file_id=${fileId}`, {
      method: 'GET',
      credentials: 'include'
    })
    log_msg('FILE', `Fetching file contents for file_id: ${fileId}`)
    if (res.ok) {
      const text = await res.text()
      if (text) {
        component.filePreviewContent = text
        handleModal(component, 'filePreviewModal', true)      
        const codeElement = document.getElementById('file-preview-code')
        if (codeElement && window.hljs) {
          let lc = lang_class(component.fileStore.files, fileId)                  
          codeElement.className = `framed-code ${lc}`
          codeElement.removeAttribute('data-highlighted')
          window.hljs.highlightElement(codeElement)
          log_msg('UI', `Highlighted code preview for file_id: ${fileId} with class: ${lc}`)
        }

        log_msg('ACTION', 'Action executed: show file preview')
      } else {
        log_error(component, new Error(`No content found for file_id: ${fileId}`), 'fetch file contents')
      }
    } else {
      const errorData = await res.json()
      log_error(component, new Error(errorData.error || 'Invalid response'), 'fetch file contents')
    }
  } catch (error) {
    log_error(component, error, 'fetch file contents')
  }
}

export async function insertTextAtCursor(component, text) {
  let cursorPos = 0
  const textarea = component.$refs.messageInput
    if (textarea) {
      const msg = component.newMessage || ''
      cursorPos = textarea.selectionStart || 0
      const textBefore = msg.substring(0, cursorPos)
      const textAfter = msg.substring(cursorPos)
      component.newMessage = `${textBefore} ${text} ${textAfter}`      
      textarea.value = component.newMessage
      component.$nextTick(() => {
        component.autoResize({ target: textarea }, 'messageInput')
      })
      
      textarea.focus()      
      cursorPos = textBefore.length + text.length + 2 // +2 for the spaces added      
      textarea.setSelectionRange(cursorPos, cursorPos)      
    } else {
      component.newMessage += ` ${text} `
      component.$refs.messageInput?.focus()
    }
    log_msg('ACTION', 'completed insertTextAtCursor %d: %s', cursorPos, text)
}

export function handleSelectFile(component, fileId) {
  log_msg('ACTION', 'Triggered handleSelectFile')
  if (!fileId) {
    log_error(component, new Error('Invalid fileId received'), 'select file')
    component.fileStore.chatError = 'Invalid file selection'
    return
  }
  insertTextAtCursor(component, `@attached_file#${fileId}`)
}

export function handleSelectDir(component, dirPath) {
  log_msg('ACTION', 'Triggered handleSelectDir')
  if (!dirPath) {
    log_error(component, new Error('Invalid dirPath received'), 'select dir')
    component.fileStore.chatError = 'Invalid directory selection'
    return
  }
  insertTextAtCursor(component, `@attach_dir#${dirPath}`)
}