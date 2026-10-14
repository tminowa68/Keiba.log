// レース結果の取り込み（import_horses.html の「登録馬の出走レースから取り込み」と horse_detail.html の取り込みボタンで共用）
(function() {
    async function post(url, body) {
        const res = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
        });
        let data;
        try {
            data = await res.json();
        } catch (e) {
            // JSONでない応答はログイン画面に戻された場合など
            throw new Error(res.redirected ? 'ログインし直してから、もう一度実行してください。'
                                           : `取り込みに失敗しました（HTTP ${res.status}）`);
        }
        if (!res.ok || data.error) throw new Error(data.error || `取り込みに失敗しました（HTTP ${res.status}）`);
        return data;
    }

    // 1頭分：JRAの競走馬情報のページの「出走レース」から、取り込まれていないレースを取り込む
    //   opts.wholeDay：同じ日（同じ競馬場）の全レースも取り込む
    //   opts.overwrite：取り込み済みのレースも取得し直す
    //   opts.doneDays：この実行で取り込んだ開催日（同じ開催日を何度も取得しないよう、複数の馬で共有する）
    //   opts.log(text)：進み具合の表示
    async function importHorse(horseUrl, opts) {
        opts = opts || {};
        const log = opts.log || function() {};
        const doneDays = opts.doneDays || new Set();
        const data = await post('/api/race_results/horse_races', { url: horseUrl });
        const races = data.races || [];
        const todo = opts.overwrite ? races : races.filter(r => !r.imported);
        const result = { races: races.length, todo: todo.length, saved: 0, failed: 0 };
        result.errors = [];
        // 1つの開催日（レース）で失敗しても、残りは続けて取り込む（続けても同じエラーになるものは中断）
        async function attempt(label, count, request) {
            try {
                const res = await request();
                result.saved += res.saved || 0;
                result.failed += (res.failed || []).length;
                return !(res.failed || []).length;
            } catch (e) {
                if (isFatalError(e.message)) throw e;
                result.failed += count;
                result.errors.push(`${label}：${e.message}`);
                return false;
            }
        }
        if (opts.wholeDay) {
            for (const r of todo) {
                if (doneDays.has(r.day_key)) continue;
                log(`${r.label} の開催日の全レースを取得しています...`);
                const ok = await attempt(r.label, 1, () =>
                    post('/api/race_results/import_day', { url: r.url, overwrite: !!opts.overwrite }));
                if (ok) doneDays.add(r.day_key);
            }
        } else {
            for (let i = 0; i < todo.length; i += 10) {
                const chunk = todo.slice(i, i + 10);
                log(`${chunk[0].label} ほか${chunk.length}レースを取得しています...`);
                await attempt(chunk[0].label, chunk.length, () =>
                    post('/api/race_results/import_races', { urls: chunk.map(r => r.url), overwrite: !!opts.overwrite }));
            }
        }
        return result;
    }

    // ファイルを作れない場合など、続けても同じエラーになるもの
    function isFatalError(message) {
        return /作成できませんでした|ログイン/.test(message || '');
    }

    window.RaceImport = { post: post, importHorse: importHorse, isFatalError: isFatalError };
})();
