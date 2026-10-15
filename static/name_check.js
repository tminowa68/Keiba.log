// 取得したページ（JRA・JBIS・equibaseなど）の馬名が、画面の馬名と合っているかを確認する
// （別の馬のURLやデータを入力してしまった場合に、違う馬の情報が入力されないようにする）

// 表記ゆれ（空白・記号・全角半角・大文字小文字）をそろえて比較する
// 馬名の後ろのⅡ・Ⅲ・2・3など（同名馬を区別するもの。JBISは「ルパンⅡ」、登録は「ルパン2」など）は取り除いて比較する
function normalizeHorseNameForCheck(name) {
    return (name || '').toString()
        .replace(/[\u2160-\u216F]+\s*$/, '')        // Ⅰ〜Ⅿ（ローマ数字の記号）
        .replace(/\s+(I{1,3}|IV|V|VI{1,3}|IX|X)\s*$/, '') // 「 II」「 III」のような英字のローマ数字
        .normalize('NFKC')
        .replace(/(\D)\d{1,2}\s*$/, '$1')            // 「ルパン2」「ルパン２」のような後ろの数字
        .replace(/[\s・.\-'’`()（）\[\]［］「」□○◇]/g, '').toLowerCase();
}

// 英字の馬名か（和名と英馬名は表記が違うため、同じ表記どうしでだけ比較する）
function isEnglishHorseName(name) {
    const n = (name || '').toString().normalize('NFKC');
    return /[A-Za-z]/.test(n) && !/[\u3040-\u30ff\u3400-\u9fff]/.test(n);
}

// 取得した馬名（data.name・data.name_en）が、画面の馬名（formNames）のどれかと一致しなければ
// エラーメッセージを返す。どちらかの馬名が分からない場合や、和名と英字のように表記が違って
// 比べられない場合は確認しない（null を返す）
function checkFetchedHorseName(data, formNames) {
    const fetched = [data.name, data.name_en].filter(Boolean);
    const mine = (formNames || []).map(n => (n || '').toString().trim()).filter(Boolean);
    if (!fetched.length || !mine.length) return null;
    const comparable = fetched.filter(f => mine.some(m => isEnglishHorseName(m) === isEnglishHorseName(f)));
    if (!comparable.length) return null;
    const mineKeys = mine.map(normalizeHorseNameForCheck);
    if (comparable.some(n => mineKeys.includes(normalizeHorseNameForCheck(n)))) return null;
    return `取得した馬（${fetched.join(' / ')}）が馬名（${mine.join(' / ')}）と一致しないため、入力を取り消しました。`
        + 'URLまたはデータを確認してください。';
}

// 一致しなかった時に、入力されていたURL・データの欄を空にする
function clearFetchInput(input) {
    if (!input) return;
    input.value = '';
    if (typeof autoResizeTextarea === 'function') autoResizeTextarea(input);
    input.focus();
}
