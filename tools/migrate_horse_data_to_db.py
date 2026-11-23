"""Horse_Data のスプレッドシートの内容を、データベース（Neon などの PostgreSQL・SQLite）にコピーする。

使い方（Keiba.log のフォルダで実行する。DATABASE_URL は環境変数か .env ファイルに書いておく）
  python tools/migrate_horse_data_to_db.py                 … コピーする（既にデータがある場合は何もしない）
  python tools/migrate_horse_data_to_db.py --force         … データベースの Horse_Data を消して、コピーし直す
  python tools/migrate_horse_data_to_db.py --export-csv 出力先のフォルダ
                                                           … データベースの内容をシートごとの CSV ファイルに書き出す

コピーした後は、アプリを起動し直すと Horse_Data をデータベースから読み書きするようになる。
それ以降にアプリで変更した内容はスプレッドシートには反映されないため、コピーし直す（--force）と、
データベースでの変更は消える点に注意する。
"""
import argparse
import csv
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, '.env'))
except ImportError:
    pass

import sheet_db  # noqa: E402

BOOK = 'Horse_Data'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
BATCH = 20   # 1 回の読み込みで取得するシートの数


def open_client():
    import gspread
    from google.oauth2.service_account import Credentials
    creds_env = os.environ.get('GOOGLE_CREDENTIALS')
    if creds_env:
        creds = Credentials.from_service_account_info(json.loads(creds_env), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(os.path.join(ROOT, 'credentials.json'), scopes=SCOPES)
    return gspread.authorize(creds)


def with_retry(func, *args, **kwargs):
    """Google Sheets の API の回数制限（429）にかかったら、少し待ってやり直す"""
    import gspread
    for wait in (10, 20, 40, 60, None):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if wait is None or getattr(e, 'code', None) != 429:
                raise
            print(f"  回数制限にかかったため {wait} 秒待ちます")
            time.sleep(wait)


def read_spreadsheet(book_name):
    """スプレッドシートの全シートの内容を読み込む → [(シート名, 列数, 行の一覧)]"""
    gc = open_client()
    sh = with_retry(gc.open, book_name)
    worksheets = with_retry(sh.worksheets)
    out = []
    for i in range(0, len(worksheets), BATCH):
        chunk = worksheets[i:i + BATCH]
        ranges = ["'" + ws.title.replace("'", "''") + "'" for ws in chunk]
        res = with_retry(sh.values_batch_get, ranges)
        for ws, vr in zip(chunk, res.get('valueRanges', [])):
            out.append((ws.title, ws.col_count, vr.get('values', [])))
            print(f"  読み込み：{ws.title}（{len(vr.get('values', []))}行）")
    return out


def normalized(values):
    """比べるための形（行の末尾の空のセルと、末尾の空の行を除く）"""
    rows = [sheet_db._trim('' if v is None else str(v) for v in r) for r in values]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def copy_to_db(store, book_name, force):
    if store.has_book(book_name) and not force:
        print(f"データベースには既に{book_name}のデータがあります。コピーし直す場合は --force を付けて実行してください。")
        return 1
    print(f"{book_name}を読み込みます")
    sheets = read_spreadsheet(book_name)
    print("データベースに書き込みます")
    with store.engine.begin() as conn:
        store.delete_book(conn, book_name)
        for pos, (title, cols, values) in enumerate(sheets):
            sheet_id = store.create_tab(conn, book_name, title, max(cols, max((len(r) for r in values), default=0)),
                                        position=pos)
            store.insert_rows(conn, sheet_id, values)
    store.forget_tabs(book_name)

    # 書き込んだ内容が元のシートと同じか確かめる
    book = store.book(book_name)
    ng = 0
    total = 0
    for title, _, values in sheets:
        got = normalized(book.worksheet(title).get_all_values())
        want = normalized(values)
        total += len(want)
        if got != want:
            ng += 1
            print(f"  ×{title}：内容が一致しません（元 {len(want)}行・データベース {len(got)}行）")
    if ng:
        print(f"{ng}枚のシートで内容が一致しませんでした。もう一度 --force を付けて実行してください。")
        return 1
    print(f"完了：{len(sheets)}枚のシート・{total}行をコピーしました（内容が元のシートと一致することを確認しました）。")
    print("アプリを起動し直すと、Horse_Data をデータベースから読み書きします。")
    return 0


def export_csv(store, book_name, out_dir):
    book = store.book(book_name)
    sheets = book.worksheets()
    if not sheets:
        print(f"データベースに{book_name}のデータがありません。")
        return 1
    os.makedirs(out_dir, exist_ok=True)
    for ws in sheets:
        safe = ''.join('_' if c in '\\/:*?"<>|' else c for c in ws.title)
        path = os.path.join(out_dir, f"{safe}.csv")
        values = ws.get_all_values()
        # Excel で文字化けしないよう、BOM 付きの UTF-8 で書き出す
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerows(values if values != [[]] else [])
        print(f"  {path}（{len(values) if values != [[]] else 0}行）")
    print(f"完了：{len(sheets)}枚のシートを書き出しました。")
    return 0


def main():
    parser = argparse.ArgumentParser(description='Horse_Data のスプレッドシートをデータベースにコピーする')
    parser.add_argument('--force', action='store_true', help='データベースの内容を消して、コピーし直す')
    parser.add_argument('--export-csv', metavar='フォルダ', help='データベースの内容を CSV ファイルに書き出す')
    parser.add_argument('--book', default=BOOK, help=f'対象のファイル名（既定：{BOOK}）')
    args = parser.parse_args()

    url = os.environ.get('DATABASE_URL', '').strip()
    if not url:
        print("環境変数 DATABASE_URL が設定されていません。.env ファイルに DATABASE_URL=接続文字列 を書いてください。")
        return 1
    store = sheet_db.SheetStore(url)
    if args.export_csv:
        return export_csv(store, args.book, args.export_csv)
    return copy_to_db(store, args.book, args.force)


if __name__ == '__main__':
    sys.exit(main())
