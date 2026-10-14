// レース結果の取り込み（import_horses.html の「登録馬の出走レースから取り込み」と horse_detail.html の取り込みボタンで共用）
(function() {
    // Google SheetsのAPIの1分あたりの回数制限（429）にかかった場合は、30秒待ってからやり直す（3回まで）
    async function post(url, body) {
        for (let attempt = 0; ; attempt++) {
            try {
                return await postOnce(url, body);
            } catch (e) {
                if (attempt >= 3 || !/429|Quota exceeded/.test(e.message || '')) throw e;
                await new Promise(resolve => setTimeout(resolve, 30000));
            }
        }
    }

    async function postOnce(url, body) {
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

    // 未登録の馬（またはURLが無い馬）を、JRAの競走馬情報から登録する（親も未登録・情報不足なら登録する）
    //   items：[{name, url, known}]（url が無い馬は競走馬検索で探す）
    //   opts.log(text, isError)：進み具合と結果の表示
    async function registerHorses(items, opts) {
        opts = opts || {};
        const log = opts.log || function() {};
        const result = { registered: 0, skipped: 0, failed: 0, messages: [] };
        for (let i = 0; i < (items || []).length; i += 4) {
            const chunk = items.slice(i, i + 4);
            log(`未登録の馬を登録しています（${Math.min(i + chunk.length, items.length)}／${items.length}頭）：${chunk.map(x => x.name).join('、')}`);
            let res;
            try {
                res = await post('/api/auto_register', { items: chunk });
            } catch (e) {
                if (isFatalError(e.message)) throw e;
                result.failed += chunk.length;
                result.messages.push({ text: `${chunk.map(x => x.name).join('、')}：${e.message}`, error: true });
                continue;
            }
            (res.results || []).forEach(r => {
                if (r.ok) result.registered++;
                else if (/失敗/.test(r.message)) result.failed++;
                else result.skipped++;
                result.messages.push({ text: r.message, error: /失敗/.test(r.message) });
            });
        }
        return result;
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
        // recheck：取り込み済みのレースも取得して（保存はしない）、未登録の出走馬を探す
        const todo = (opts.overwrite || opts.recheck) ? races : races.filter(r => !r.imported);
        const result = { races: races.length, todo: todo.length, saved: 0, failed: 0 };
        result.errors = [];
        result.unregistered = [];
        // 1つの開催日（レース）で失敗しても、残りは続けて取り込む（続けても同じエラーになるものは中断）
        async function attempt(label, count, request) {
            try {
                const res = await request();
                (res.unregistered || []).forEach(u => {
                    if (!result.unregistered.some(x => x.name === u.name)) result.unregistered.push(u);
                });
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
                    post('/api/race_results/import_day', { url: r.url, overwrite: !!opts.overwrite, recheck: !!opts.recheck }));
                if (ok) doneDays.add(r.day_key);
            }
        } else {
            for (let i = 0; i < todo.length; i += 10) {
                const chunk = todo.slice(i, i + 10);
                log(`${chunk[0].label} ほか${chunk.length}レースを取得しています...`);
                await attempt(chunk[0].label, chunk.length, () =>
                    post('/api/race_results/import_races', { urls: chunk.map(r => r.url), overwrite: !!opts.overwrite,
                                                             recheck: !!opts.recheck }));
            }
        }
        // 出走馬のうち未登録の馬を登録する
        if (opts.register && result.unregistered.length) {
            const reg = await registerHorses(result.unregistered, { log: log });
            result.registered = reg.registered;
            result.registerFailed = reg.failed;
            result.registerMessages = reg.messages;
        }
        return result;
    }

    // ファイルを作れない場合など、続けても同じエラーになるもの
    function isFatalError(message) {
        return /作成できませんでした|ログイン/.test(message || '');
    }

    window.RaceImport = { post: post, importHorse: importHorse, registerHorses: registerHorses, isFatalError: isFatalError };
})();
