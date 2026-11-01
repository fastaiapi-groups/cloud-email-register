/* Session enforcement for the main dashboard and its same-origin panels. */
(() => {
  const send = window.fetch.bind(window);
  let metadata;
  function login() { window.top.location.assign('/login'); }
  async function session() {
    if (!metadata) metadata = send('/api/auth/session', {cache: 'no-store'}).then(async r => {
      if (r.status === 401) { login(); throw new Error('请重新登录'); }
      if (!r.ok) throw new Error('无法验证管理会话');
      return r.json();
    }).catch(e => { metadata = null; throw e; });
    return metadata;
  }
  window.fetch = async (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (url.origin === location.origin && url.pathname.startsWith('/api/') && !url.pathname.startsWith('/api/public/')) {
      if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
        const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
        headers.set('X-CSRF-Token', (await session()).csrf_token);
        init = {...init, headers};
      }
      const response = await send(input, init);
      if (response.status === 401) login();
      if (method === 'POST' && /^\/api\/accounts\/[^/]+\/(validate|cookies)$/.test(url.pathname)
          && [200, 400, 503].includes(response.status)) {
        document.getElementById('autoFrame')?.contentWindow?.postMessage(
          {type: 'icloud-account-status-changed'}, location.origin);
      }
      return response;
    }
    return send(input, init);
  };
  document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) {
      const link = document.createElement('a');
      link.href = '/admin/security'; link.className = 'nav-item'; link.textContent = '管理账号与 API Key';
      sidebar.append(link);
    }
  });
})();
