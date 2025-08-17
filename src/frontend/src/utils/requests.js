// /frontend/rtm/src/utils/requests.js, created 2025-07-26 18:00 EEST
import { log_msg, log_error } from './debugging'

export function makeRequest(store, endpoint, options = {}, requestKey, minDelay) {
  const now = Date.now();
  const lastRequestTime = store.lastRequestTimes.get(requestKey) || 0;
  if (minDelay && now - lastRequestTime < minDelay) {
    log_msg('REQUEST', `Request ${requestKey} throttled, waiting ${minDelay - (now - lastRequestTime)}ms`);
    return new Promise(resolve => setTimeout(() => resolve(makeRequest(store, endpoint, options, requestKey, minDelay)), minDelay - (now - lastRequestTime)));
  }

  if (store.pendingRequests.has(requestKey)) {
    log_msg('REQUEST', `Request ${requestKey} already pending, awaiting result`);
    return store.pendingRequests.get(requestKey);
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => {
    controller.abort();
    log_error(null, new Error(`Request ${requestKey} timeout after 25s`), 'make request');
    store.chatError = `Request ${endpoint} timeout`;
  }, 25000);

  const promise = new Promise(async (resolve, reject) => {
    try {
      log_msg('REQUEST', `Fetching ${endpoint}:`, options);
      const res = await fetch(`${store.apiUrl}${endpoint}`, {
        ...options,
        credentials: 'include',
        signal: controller.signal
      });
      clearTimeout(timeoutId);
      if (res.status === 500 || res.status === 502) {
        log_error(null, new Error(`Server error: ${res.status}`), `fetch ${endpoint}`);
        store.backendError = true;
        reject(new Error(`Server error: ${res.status}`));
        return;
      }
      const data = await res.json();
      if (res.ok && !data.error) {
        store.backendError = false;
        store.chatError = '';
        resolve(data);
      } else {
        log_error(null, new Error(data.error || `Failed to fetch ${endpoint}`), `fetch ${endpoint}`);
        store.chatError = data.error || `Failed to fetch ${endpoint}`;
        reject(new Error(data.error || `Failed to fetch ${endpoint}`));
      }
    } catch (e) {
      clearTimeout(timeoutId);
      if (e.name === 'AbortError') {
        log_error(null, e, `fetch ${endpoint} aborted due to timeout`);
        store.chatError = `Fetch ${endpoint} timeout`;
      } else {
        log_error(null, e, `fetch ${endpoint}`);
        store.chatError = `Failed to fetch ${endpoint}`;
      }
      reject(e);
    } finally {
      store.pendingRequests.delete(requestKey);
      store.lastRequestTimes.set(requestKey, Date.now());
    }
  });

  store.pendingRequests.set(requestKey, promise);
  return promise;
}