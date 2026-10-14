// 取得したページ（JRA・JBIS・equibaseなど）の馬名が、画面の馬名と合っているかを確認する
// （別の馬のURLやデータを入力してしまった場合に、違う馬の情報が入力されないようにする）

// 表記ゆれ（空白・記号・全角半角・大文字小文字）をそろえて比較する
function normalizeHorseNameForCheck(name) {
    return (name || '').toString().normalize('NFKC')
        .replace(/[\s・.\-'’`()（）\[\]［］「」□○◇]/g, '').toLowerCase();
}

// 取得した馬名（data.name・data.name_en）が、画面の馬名（formNames）のどれかと一致しなければ
// エラーメッセージを返す。どちらかの馬名が分からない場合は確認しない（null を返す）
function checkFetchedHorseName(data, formNames) {
    const fetched = [data.name, data.name_en].filter(Boolean);
    const mine = (formNames || []).map(n => (n || '').toString().trim()).filter(Boolean);
    if (!fetched.length || !mine.length) return null;
    const mineKeys = mine.map(normalizeHorseNameForCheck);
    if (fetched.some(n => mineKeys.includes(normalizeHorseNameForCheck(n)))) return null;
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
