(() => {
  let running = false;
  async function refresh() {
    const current = document.getElementById('finance-pepel-balance');
    if (!current || document.hidden || running) return;
    running = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(current.dataset.refreshUrl, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        redirect: 'error'
      });
      if (!response.ok) throw new Error('Refresh unavailable');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = page.getElementById('finance-pepel-balance');
      if (!next) throw new Error('Unexpected response');
      const value = next.querySelector('.finance-pepel-available-source').textContent;
      current.replaceWith(next);
      document.querySelectorAll('[data-pepel-available]').forEach(node => { node.textContent = value; });
    } catch (_) {
      const note = current.querySelector('[data-refresh-status]');
      if (note) note.textContent = 'Автообновление временно недоступно. Показан расчёт на указанное выше время.';
    } finally {
      clearTimeout(timeout);
      running = false;
    }
  }
  setInterval(refresh, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
