(() => {
    'use strict';
    const content = document.getElementById('notification-live-content');
    const status = document.getElementById('notification-refresh-status');
    if (!content || !status) return;
    const labels = {pending: 'в очереди', sending: 'отправляется', sent: 'отправлено', failed: 'ошибка'};
    let timer;
    let busy = false;
    let stopped = false;
    let previousHtml;
    const schedule = () => {
        clearTimeout(timer);
        if (!stopped && !document.hidden) timer = setTimeout(refresh, 5000);
    };
    async function refresh() {
        if (busy || stopped || document.hidden) return;
        busy = true;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        try {
            const response = await fetch(content.dataset.statusUrl, {
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                headers: {'Accept': 'application/json'}
            });
            if (response.redirected || response.status === 401 || response.status === 403) {
                stopped = true;
                status.textContent = 'Сессия завершена. Войдите в админку заново для обновления статусов.';
                return;
            }
            if (!response.ok) throw new Error('Status update unavailable');
            const data = await response.json();
            if (typeof data.html !== 'string') throw new Error('Invalid status response');
            // Do not replace a focused retry button while the user is acting on it.
            if (data.html !== previousHtml && !content.contains(document.activeElement)) {
                const scroll = content.querySelector('.referral-scroll');
                const left = scroll ? scroll.scrollLeft : 0;
                content.innerHTML = data.html;
                const updatedScroll = content.querySelector('.referral-scroll');
                if (updatedScroll) updatedScroll.scrollLeft = left;
                previousHtml = data.html;
            }
            document.querySelectorAll('[data-withdrawal-notification]').forEach(link => {
                const value = (data.withdrawal_statuses || {})[link.dataset.withdrawalNotification];
                if (labels[value]) link.textContent = `Уведомление: ${labels[value]}`;
            });
            status.textContent = 'Статусы обновляются автоматически каждые 5 секунд.';
        } catch (_) {
            status.textContent = 'Не удалось обновить статусы. Повторяем автоматически; показаны последние полученные данные.';
        } finally {
            clearTimeout(timeout);
            busy = false;
            schedule();
        }
    }
    document.addEventListener('visibilitychange', () => {
        clearTimeout(timer);
        if (!document.hidden) refresh();
    });
    schedule();
})();
