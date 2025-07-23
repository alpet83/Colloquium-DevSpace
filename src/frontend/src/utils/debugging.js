// /frontend/rtm/src/utils/debugging.js, updated 2025-07-22 17:15 EEST
export let show_logs = JSON.parse(localStorage.getItem('show_logs')) || ['CHAT', 'FILE', 'ACTION', 'ERROR', 'UI'];

export function log_msg(filter_code, msg, ...args) {
  if (show_logs.includes(filter_code)) {
    const now = new Date();
    const timeStr = now.toTimeString().split(' ')[0] + `.${now.getMilliseconds().toString().padStart(3, '0')}`;
    console.log(`[${timeStr}] #${filter_code}: ${msg}`, ...args);
  }
}

export function log_error(component, error, message, filter_code = 'ERROR') {
  if (show_logs.includes(filter_code)) {
    const now = new Date();
    const timeStr = now.toTimeString().split(' ')[0] + `.${now.getMilliseconds().toString().padStart(3, '0')}`;
    console.error(`[${timeStr}] #${filter_code}: Error ${message}:`, error);
    if (component && component.debugLogs) {
      component.debugLogs.push({
        type: 'error',
        message: `Failed to ${message}: ${error.message}`,
        timestamp: timeStr
      });
    } else {
      console.warn(`[${timeStr}] #${filter_code}: Attempted to log error without debugLogs`);
    }
  }
}

export function set_show_logs(filter_codes) {
  show_logs = filter_codes.split(',').map(code => code.trim()).filter(code => code);
  localStorage.setItem('show_logs', JSON.stringify(show_logs));
  log_msg('UI', 'Updated log filters:', show_logs);
}