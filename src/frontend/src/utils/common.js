//frontend/rtm/src/utils/common.js, created 2025-07-27 12:00 EEST
export function formatDateTime(timestamp) {
  return new Date(timestamp * 1000).toLocaleString(undefined, {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}