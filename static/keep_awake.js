// 取り込み・登録などの処理中（このアプリのAPIへの通信中）は、パソコンの画面がスリープしないようにする
// （ブラウザのWake Lockを使う。最後の通信が終わってから少し待って解除し、通信の合間に解除されないようにする）。
// ふたを閉じた場合や、タブを切り替え・最小化している間は効かない（タブに戻れば掛け直す）
(function() {
    if (!('wakeLock' in navigator) || !window.fetch) return;
    const RELEASE_DELAY = 20000;
    let active = 0, lock = null, releaseTimer = null, badge = null;

    function showBadge(show) {
        if (show && !badge) {
            badge = document.createElement('div');
            badge.className = 'keep-awake-badge';
            badge.textContent = 'スリープ防止中';
            document.body.appendChild(badge);
        } else if (!show && badge) {
            badge.remove();
            badge = null;
        }
    }

    async function acquire() {
        if (lock || document.visibilityState !== 'visible') return;
        try {
            lock = await navigator.wakeLock.request('screen');
            lock.addEventListener('release', () => { lock = null; showBadge(false); });
            showBadge(true);
        } catch (e) {
            lock = null;   // 電池残量が少ない場合などは取得できない
        }
    }

    function release() {
        if (lock) lock.release().catch(() => {});
        lock = null;
        showBadge(false);
    }

    function hold() {
        active++;
        clearTimeout(releaseTimer);
        acquire();
    }

    function unhold() {
        active = Math.max(0, active - 1);
        if (!active) {
            clearTimeout(releaseTimer);
            releaseTimer = setTimeout(() => { if (!active) release(); }, RELEASE_DELAY);
        }
    }

    // タブに戻った時、処理中なら掛け直す
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && (active || releaseTimer)) acquire();
    });

    const originalFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (!/^(\/|https?:\/\/[^/]*\/)api\//.test(url.replace(location.origin, ''))) {
            return originalFetch(input, init);
        }
        hold();
        return originalFetch(input, init).finally(unhold);
    };
    window.KeepAwake = { hold: hold, unhold: unhold };
})();
