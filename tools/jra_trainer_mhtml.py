"""
JRAの調教師「管理馬一覧」ページを全厩舎分MHTMLで保存し、keiba.logの「管理馬一覧インポート」にアップロードする。

手順（ブラウザで手作業していた操作をそのまま自動で行う）
  1. 競馬メニュー（https://www.jra.go.jp/keiba/）から「騎手・調教師データ」を開く
  2. 「プロフィール」（trainer.html）を開く
  3. 「あ行」〜「わ行」を順番に開き、各行の調教師のページを順番に開く
  4. 調教師のページから「管理馬一覧」を開き、MHTML形式で保存する
       保存先：<保存フォルダ>\\YYYYMM\\YYYYMMDD\\ア〜ワ\\アイザワ イクオ.mhtml（フォルダが無ければ作成）
  5. 保存したファイルを keiba.log の /import_trainer_horses に順番にアップロードする

準備（最初の1回だけ。コマンドプロンプトで実行）
  pip install playwright requests
  playwright install chromium

※ keiba.log の「データ更新」画面の「JRAから管理馬一覧を取り込む」ボタンでも、ファイルを保存せずに
  同じ取り込みができます。このスクリプトは、管理馬一覧のページをファイルとして残したい場合に使います。

使い方（このファイルは tools フォルダにあるため、リポジトリのフォルダで実行する場合は
「py tools/jra_trainer_mhtml.py --upload-only」のようにフォルダ名を付ける）
  python jra_trainer_mhtml.py                 保存してアップロードする
  python jra_trainer_mhtml.py --no-upload     保存だけする
  python jra_trainer_mhtml.py --upload-only   今日のフォルダに保存済みのファイルをアップロードだけする
  python jra_trainer_mhtml.py --rows ア カ    指定した行だけ処理する
  python jra_trainer_mhtml.py --show          ブラウザの画面を表示しながら動かす

keiba.logのURL・ユーザー名・パスワードは、環境変数 KEIBA_URL / KEIBA_USER / KEIBA_PASSWORD に
設定しておくと入力を省けます（設定が無ければ実行時に聞きます。パスワードは保存しません）。
"""
import argparse
import getpass
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_BASE_DIR = r"C:\Users\tomdr\OneDrive\keiba.log\mhtml"
JRA_MENU_URL = os.environ.get("JRA_MENU_URL", "https://www.jra.go.jp/keiba/")

# 「あ行」〜「わ行」のリンク名と、保存先のフォルダ名
ROWS = [("あ行", "ア"), ("か行", "カ"), ("さ行", "サ"), ("た行", "タ"), ("な行", "ナ"),
        ("は行", "ハ"), ("ま行", "マ"), ("や行", "ヤ"), ("ら行", "ラ"), ("わ行", "ワ")]

# JRAのサーバーに負担をかけないよう、ページを開くたびに少し待つ（秒）
WAIT_SECONDS = 1.0

_DO_ACTION_RE = re.compile(r"doAction\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")


def log(message):
    print(message, flush=True)


def today_dir(base_dir):
    now = datetime.now()
    return Path(base_dir) / now.strftime("%Y%m") / now.strftime("%Y%m%d")


def safe_filename(text):
    return re.sub(r'[\\/:*?"<>|]', "_", text).strip() or "noname"


def do_action(page, onclick):
    """リンクの onclick="return doAction('/JRADB/accessC.html', 'pw05...')" と同じ操作でページを開く"""
    m = _DO_ACTION_RE.search(onclick or "")
    if not m:
        raise ValueError(f"リンクを読み取れませんでした: {onclick}")
    with page.expect_navigation(wait_until="load"):
        page.evaluate("([url, cname]) => doAction(url, cname)", [m.group(1), m.group(2)])
    time.sleep(WAIT_SECONDS)


def open_href(page, href):
    """<a href="..."> のリンク先を開く。同じリンクが開いていないメニューの中（非表示）にもあるため、
    クリックではなくリンク先のアドレスを直接開く"""
    link = page.locator(f"a[href='{href}']").first
    target = link.evaluate("a => a.href") if link.count() else None
    if not target:
        raise RuntimeError(f"リンク（{href}）が見つかりませんでした。")
    page.goto(target, wait_until="load")
    time.sleep(WAIT_SECONDS)


def link_onclick(page, text):
    """表示文字が text のリンクの onclick を返す（無ければ None）"""
    for a in page.locator("a[onclick*='doAction']").all():
        if a.inner_text().strip() == text:
            return a.get_attribute("onclick")
    return None


def trainer_kana(page):
    """<span class="txt"><span class="opt">調教師情報</span>相沢 郁<span class="kana">（アイザワ イクオ）</span></span>
    からカタカナの読み（アイザワ イクオ）を取り出す"""
    el = page.locator("span.txt span.kana").first
    if not el.count():
        return ""
    return re.sub(r"^[（(]\s*|\s*[）)]$", "", el.inner_text().strip()).strip()


def save_mhtml(page, path):
    """表示中のページをMHTML形式で保存する（Chromeの「ウェブページ、1ファイルのみ」と同じ形式）"""
    session = page.context.new_cdp_session(page)
    data = session.send("Page.captureSnapshot", {"format": "mhtml"})["data"]
    session.detach()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(data)


def save_all_trainer_pages(base_dir, row_folders, show=False):
    """全調教師の管理馬一覧ページを保存し、保存したファイルの一覧を返す"""
    from playwright.sync_api import sync_playwright

    out_dir = today_dir(base_dir)
    saved, skipped = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not show)
        page = browser.new_page()

        # ① 競馬メニュー →「騎手・調教師データ」 ② →「プロフィール」（調教師）
        log("JRAの競馬メニューを開いています...")
        page.goto(JRA_MENU_URL, wait_until="load")
        open_href(page, "/datafile/meikan/")
        open_href(page, "trainer.html")
        trainer_index_url = page.url

        for row_label, folder in ROWS:
            if folder not in row_folders:
                continue
            # ③ 「あ行」〜「わ行」
            page.goto(trainer_index_url, wait_until="load")
            onclick = link_onclick(page, row_label)
            if not onclick:
                log(f"【{row_label}】リンクが見つからないため飛ばします")
                continue
            do_action(page, onclick)

            # 調教師の一覧（<span class="icon"><span class="east">美浦</span></span><span class="txt">相沢 郁</span>）
            # 調教師のページへのリンク（cnameが pw05cmk で始まるもの）だけを対象にする
            trainers = []
            for a in page.locator("a[onclick*='pw05cmk']").all():
                name_el = a.locator("span.txt")
                if name_el.count() == 0:
                    continue
                area_el = a.locator("span.icon span")
                area = area_el.first.inner_text().strip() if area_el.count() else ""
                trainers.append((area, name_el.first.inner_text().strip(), a.get_attribute("onclick")))
            log(f"【{row_label}】{len(trainers)}人")

            for area, name, trainer_onclick in trainers:
                label = f"{area}・{name}" if area else name
                try:
                    # ④ 調教師のページ →「管理馬一覧」
                    do_action(page, trainer_onclick)
                    horses_onclick = link_onclick(page, "管理馬一覧")
                    if not horses_onclick:
                        skipped.append(f"{label}（管理馬一覧のリンクがありません）")
                        log(f"  {label}：管理馬一覧のリンクが無いため飛ばします")
                        continue
                    do_action(page, horses_onclick)
                    # ファイル名は調教師名のカタカナ（<span class="kana">（アイザワ イクオ）</span>）
                    kana = trainer_kana(page) or (f"{area}_{name}" if area else name)
                    path = out_dir / folder / f"{safe_filename(kana)}.mhtml"
                    if path in saved:
                        path = out_dir / folder / f"{safe_filename(kana + '_' + area)}.mhtml"
                    save_mhtml(page, path)
                    saved.append(path)
                    log(f"  {label}：保存しました")
                except Exception as e:
                    skipped.append(f"{label}（{e}）")
                    log(f"  {label}：失敗しました（{e}）")
        browser.close()

    log(f"保存：{len(saved)}件／飛ばした調教師：{len(skipped)}人（保存先：{out_dir}）")
    for s in skipped:
        log(f"  - {s}")
    return saved


def login(base_url, username, password):
    import requests

    s = requests.Session()
    log("keiba.logにログインしています（サーバーが起動するまで時間がかかることがあります）...")
    r = s.post(f"{base_url}/login", data={"username": username, "password": password, "next_url": ""},
               timeout=180)
    if "/login" in r.url:
        raise SystemExit("keiba.logにログインできませんでした。ユーザー名・パスワードを確認してください。")
    return s


def flash_messages(html):
    """import_horses の画面に表示されたメッセージを取り出す"""
    msgs = re.findall(r'<div style="color: green;[^"]*">(.*?)</div>', html, re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m)).strip() for m in msgs]


def upload_files(base_url, username, password, files):
    session = login(base_url, username, password)
    for i, path in enumerate(files, start=1):
        log(f"[{i}/{len(files)}] {path.parent.name}\\{path.name} をアップロードしています...")
        with open(path, "rb") as f:
            r = session.post(f"{base_url}/import_trainer_horses",
                             files={"trainer_file": (path.name, f, "multipart/related")}, timeout=600)
        if "/login" in r.url:
            session = login(base_url, username, password)
            log("  ログインし直しました。もう一度アップロードします")
            with open(path, "rb") as f:
                r = session.post(f"{base_url}/import_trainer_horses",
                                 files={"trainer_file": (path.name, f, "multipart/related")}, timeout=600)
        if r.status_code >= 400:
            log(f"  失敗しました（HTTP {r.status_code}）")
            continue
        for m in flash_messages(r.text) or ["（結果のメッセージがありませんでした）"]:
            log(f"  {m}")


def main():
    parser = argparse.ArgumentParser(description="JRAの管理馬一覧を保存してkeiba.logにアップロードする")
    parser.add_argument("--base", default=DEFAULT_BASE_DIR, help="保存先のフォルダ")
    parser.add_argument("--rows", nargs="*", default=[f for _, f in ROWS], help="処理する行（ア カ サ …）")
    parser.add_argument("--no-upload", action="store_true", help="保存だけしてアップロードしない")
    parser.add_argument("--upload-only", action="store_true", help="今日のフォルダの保存済みファイルをアップロードだけする")
    parser.add_argument("--show", action="store_true", help="ブラウザの画面を表示する")
    args = parser.parse_args()
    row_folders = set(args.rows)

    if args.upload_only:
        out_dir = today_dir(args.base)
        files = [p for _, folder in ROWS if folder in row_folders
                 for p in sorted((out_dir / folder).glob("*.mhtml"))]
        if not files:
            raise SystemExit(f"{out_dir} に保存済みのファイルがありません。")
    else:
        files = save_all_trainer_pages(args.base, row_folders, show=args.show)

    if args.no_upload or not files:
        return
    base_url = (os.environ.get("KEIBA_URL") or input("keiba.logのURL（例：https://xxxx.onrender.com）：")).rstrip("/")
    username = os.environ.get("KEIBA_USER") or input("ユーザー名：")
    password = os.environ.get("KEIBA_PASSWORD") or getpass.getpass("パスワード：")
    upload_files(base_url, username, password, files)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("中断しました。")
