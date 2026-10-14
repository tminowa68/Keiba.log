import sqlite3
import re, os, time
import json
import requests
import email
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import race_results as RR

# --- gspread関連のインポート ---
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
app.secret_key = 'your_secret_key_here' # セッション用の秘密鍵
app.permanent_session_lifetime = timedelta(days=30)
UsersDB= 'users.db'
horse_data = 'Horse_Data'

# --- スプレッドシート認証設定 ---
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets', 
    'https://www.googleapis.com/auth/drive'
    ]
creds_env = os.environ.get('GOOGLE_CREDENTIALS')
if creds_env:
    # Render環境（環境変数から読み込み）
    creds_dict = json.loads(creds_env)
    CREDS = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
else:
    # ローカル開発環境（ファイルが存在すればファイルから読み込み）
    CREDS = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)

gc = gspread.authorize(CREDS)

# --- データベース初期化 ---
def add_new_user(username, password):
    with sqlite3.connect(UsersDB) as conn:
        cursor = conn.cursor()
        hashed_pw = generate_password_hash(password)
        try:
            cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
            conn.commit()
            print(f"ユーザー {username} を登録しました。")
        except sqlite3.IntegrityError:
            print("そのユーザー名は既に存在します。")

def delete_user(username):
    with sqlite3.connect(UsersDB) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        if cursor.rowcount > 0:
            print(f"ユーザー {username} を削除しました。")
        else:
            print(f"ユーザー {username} が見つかりませんでした。")

# --- 認証用デコレータ ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('login'):
            flash('この操作にはログインが必要です。')
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# ==============================================================================
# シートの列構成
# ==============================================================================
# Horsesシートの列（0始まりの番号）。馬名は和名ならA列、英字ならB列（英馬名）に記録する
# クラス・最新レース・収得賞金は取り込んだレース結果から計算して書き込む列（毎回レース結果を読み込まなくて済むように）。
# 収得賞金は最新レースが平地なら平地の、障害なら障害の収得賞金（3歳の6月にクラスが変わる馬を、計算し直さずに判定するため）
HORSE_COLUMNS = ["馬名", "英馬名", "性別", "生年月日", "父", "母", "馬主名", "拠点", "厩舎", "状態",
                 "産地", "地域", "生産牧場", "JRAのURL", "NARのURL", "競走馬登録", "登録時状態",
                 "クラス", "最新レース", "収得賞金"]
(H_NAME, H_NAME_EN, H_GENDER, H_BIRTH, H_SIRE, H_DAM, H_OWNER, H_AREA, H_STABLE, H_STATUS,
 H_REGION, H_DETAIL, H_BREEDER, H_JRA_URL, H_NAR_URL, H_REGISTRATION, H_REG_STATUS,
 H_CLASS, H_LAST_RACE, H_SHUTOKU) = range(len(HORSE_COLUMNS))
# Sire・Damシートの列
PARENT_COLUMNS = ["馬名", "英馬名", "生年月日", "父", "母", "馬主", "産地", "地域", "生産牧場", "JRAのURL", "NARのURL"]
(P_NAME, P_NAME_EN, P_BIRTH, P_SIRE, P_DAM, P_OWNER, P_REGION, P_DETAIL, P_BREEDER,
 P_JRA_URL, P_NAR_URL) = range(len(PARENT_COLUMNS))

def col_letter(idx):
    """0始まりの列番号を列記号（A, B, …）にする"""
    return chr(ord('A') + idx)

_JAPANESE_CHAR_RE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]')

def is_english_name(name):
    """馬名が英字表記か（英字を含み、かな・漢字を含まない）"""
    name = str(name or '').strip()
    return bool(name) and bool(re.search(r'[A-Za-z]', name)) and not _JAPANESE_CHAR_RE.search(name)

def name_column_for(name):
    """馬名を探す列：和名ならA列（0）、英字ならB列（1）。Horses・Sire・Damシート共通"""
    return 1 if is_english_name(name) else 0

def row_matches_name(row, name):
    """シートの行（リスト）が馬名に一致するか（和名はA列、英字はB列で照合する）"""
    name = str(name or '').strip()
    col = name_column_for(name)
    return bool(name) and len(row) > col and str(row[col] or '').strip() == name

def record_matches_name(record, name):
    """get_all_records の1件が馬名に一致するか（和名は「馬名」、英字は「英馬名」で照合する）"""
    name = str(name or '').strip()
    key = '英馬名' if is_english_name(name) else '馬名'
    return bool(name) and str(record.get(key, '') or '').strip() == name

def display_name_of_row(row):
    """表示用の馬名：A列（和名）が空欄ならB列（英馬名）"""
    a = str(row[0] or '').strip() if len(row) > 0 else ''
    b = str(row[1] or '').strip() if len(row) > 1 else ''
    return a or b

def normalize_name_records(records):
    """get_all_records の結果で「馬名」が空欄（英字の馬）の場合は、英馬名を「馬名」として扱う"""
    for r in records:
        if not str(r.get('馬名', '') or '').strip() and str(r.get('英馬名', '') or '').strip():
            r['馬名'] = str(r.get('英馬名')).strip()
    return records

# ==============================================================================
# スプレッドシートの読み込み待ち
# ==============================================================================
class SheetNotReady(Exception):
    """スプレッドシートを読み込めなかった（通信エラー・読み込み制限・データが0件など）"""
    pass

# 読み込み待ちの画面を何回まで自動で再読み込みするか（それを超えたら再読み込み／ホームを選ぶ画面）
SHEET_RETRY_LIMIT = 5

def sheet_strict():
    """競走馬一覧・詳細などの画面では、読み込みに失敗したら空のまま表示せず読み込み待ちにする"""
    try:
        return bool(getattr(g, 'strict_sheets', False))
    except RuntimeError:
        return False

def sheet_not_ready(message):
    """読み込み待ちの画面の対象なら SheetNotReady を送出する"""
    if sheet_strict():
        raise SheetNotReady(message)

def wait_for_sheets(view):
    """スプレッドシートの読み込みが完了するまで、1秒ごとに再読み込みする画面にする"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        g.strict_sheets = True
        try:
            return view(*args, **kwargs)
        except SheetNotReady as e:
            print(f"スプレッドシートの読み込み待ち（{request.path}）: {e}")
            try:
                retry = int(request.args.get('_retry', '0'))
            except ValueError:
                retry = 0
            params = request.args.to_dict()
            params['_retry'] = str(retry + 1)
            retry_url = request.path + '?' + '&'.join(
                f"{k}={requests.utils.quote(v, safe='')}" for k, v in params.items())
            params['_retry'] = '0'
            reload_url = request.path + '?' + '&'.join(
                f"{k}={requests.utils.quote(v, safe='')}" for k, v in params.items())
            return render_template('loading.html', retry=retry, retry_limit=SHEET_RETRY_LIMIT,
                                   retry_url=retry_url, reload_url=reload_url), 503
    return wrapper

# ==============================================================================
# 初期設定 (スプレッドシート)
# ==============================================================================
try:
    sh = gc.open(horse_data)
except gspread.SpreadsheetNotFound:
    sh = gc.create(horse_data)
    ws1 = sh.sheet1
    ws1.update_title("Horses")
    ws1.append_row(HORSE_COLUMNS)
    ws_miho = sh.add_worksheet(title="美浦", rows=100, cols=20)
    ws_miho.append_row(["厩舎名", "ヨミガナ", "生年月日", "免許取得年", "開業", "引退", "馬房数", "臨時貸付", "貸付終了"])
    ws_ritto = sh.add_worksheet(title="栗東", rows=100, cols=20)
    ws_ritto.append_row(["厩舎名", "ヨミガナ", "生年月日", "免許取得年", "開業", "引退", "馬房数", "臨時貸付", "貸付終了"])

try:
    sh.worksheet("Changes")
except gspread.WorksheetNotFound:
    ws_changes = sh.add_worksheet(title="Changes", rows=100, cols=10)
    ws_changes.append_row(["年月日", "馬名", "項目", "旧", "新"])

try:
    sh.worksheet("Entry")
except gspread.WorksheetNotFound:
    ws_entry = sh.add_worksheet(title="Entry", rows=100, cols=10)
    ws_entry.append_row(["race_date", "venue", "race_num", "horse_name", "status", "horse_num", "rank"])

# ==============================================================================
# 共通ヘルパー関数群
# ==============================================================================

def kana_to_hira(text):
    return "".join([chr(ord(c) - 96) if "ァ" <= c <= "ヶ" else c for c in text])

def hira_to_kana(text):
    return "".join([chr(ord(c) + 96) if "ぁ" <= c <= "ゖ" else c for c in text])

def stable_kana_key(kana):
    """厩舎のヨミガナを並べ替え・五十音の絞り込み用にそろえる（ひらがな・空白なし）。
    ヨミガナはカタカナ（空白あり）で登録するが、以前の登録分はひらがな（空白なし）のため"""
    return kana_to_hira(re.sub(r'\s+', '', str(kana or '')))

# --- 日付の表記（スプレッドシートには「20261008」のように年月日を数字8桁で記入する） ---
# 年月日を区切る形式（2026/10/8・2026-10-08・2026年10月8日）と、数字だけの形式（20261008・202610・2026）の両方を読み取る
_DATE_SEP_RE = re.compile(r'^\s*(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*(?:[/\-.月]\s*(\d{1,2})\s*日?)?')
_DATE_DIGITS_RE = re.compile(r'^\s*(\d{4})(\d{2})?(\d{2})?(?!\d)')

def split_date(value):
    """日付の文字列を (年, 月, 日) の整数に分解する。月・日が無い場合は None（読み取れなければ None を返す）"""
    if isinstance(value, (datetime, date)):
        return value.year, value.month, value.day
    text = str(value or '').strip().lstrip("'")
    m = _DATE_SEP_RE.match(text) or _DATE_DIGITS_RE.match(text)
    if not m:
        return None
    y, mo, d = (int(g) if g else None for g in m.groups())
    if (mo is not None and not 1 <= mo <= 12) or (d is not None and not 1 <= d <= 31):
        return None
    return y, mo, d

def format_date8(y, m=None, d=None):
    """年・月・日を「20261008」形式にする（日が無ければ「202610」、年だけなら「2026」）"""
    y = str(y or '').strip()
    if not y:
        return ''
    if not str(m or '').strip():
        return y
    if not str(d or '').strip():
        return f"{int(y):04d}{int(m):02d}"
    return f"{int(y):04d}{int(m):02d}{int(d):02d}"

def to_date8(value):
    """日付の文字列を「20261008」形式に揃える（読み取れない文字列はそのまま返す）"""
    parts = split_date(value)
    if not parts:
        return str(value or '').strip()
    return format_date8(*parts)

def today8():
    return format_date8(*split_date(datetime.now()))

def parse_full_date(value):
    """年月日がそろった日付の文字列を date に変換する（変換できなければ None）"""
    parts = split_date(value)
    if not parts or parts[1] is None or parts[2] is None:
        return None
    try:
        return date(*parts)
    except ValueError:
        return None

def date_year(value):
    """日付の文字列から西暦（整数）を取り出す（取り出せなければ None）"""
    parts = split_date(value)
    return parts[0] if parts else None

def format_date_jp(value, weekday=False):
    """画面表示用：日付を「2026年1月1日」形式にする（weekday=Trueなら「2026年1月1日(木)」）。
    月・日が無い場合は「2026年1月」「2026年」、読み取れない文字列はそのまま返す"""
    parts = split_date(value)
    if not parts:
        return str(value or '')
    y, m, d = parts
    text = f"{y}年" + (f"{m}月" if m else '') + (f"{d}日" if m and d else '')
    if weekday and m and d:
        try:
            text += f"({JP_WEEKDAYS[date(y, m, d).weekday()]})"
        except ValueError:
            pass
    return text

# テンプレートから日付の分解（入力欄の初期値用）と表示用の変換（|jpdate）を使えるようにする
app.jinja_env.globals['split_date'] = split_date
app.jinja_env.filters['jpdate'] = format_date_jp

# --- 「データ更新」の最終実行日時の記録・取得（Metaシートを使用） ---
JP_WEEKDAYS = ['月', '火', '水', '木', '金', '土', '日']

def _get_meta_sheet():
    sh = gc.open(horse_data)
    try:
        return sh.worksheet("Meta")
    except gspread.WorksheetNotFound:
        ws_meta = sh.add_worksheet(title="Meta", rows=10, cols=5)
        ws_meta.append_row(["項目", "値"])
        return ws_meta

def get_last_updated():
    """Metaシートから「データ更新」機能の最終実行日時を取得する（未設定ならNone）"""
    try:
        ws_meta = _get_meta_sheet()
        for row in ws_meta.get_all_values()[1:]:
            if len(row) > 0 and row[0] == 'last_updated':
                return row[1] if len(row) > 1 else None
        return None
    except Exception:
        return None

def set_last_updated(dt_str):
    """Metaシートに「データ更新」機能の最終実行日時を書き込む（無ければ新規追加）"""
    try:
        ws_meta = _get_meta_sheet()
        data = ws_meta.get_all_values()
        for i, row in enumerate(data[1:], start=2):
            if len(row) > 0 and row[0] == 'last_updated':
                ws_meta.update_acell(f'B{i}', dt_str)
                return
        ws_meta.append_row(['last_updated', dt_str])
    except Exception as e:
        print(f"最終更新日時の保存に失敗しました: {e}")

# --- JRAのページ（管理馬一覧・競走馬登録・抹消一覧）の取り込み状況 ---
# Metaシートに、取り込んだページの日付（YYYYMMDD）を種類ごとに記録する
IMPORT_KINDS = [
    # (Metaシートの項目名, 表示名, 通常の公開日)
    ('import_registration', '競走馬登録', '毎週月曜日（前週木曜日の登録分）'),
    ('import_trainer', '管理馬一覧', '毎週月曜日・木曜日'),
    ('import_cancel', '抹消一覧', '毎週月曜日・木曜日'),
]
IMPORT_SCHEDULE_NOTE = '月曜日まで競馬の開催がある週は、火曜日にずれることがあります。'

def get_meta_values():
    """Metaシートの「項目 → 値」をまとめて取得する"""
    try:
        return {r[0]: (r[1] if len(r) > 1 else '') for r in _get_meta_sheet().get_all_values()[1:] if r and r[0]}
    except Exception:
        return {}

def record_import_date(key, date8):
    """取り込んだページの日付を記録する（記録済みの日付より新しい場合だけ更新する）"""
    if not date8:
        return
    try:
        ws_meta = _get_meta_sheet()
        for i, row in enumerate(ws_meta.get_all_values()[1:], start=2):
            if row and row[0] == key:
                if not (len(row) > 1 and row[1] and row[1] >= date8):
                    ws_meta.update(range_name=f'B{i}', values=[[date8]])
                return
        ws_meta.append_row([key, date8])
    except Exception as e:
        print(f"取り込み日の記録に失敗しました（{key}）: {e}")

def get_import_status():
    """各ページの通常の公開日と最終更新日（取り込んだページの日付）の一覧"""
    meta = get_meta_values()
    return [{'label': label, 'schedule': schedule,
             'last': format_date_jp(meta.get(key, ''), weekday=True) if meta.get(key) else '未取り込み',
             'date8': meta.get(key, '')}
            for key, label, schedule in IMPORT_KINDS]

def get_latest_update_label(import_status=None):
    """競走馬一覧の「最終更新」：データ更新と各ページの取り込みのうち、最も新しい日付（時間は表示しない）"""
    dates = [d['date8'] for d in (import_status or get_import_status()) if d['date8']]
    last_updated = get_last_updated()
    if last_updated:
        parts = split_date(last_updated.split(' ')[0])
        if parts and parts[1] and parts[2]:
            dates.append(f"{parts[0]:04d}{parts[1]:02d}{parts[2]:02d}")
    return format_date_jp(max(dates), weekday=True) if dates else None

def format_last_updated(dt_str):
    """Meta保存形式（YYYYMMDD HH:MM。旧形式 YYYY/MM/DD HH:MM も可）を「年月日(曜日)」の表示形式に変換する"""
    if not dt_str:
        return None
    dt = None
    for fmt in ('%Y%m%d %H:%M', '%Y%m%d', '%Y/%m/%d %H:%M', '%Y/%m/%d'):
        try:
            dt = datetime.strptime(dt_str, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return dt_str
    return f"{dt.year}年{dt.month}月{dt.day}日({JP_WEEKDAYS[dt.weekday()]})"

# --- JRA競走馬情報ページ取得・解析 ---
JRA_ALLOWED_HOSTS = ("jra.go.jp", "jra.jp")

def _dt_dd_text(soup, label):
    """<dt>label</dt> の次にある <dd> のテキストを取得する"""
    dt = soup.find('dt', string=lambda s: s and s.strip() == label)
    if not dt:
        return None
    dd = dt.find_next_sibling('dd')
    if not dd:
        dd = dt.find_next('dd')
    if dd:
        return dd.get_text(strip=True)
    return None

def _text_after(element):
    """要素の直後（その要素自身の子孫は含まない）に現れる、最初の空でないテキストを取得する"""
    for sib in element.next_siblings:
        if isinstance(sib, str):
            text = sib.strip()
            if text:
                return text
        else:
            text = sib.get_text(strip=True)
            if text:
                return text
    # 同階層に見つからない場合は、親要素より後を辿る
    if element.parent is not None:
        return _text_after(element.parent)
    return None

def _fetch_jra_soup(url):
    """指定されたJRA公式サイトのURLを取得し、BeautifulSoupオブジェクトを返す"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not any(
        parsed.netloc.endswith(host) for host in JRA_ALLOWED_HOSTS
    ):
        raise ValueError("JRAの公式パソコンサイト（jra.go.jp/）のURLを入力してください。")

    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; keiba.log/1.0)"},
        timeout=10
    )
    resp.raise_for_status()

    html_text = resp.content.decode('cp932', errors='replace')
    return BeautifulSoup(html_text, 'html.parser')

def _parse_status_from_header(soup):
    """header_line内のspan.opt／span.restから、状態(抹消／放牧／入厩)を判定する。
    抹消の場合は (抹消, 抹消年月日 or None)、それ以外は (放牧 or 入厩, None) を返す"""
    header_div = soup.select_one('div.header_line.no-mb')
    opt_span = None
    if header_div:
        for cand in header_div.select('span.opt'):
            # span.txt の中にある span.opt は「競走馬情報」ラベル用なので無視する
            if cand.find_parent('span', class_='txt') is None:
                opt_span = cand
                break
    rest_span = header_div.select_one('span.rest') if header_div else None

    if opt_span:
        text = opt_span.get_text(strip=True)
        cancel_date = None
        m = re.search(r'(\d{4})\D+(\d{1,2})\D+(\d{1,2})', text)
        if m:
            cancel_date = format_date8(m.group(1), m.group(2), m.group(3))
        return '抹消', cancel_date

    return ('放牧' if rest_span else '入厩'), None

JRA_KAKU_ICONS = ('カクチ', 'カクガイ')   # 〔地〕地方所属馬・〔外〕外国調教馬（JRAの競走馬としては登録しない）
JRA_KAKU_ERROR = "地方または海外所属の馬は地方またはその他のタブを選択して登録してください。"

def _jra_link_url(a):
    """競走馬のリンク（href または onclick="doAction('/JRADB/accessU.html', 'pw01dud…')"）を、開けるURLにする"""
    if a is None:
        return ''
    href = a.get('href') or ''
    m = re.search(r'CNAME=(pw01dud[0-9A-Za-z]+/[0-9A-Fa-f]{2})', href)
    if not m:
        m = re.search(r"doAction\(\s*'[^']*accessU[^']*'\s*,\s*'(pw01dud[0-9A-Za-z]+/[0-9A-Fa-f]{2})'", a.get('onclick') or '')
    return f"https://www.jra.go.jp/JRADB/accessU.html?CNAME={m.group(1)}" if m else ''

def _dd_link_url(soup, label):
    """<dt>label</dt> の <dd> にある競走馬のリンク先（JRAに競走馬として登録されている馬だけリンクがある）"""
    dt = soup.find('dt', string=lambda s: s and s.strip() == label)
    dd = (dt.find_next_sibling('dd') or dt.find_next('dd')) if dt else None
    if not dd:
        return ''
    for a in dd.find_all('a'):
        url = _jra_link_url(a)
        if url:
            return url
    return ''

def _jra_horse_icons(soup, opt_span=None):
    """馬名の前の記号（マルチ・カクチ など）の一覧"""
    areas = list(soup.select('div.header_line'))
    if opt_span is not None:
        parent = opt_span.find_parent('div')
        if parent is not None:
            areas.append(parent)
    icons = []
    for area in areas:
        for img in area.select('img[alt]'):
            if img['alt'] and img['alt'] not in icons:
                icons.append(img['alt'])
    return icons

def fetch_jra_horse_info(url):
    soup = _fetch_jra_soup(url)

    result = {}

    # 状態（抹消／放牧／入厩）：add_horse・edit_horseでも放牧/入厩を自動判定するために取得する
    status, cancel_date = _parse_status_from_header(soup)
    result['status'] = status
    if cancel_date:
        result['cancel_date'] = cancel_date

    # 馬名：<span class="opt">競走馬情報</span> の次の値
    # （spanタグ自身の子テキストを拾ってしまわないよう、兄弟要素以降だけを探索する）
    opt_span = soup.find('span', class_='opt', string=lambda s: s and '競走馬情報' in s)
    if opt_span:
        name = _text_after(opt_span)
        if name:
            result['name'] = name

    # 英馬名：<span class="name_en">Dahlia Flavor（JPN）</span>（国名の括弧書きは除く）
    name_en_span = soup.select_one('span.name_en')
    if name_en_span:
        name_en = _strip_country_suffix(name_en_span.get_text(strip=True))
        if name_en:
            result['name_en'] = name_en
            # 馬名の取得で英馬名まで含んでしまった場合は取り除く
            if result.get('name', '').endswith(name_en_span.get_text(strip=True)):
                result['name'] = result['name'][:-len(name_en_span.get_text(strip=True))].strip()

    # 地方・海外所属の馬（馬名の前に〔地〕〔外〕の記号がある）
    icons = _jra_horse_icons(soup, opt_span)
    kaku = next((i for i in icons if i in JRA_KAKU_ICONS), '')
    if kaku:
        result['kaku'] = kaku

    # 「産駒一覧」のボタン（産駒がいる馬＝繁殖馬になっている馬だけ表示される）
    #  <div class="opt2"> … <a onclick="return doAction('/JRADB/accessU.html', 'pj01snk…');">産駒一覧</a>
    result['has_offspring'] = any('産駒一覧' in a.get_text() and 'pj01snk' in (a.get('onclick') or '')
                                  for a in soup.find_all('a'))

    # 父・母・母の父のリンク（JRAに競走馬として登録されている馬だけリンクがある）
    for key, label in (('sire_url', '父'), ('dam_url', '母'), ('dam_sire_url', '母の父')):
        link = _dd_link_url(soup, label)
        if link:
            result[key] = link
    dam_sire = _dt_dd_text(soup, '母の父')
    if dam_sire:
        result['dam_sire'] = dam_sire

    sire = _dt_dd_text(soup, '父')
    if sire:
        result['sire'] = sire

    dam = _dt_dd_text(soup, '母')
    if dam:
        # 「産駒」の文字が含まれる場合は取り除く（括弧付きの場合も考慮）
        dam = re.sub(r'[（(]?産駒[）)]?', '', dam).strip()
        result['dam'] = dam

    gender_text = _dt_dd_text(soup, '性別')
    if gender_text:
        if 'せん' in gender_text or 'セン' in gender_text:
            result['gender'] = 'せん'
        elif '牝' in gender_text:
            result['gender'] = '牝'
        elif '牡' in gender_text:
            result['gender'] = '牡'

    birth_text = _dt_dd_text(soup, '生年月日')
    if birth_text:
        m = re.search(r'(\d{4})\D+(\d{1,2})\D+(\d{1,2})', birth_text)
        if m:
            result['birth_year'] = m.group(1)
            result['birth_month'] = str(int(m.group(2)))
            result['birth_day'] = str(int(m.group(3)))

    owner = _dt_dd_text(soup, '馬主名')
    if owner:
        result['owner'] = owner

    # 調教師名：「名前（所属）」形式で取得されるため、そのまま渡してフロント側で
    # 「所属・名前」形式に変換し、厩舎の選択肢から一致するものを選ぶ
    trainer_raw = _dt_dd_text(soup, '調教師名')
    if trainer_raw:
        result['trainer_raw'] = trainer_raw

    breeder = _dt_dd_text(soup, '生産牧場') or _dt_dd_text(soup, '生産者')
    if breeder:
        result['breeder'] = breeder

    # 産地：フロント側で birthplace_detail → birthplace_region → 海外(略称変換) の
    # 優先順位でマッチングするため、生データをそのまま渡す
    birthplace = _dt_dd_text(soup, '産地')
    if birthplace:
        result['birthplace'] = birthplace

    return result

def fetch_jra_horse_status(url):
    """データ更新機能用：馬の現在の状態（抹消／放牧／入厩）と、
    抹消でない場合の性別・馬主名・調教師名（生データ）を取得する"""
    soup = _fetch_jra_soup(url)
    result = {}

    status, cancel_date = _parse_status_from_header(soup)
    result['status'] = status

    # ① 抹消判定：header_line内にspan.optがあれば抹消
    if status == '抹消':
        if cancel_date:
            result['cancel_date'] = cancel_date
        return result

    # ③ 性別（add_horseと同じ判定ロジック）
    gender_text = _dt_dd_text(soup, '性別')
    if gender_text:
        if 'せん' in gender_text or 'セン' in gender_text:
            result['gender'] = 'せん'
        elif '牝' in gender_text:
            result['gender'] = '牝'
        elif '牡' in gender_text:
            result['gender'] = '牡'

    # ④ 馬主名・調教師名
    owner = _dt_dd_text(soup, '馬主名')
    if owner:
        result['owner'] = owner

    trainer_raw = _dt_dd_text(soup, '調教師名')
    if trainer_raw:
        result['trainer_raw'] = trainer_raw

    return result

# --- JBISサーチ経由での血統情報取得（JRAに掲載のない馬向け） ---
JBIS_BASE = "https://www.jbis.or.jp"
# 検索結果ページの実URL（sid=horse：馬情報検索／keyword：馬名／match=exact：完全一致検索／
# entry：登録状況の絞り込み＝3が種牡馬、4が繁殖牝馬）
JBIS_RESULT_URL = "https://www.jbis.or.jp/horse/result/"
JBIS_ENTRY_CODE = {"Sire": "entry_3", "Dam": "entry_4"}
_JBIS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.jbis.or.jp/",
}
_ALPHABET_NAME_RE = re.compile(r'^[A-Za-z0-9 .\'\-]+$')
_KATAKANA_NAME_RE = re.compile(r'^[\u30A0-\u30FF\s　]+$')
_COUNTRY_SUFFIX_RE = re.compile(r'[（(][^（）()]*[）)]\s*$')

class JbisMultipleMatchError(Exception):
    """JBISサーチの検索結果が複数件だった場合に送出する"""
    pass

def _fetch_jbis_soup(url, params=None, method="GET"):
    """指定されたJBIS（jbis.or.jp）のURLを取得し、BeautifulSoupオブジェクトを返す。
    JBISサイトは応答が遅いことがあるため、タイムアウトを長めにし、
    タイムアウト時は1回だけ再試行する。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc.endswith("jbis.or.jp"):
        raise ValueError("JBISのURL（jbis.or.jp）ではありません。")

    last_error = None
    for attempt in range(2):
        try:
            if method.upper() == "POST":
                resp = requests.post(url, data=params, headers=_JBIS_HEADERS, timeout=25)
            else:
                resp = requests.get(url, params=params, headers=_JBIS_HEADERS, timeout=25)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or 'utf-8'
            return BeautifulSoup(resp.text, 'html.parser')
        except requests.exceptions.Timeout as e:
            last_error = e
            continue
    raise last_error

def _strip_country_suffix(text):
    """名前末尾の「(国名)」のような括弧書きを取り除く"""
    if not text:
        return text
    return _COUNTRY_SUFFIX_RE.sub('', text.strip()).strip()

def search_jbis_horse_url(horse_name, p_type):
    """
    JBISサーチ（馬情報検索）で馬名を完全一致検索する
    （種別に応じて登録状況を種牡馬／繁殖牝馬に絞り込む）。
    検索結果が1件ならその馬の詳細ページの絶対URLを返す。
    0件の場合はNone、2件以上の場合はJbisMultipleMatchErrorを送出する。
    """
    payload = {
        'sid': 'horse',
        'keyword': horse_name,
        'match': 'exact',
        'entry': JBIS_ENTRY_CODE.get(p_type, ''),
    }

    result_soup = _fetch_jbis_soup(JBIS_RESULT_URL, params=payload, method='GET')

    count_span = result_soup.select_one('div.data-hit__count span')
    count_text = count_span.get_text(strip=True) if count_span else ''
    count = int(count_text) if count_text.isdigit() else None

    if not count:
        return None
    if count >= 2:
        raise JbisMultipleMatchError("検索結果が複数あります。URLを入力してください。")

    link = result_soup.select_one('a.txt-link[href^="/horse/"]')
    if not link or not link.get('href'):
        return None
    return requests.compat.urljoin(JBIS_BASE, link.get('href'))

def _resolve_jbis_sub_name(name, url):
    """リンク先ページの<span class="hdg1-search__sub">の馬名（国名の括弧書きは除去）を返す。
    取得できなければ name をそのまま返す"""
    if not url:
        return name
    try:
        sub_span = _fetch_jbis_soup(url).select_one('span.hdg1-search__sub')
        sub_name = _strip_country_suffix(sub_span.get_text(strip=True)) if sub_span else ''
        if sub_name:
            return sub_name
    except Exception:
        pass
    return name

def _resolve_jbis_display_name(name, url):
    """
    名前がアルファベット表記の場合、リンク先ページの<span class="hdg1-search__sub">を
    採用する（国名の括弧書きは除去する）。それ以外はそのまま返す。
    """
    if not name or not url or not _ALPHABET_NAME_RE.match(name):
        return name
    try:
        parent_soup = _fetch_jbis_soup(url)
        sub_span = parent_soup.select_one('span.hdg1-search__sub')
        if sub_span:
            sub_name = _strip_country_suffix(sub_span.get_text(strip=True))
            if sub_name:
                return sub_name
    except Exception:
        pass
    return name

def _extract_jbis_parent(soup, class_name, label_texts):
    """
    父・母の (名前, URL) を取得する。
    ページ内では「父の母」等のネスト表示にも同じクラス（data-3__female等）が
    使い回されていることがあるため、まず見出し（<div class="{class_name}-hdg">母</div>等）
    に「直後に続く」<div class="{class_name}">を優先的に探す。
    見つからない場合は指定クラスをそのまま探し、それでも見つからない場合はさらに緩い
    フォールバック（クラス名の部分一致／ラベル文字列への近接）を行う。
    """
    def _link_to_result(link):
        if not link or not link.get('href'):
            return None
        name = _strip_country_suffix(link.get_text(strip=True))
        if not name:
            return None
        url = requests.compat.urljoin(JBIS_BASE, link.get('href'))
        return name, url

    def _link_in(container):
        if not container:
            return None
        return container.select_one('a.txt-link[href^="/horse/"]') or container.select_one('a[href^="/horse/"]')

    # 0. 見出し（例："母"）の直後に続くdiv.{class_name}を優先する
    #    （「父の母」等、同じクラス名が別の意味で使い回されているケースへの対策）
    hdg_class = f'{class_name}-hdg'
    for label_text in label_texts:
        for hdg in soup.select(f'.{hdg_class}'):
            if hdg.get_text(strip=True) != label_text:
                continue
            content = hdg.find_next_sibling(class_=class_name)
            result = _link_to_result(_link_in(content))
            if result:
                return result

    # 1. 指定されたクラス名そのもの（最初に見つかったもの）
    result = _link_to_result(_link_in(soup.select_one(f'div.{class_name}')))
    if result:
        return result

    # 2. クラス名の一部（male/female）を含む要素
    key = 'male' if 'male' in class_name else 'female'
    for el in soup.select(f'[class*="{key}"]'):
        result = _link_to_result(_link_in(el))
        if result:
            return result

    # 3. ラベル文字列（父／母／Sire／Dam等）に隣接する馬リンク
    for label_text in label_texts:
        label_el = soup.find(string=lambda s: s and s.strip() == label_text)
        if not label_el:
            continue
        container = label_el.find_parent()
        for _ in range(4):
            if not container:
                break
            result = _link_to_result(_link_in(container))
            if result:
                return result
            container = container.find_next_sibling() or container.find_parent()

    return None

# --- equibase（アメリカ等の海外産馬）のページの文章からの取得 ---
_ENGLISH_MONTHS = {m: i for i, m in enumerate(
    ['january', 'february', 'march', 'april', 'may', 'june', 'july',
     'august', 'september', 'october', 'november', 'december'], start=1)}
# 馬主・生産者が分からないときの表記（空欄として扱う）
_EQUIBASE_EMPTY_VALUES = {'', 'n/a', 'na', 'unknown', 'none', '-'}

# 地域変換表シートの初期値（equibaseの馬名の後ろの「(KY)」などの略称 → 産地（海外）の地域名）
REGION_CONVERSION_DEFAULTS = (
    [('USA', 'アメリカ')]
    + [(code, 'アメリカ') for code in (
        'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH '
        'NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY').split()]
    + [('CAN', 'カナダ')]
    + [(code, 'カナダ') for code in 'AB BC MB NB NL NS ON PE QC SK'.split()]
    + [('MEX', 'メキシコ'), ('GB', 'イギリス'), ('IRE', 'アイルランド'), ('FR', 'フランス'), ('GER', 'ドイツ'),
       ('ITY', 'イタリア'), ('DEN', 'デンマーク'), ('SWE', 'スウェーデン'), ('AUS', 'オーストラリア'),
       ('NZ', 'ニュージーランド'), ('ARG', 'アルゼンチン'), ('CHI', 'チリ'), ('BRZ', 'ブラジル'),
       ('URU', 'ウルグアイ'), ('PER', 'ペルー'), ('UAE', 'アラブ首長国連邦'), ('SAU', 'サウジアラビア'),
       ('SAF', '南アフリカ')]
)

def get_region_conversion_map():
    """スプレッドシートの「地域変換表」シート（A列：略称、B列：地域名）を読み込む。
    シートが無ければ初期値で作成する。"""
    sh = gc.open(horse_data)
    try:
        rows = sh.worksheet("地域変換表").get_all_values()[1:]
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="地域変換表", rows=len(REGION_CONVERSION_DEFAULTS) + 20, cols=2)
        ws.update(range_name='A1', values=[["略称", "地域"]] + [list(r) for r in REGION_CONVERSION_DEFAULTS])
        rows = [list(r) for r in REGION_CONVERSION_DEFAULTS]
    return {r[0].strip().upper(): r[1].strip() for r in rows if len(r) > 1 and r[0].strip() and r[1].strip()}

def parse_equibase_text(text):
    """equibaseの競走馬ページからコピーした文章を読み取り、地域・生年月日・馬主名・生産牧場を返す。
    例：
        Jealous Wildcat (MD)
        TB, B, M, foaled March 26, 1998
        ( Forest Wildcat - Jealous Sword, by Crusader Sword )
        ...
        Owner: Sally Thomas
        Breeder: Howard M. Bender & Sondra Bender
    """
    lines = [l.strip() for l in (text or '').splitlines() if l.strip()]
    # equibaseの馬は海外産なので、産地は「海外」にする
    result = {'birthplace_region': '海外'}

    # 馬名の後ろの「(MD)」などの略称から地域を求める（地域変換表シートで変換）
    for line in lines:
        m = re.search(r'\(([A-Za-z]{2,4})\)\s*$', line)
        if m:
            code = m.group(1).upper()
            result['origin_code'] = code
            # 略称の前が馬名（英馬名）
            name_en = re.sub(r'\s+', ' ', line[:m.start()]).strip()
            if name_en:
                result['name_en'] = name_en
            detail = get_region_conversion_map().get(code)
            if detail:
                result['birthplace_detail'] = detail
            break

    # 「foaled March 26, 1998」から生年月日
    m = re.search(r'foaled\s+([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})', text or '', re.I)
    if m and m.group(1).lower() in _ENGLISH_MONTHS:
        result['birth_year'] = m.group(3)
        result['birth_month'] = str(_ENGLISH_MONTHS[m.group(1).lower()])
        result['birth_day'] = str(int(m.group(2)))

    # 「Owner: 〇〇」「Breeder: 〇〇」（N/A・Unknown・空欄は空欄にする）
    # コピーのしかたによって値が次の行に分かれる場合は、次の行（別の項目でなければ）を値とする
    for key, label in (('owner', 'Owner'), ('breeder', 'Breeder')):
        value = ''
        for i, line in enumerate(lines):
            m = re.match(rf'{label}\s*:\s*(.*)$', line, re.I)
            if not m:
                continue
            value = m.group(1)
            if not value.strip() and i + 1 < len(lines) and not re.match(r'[A-Za-z][A-Za-z ]{0,20}:', lines[i + 1]):
                value = lines[i + 1]
            break
        # ページをコピーした時に一緒に入るリンクの文字（Owner Link など）は取り除く
        value = re.sub(rf'\b{label}\s+Link\b', ' ', value, flags=re.I)
        value = re.sub(r'\s+', ' ', value).strip()
        if value and value.lower() not in _EQUIBASE_EMPTY_VALUES:
            result[key] = value
    return result

def parse_jbis_profile(soup):
    """JBISの競走馬ページの基本情報（見出しの次の欄）から、生年月日・馬主名・産地・生産牧場を取り出す"""
    def value_of(*labels):
        for el in soup.find_all(['th', 'dt', 'td']):
            if el.get_text(strip=True) in labels:
                nxt = el.find_next_sibling(['td', 'dd'])
                if nxt:
                    return re.sub(r'\s+', ' ', nxt.get_text(' ', strip=True)).strip()
        return ''

    result = {}
    # 生年月日は「1960.4.1」「1960.4.1 ○歳」、または古い馬では「1960」のように西暦だけ。
    # 後ろの年齢（○歳）は先に取り除き、月や日と取り違えないようにする
    birth_text = re.sub(r'[（(]?\s*\d+\s*歳\s*[）)]?', '', value_of('生年月日'))
    m = re.search(r'(\d{4})\D+(\d{1,2})\D+(\d{1,2})', birth_text)
    if m:
        result['birth_year'] = m.group(1)
        result['birth_month'] = str(int(m.group(2)))
        result['birth_day'] = str(int(m.group(3)))
    else:
        m = re.search(r'(\d{4})', birth_text)
        if m:
            result['birth_year'] = m.group(1)
    owner = normalize_nar_company_name(value_of('馬主', '馬主名'))
    if owner:
        result['owner'] = owner
    breeder = normalize_nar_company_name(value_of('生産者', '生産牧場'))
    if breeder:
        result['breeder'] = breeder
    # 産地は「北海道浦河郡浦河町」「浦河町（日高）」など。括弧書きは除き、画面側で末尾の市町村から選ぶ
    birthplace = re.sub(r'[（(][^（）()]*[）)]', '', value_of('産地')).replace(' ', '').strip()
    if birthplace:
        result['birthplace'] = birthplace
    return result

def fetch_other_horse_info(p_name, p_type, direct_url=None, overseas=False):
    """
    直接URLが指定されていればそのページから、指定がなければJBISサーチで馬名から特定した
    馬のページから、父・母の名前を取得する。
    父・母の名前がアルファベット表記の場合は、それぞれの詳細ページから
    <span class="hdg1-search__sub"> の値（国名の括弧書きを除いたもの）を採用する。
    """
    if direct_url:
        horse_url = direct_url
    else:
        horse_url = search_jbis_horse_url(p_name, p_type)
        if not horse_url:
            raise ValueError("JBISサーチで馬が見つかりませんでした。")

    detail_soup = _fetch_jbis_soup(horse_url)
    result = {"jbis_url": horse_url}

    # 馬名（<h1>ココナフレイバー</h1>）と英馬名（<span class="hdg1-search__sub">Cocona Flavor(JPN)</span>）
    heading = detail_soup.select_one('div.hdg1-search')
    if heading:
        # 馬名の後ろの国名が(JPN)以外なら海外産の馬として扱う
        for el in heading.select('h1, span.hdg1-search__sub'):
            m = re.search(r'[（(]\s*([A-Za-z]{2,4})\s*[）)]\s*$', el.get_text(strip=True))
            if m and m.group(1).upper() != 'JPN':
                overseas = True
        h1 = heading.select_one('h1')
        sub = heading.select_one('span.hdg1-search__sub')
        h1_name = _strip_country_suffix(h1.get_text(strip=True)) if h1 else ''
        sub_name = _strip_country_suffix(sub.get_text(strip=True)) if sub else ''
        # 外国産馬は見出しが英字・サブが和名の場合もあるため、表記で振り分ける
        for value in (h1_name, sub_name):
            if value:
                result.setdefault('name_en' if is_english_name(value) else 'name', value)

    # 生年月日・馬主名・産地・生産牧場も取得する（海外産馬でequibaseのページが無い場合もあるため、
    # 海外産馬も取得する。equibaseのデータが入力されていれば、画面側でその内容に上書きする）
    result.update(parse_jbis_profile(detail_soup))

    # 登録する馬の名前が英字、または産地が海外で、父・母がカタカナ表記の場合は、父・母のページの
    # <span class="hdg1-search__sub">（例：Danehill(USA)）から英字の馬名を取得する
    english_horse = is_english_name(p_name) or overseas

    def _parent_name(name, url):
        if english_horse:
            # 英字の馬の父・母は英字で揃える（英字表記の父・母はそのまま使う）
            return _resolve_jbis_sub_name(name, url) if name and not is_english_name(name) else name
        return _resolve_jbis_display_name(name, url)

    sire_info = _extract_jbis_parent(detail_soup, 'data-3__male', ['父', 'Sire'])
    if sire_info:
        result['sire'] = _parent_name(*sire_info)

    dam_info = _extract_jbis_parent(detail_soup, 'data-3__female', ['母', 'Dam'])
    if dam_info:
        result['dam'] = _parent_name(*dam_info)

    return result

def parse_trainer_field(text):
    """「名前（所属）」形式の文字列を (name, area) に分解する（add_horse.htmlのJS版と同等）"""
    m = re.match(r'^(.+?)[（(]\s*(.+?)\s*[）)]\s*$', text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), ''

def find_stable_match(all_stables, area, trainer_name):
    """所属・調教師名から、登録済み厩舎の選択肢の中から一致するものを探す（add_horse.htmlのJS版と同等）"""
    candidates = [s for s in all_stables if not area or s['area'] == area]

    def stable_name_of(s):
        return s['display_name'].split('・', 1)[1] if '・' in s['display_name'] else s['display_name']

    for s in candidates:
        if stable_name_of(s) == trainer_name:
            return s
    for s in candidates:
        sn = stable_name_of(s)
        if sn and (trainer_name in sn or sn in trainer_name):
            return s
    return None

def get_all_horses():
    """Horsesシートの全行を返す（英字の馬はA列が空欄のため、A列の位置に英馬名を入れて返す）"""
    try:
        sh = gc.open(horse_data)
        data = sh.worksheet("Horses").get_all_values()
        rows = []
        for r in data[1:]:
            r = list(r)
            if r and not str(r[0]).strip() and len(r) > 1 and str(r[1]).strip():
                r[0] = r[1]
            rows.append(r)
        if not rows:
            sheet_not_ready("Horsesシートのデータが0件です")
        return rows
    except SheetNotReady:
        raise
    except Exception as e:
        sheet_not_ready(f"Horsesシートを読み込めませんでした: {e}")
        return []

def get_stables_list():
    try:
        sh = gc.open(horse_data)
        all_stables = []
        current_year = datetime.now().year # 現在の年を取得
        
        for sheet_name in ["美浦", "栗東"]:
            try:
                sheet = sh.worksheet(sheet_name)
                data = sheet.get_all_values()
                for row in data[1:]:
                    if len(row) >= 2 and row[0] and row[1]:
                        # 列インデックスの更新 (E:開業[4], F:引退[5], G:馬房数[6], H:臨時貸付[7], I:貸付終了[8])
                        opening = row[4] if len(row) > 4 else ""
                        retirement = row[5] if len(row) > 5 else ""
                        capacity_str = row[6] if len(row) > 6 else "0"
                        temp_loan_str = row[7] if len(row) > 7 else "0"
                        loan_end_str = row[8] if len(row) > 8 else ""

                        # 貸付終了日（I列）が過去の日付なら、臨時貸付は馬房数の計算に含めない
                        if loan_end_str:
                            loan_end_date = parse_full_date(loan_end_str)
                            if loan_end_date and loan_end_date < datetime.now().date():
                                temp_loan_str = "0"
                        
                        # --- 〇年目の計算（開業の年月日の西暦から数える） ---
                        years_active = ""
                        if opening:
                            # YYYYMMDD・YYYY/MM/DD・YYYY-MM-DD から西暦部分を抽出
                            open_y = date_year(opening)
                            if open_y:
                                years_active = f"{current_year - open_y + 1}年目"
                                
                        # --- 馬房数と臨時貸付の合計・文字装飾 ---
                        display_capacity = capacity_str
                        if capacity_str != "技術調教師":
                            try:
                                cap_val = int(capacity_str) if capacity_str else 0
                                temp_val = int(temp_loan_str) if temp_loan_str else 0
                                total_stalls = cap_val + temp_val
                                
                                # 臨時貸付がある場合は「〇 (臨時：〇)」を追加
                                if temp_val > 0:
                                    display_capacity = f"{total_stalls}（臨時：{temp_val}）"
                                else:
                                    display_capacity = f"{total_stalls}"
                            except ValueError:
                                display_capacity = capacity_str

                        all_stables.append({
                            'display_name': f"{sheet_name}・{row[0]}", 
                            'kana': stable_kana_key(row[1]),
                            'area': sheet_name,
                            'birth_date': row[2] if len(row) > 2 else "",
                            'license_year': row[3] if len(row) > 3 else "",
                            'opening': opening,
                            'years_active': years_active,
                            'retirement': retirement,
                            'capacity': display_capacity,
                            'raw_capacity': capacity_str,
                            'temp_loan': temp_loan_str
                        })
            except gspread.WorksheetNotFound:
                continue
        if not all_stables:
            sheet_not_ready("美浦・栗東シートのデータが0件です")
        return all_stables
    except SheetNotReady:
        raise
    except Exception as e:
        sheet_not_ready(f"美浦・栗東シートを読み込めませんでした: {e}")
        return []

# --- ファイル操作・整理の共通処理 ---
def sort_and_resize_table(ws, sort_col_index=0, key=None):
    """スプレッドシートのデータをメモリ上でソートし一括更新する（key を指定するとその値で並べ替える）"""
    data = ws.get_all_values()
    if len(data) <= 1: return
    headers = data[0]
    if sort_col_index == 0 and len(headers) > 1 and headers[1] == '英馬名':
        # Horses・Sire・Damシート：英字の馬はA列が空欄でB列（英馬名）に記録しているため、
        # A列が空欄ならB列の馬名で並べる（どちらも空欄の行は除く）
        rows = [r for r in data[1:] if display_name_of_row(r)]
        key = key or (lambda v: v)
        rows.sort(key=lambda x: key(display_name_of_row(x)))
        ws.clear()
        ws.update(range_name='A1', values=[headers] + rows)
        return
    rows = [r for r in data[1:] if r and len(r) > sort_col_index and r[sort_col_index]]
    
    key = key or (lambda v: v)
    rows.sort(key=lambda x: key(x[sort_col_index]) if x[sort_col_index] else "")
    ws.clear()
    ws.update(range_name='A1', values=[headers] + rows)

def extract_year(date_str):
    """ '20000101', '2000/1/1', '2000-01-01', '2000' などの文字列から西暦を抽出 """
    if not date_str:
        return None
    return date_year(date_str)

_NAME_DAM_YEAR_RE = re.compile(r'^(.*?)\s*\((.*?)\s*-\s*(\d{4})[年]?\)$')

def split_dam_annotation(raw):
    """
    「馬名 (母馬 - 生年)」形式の文字列を (表示名, 母馬名 or None, 生年 or None) に分解する。
    同名馬混同を避けるために付与される注記を解析する共通ヘルパー。
    """
    raw = (raw or '').strip()
    if not raw:
        return '', None, None
    m = _NAME_DAM_YEAR_RE.match(raw)
    if m:
        return m.group(1).strip(), m.group(2).strip(), int(m.group(3))
    return raw, None, None

def get_5gen_pedigree(sire_name, dam_name, base_birth_year, gc, base_is_english=False):
    # スプレッドシートから全データを取得
    try:
        wb = gc.open('Horse_Data')
        sire_data = wb.worksheet('Sire').get_all_records()
        dam_data = wb.worksheet('Dam').get_all_records()
    except Exception as e:
        print(f"Spreadsheet fetch error: {e}")
        sheet_not_ready(f"Sire・Damシートを読み込めませんでした: {e}")
        sire_data = []
        dam_data = []
    if not sire_data or not dam_data:
        # Sire・Damシートの登録が0件＝読み込みに失敗している（親馬が見つからないのとは区別する）
        sheet_not_ready("Sire・Damシートのデータが0件です")

    # 馬名は和名ならA列（馬名）、英字ならB列（英馬名）から探す
    def _index_by(records, key):
        result = {}
        for r in records:
            name = str(r.get(key, '') or '').strip()
            if name:
                result.setdefault(name, []).append(r)
        return result

    sire_dict, sire_dict_en = _index_by(sire_data, '馬名'), _index_by(sire_data, '英馬名')
    dam_dict, dam_dict_en = _index_by(dam_data, '馬名'), _index_by(dam_data, '英馬名')
    # 候補の表示用に、英字の馬は英馬名を「馬名」として扱う
    normalize_name_records(sire_data)
    normalize_name_records(dam_data)

    pedigree = {}

    def traverse(node_index, h_name, child_birth_year, is_sire, child_is_english=False):
        if node_index >= 64:
            return
        
        h_name = str(h_name).strip() if h_name else ''
        if not h_name or h_name == '不明':
            pedigree[node_index] = None
            return

        # 1. 括弧指定「馬名 (母馬 - 生年)」の解析
        match = re.match(r'^(.*?)\s*\((.*?)\s*-\s*(\d{4})[年]?\)$', h_name)
        
        display_name = h_name
        target_dam = None
        target_year = None
        
        if match:
            display_name = match.group(1).strip()
            target_dam = match.group(2).strip()
            target_year = int(match.group(3))

        if is_english_name(display_name):
            records = (sire_dict_en if is_sire else dam_dict_en).get(display_name, [])
        else:
            records = (sire_dict if is_sire else dam_dict).get(display_name, [])
        record = None
        is_ambiguous = False
        candidates = []

        if records:
            if match:
                for r in records:
                    r_dam = str(r.get('母', '')).strip()
                    r_dob = str(r.get('生年月日', '')).strip()
                    r_year = extract_year(r_dob)
                    
                    if r_dam == target_dam and r_year == target_year:
                        record = r
                        break
                
                if not record:
                    record = records[0]
            else:
                if child_birth_year:
                    valid_records = []   # 仔馬の生年より前に生まれたことが確認できる候補
                    unknown_records = [] # 生年不明で、前か後か判定できない候補
                    for r in records:
                        dob = str(r.get('生年月日', '')).strip()
                        b_year = extract_year(dob)
                        if b_year is not None:
                            if b_year < child_birth_year:
                                valid_records.append((r, b_year))
                            # b_year >= child_birth_year の場合は
                            # 仔馬より後（または同年）に生まれた馬なので候補から除外する
                        else:
                            unknown_records.append(r)

                    if valid_records:
                        # 仔馬の生年に最も近い（＝より新しい）候補を優先
                        valid_records.sort(key=lambda x: child_birth_year - x[1])
                        record = valid_records[0][0]
                        if len(valid_records) > 1:
                            is_ambiguous = True
                            candidates = [r[0] for r in valid_records]
                    elif unknown_records:
                        # 前後関係を確認できる候補が無い場合のみ、生年不明の馬を次点として使用
                        record = unknown_records[0]
                        if len(unknown_records) > 1:
                            is_ambiguous = True
                            candidates = unknown_records
                    else:
                        # 仔馬より前に生まれたことが確認できる候補も、不明な候補も無い
                        # （＝同名馬は全て仔馬と同年か後に生まれている）場合は
                        # 誤った馬を親として表示しないよう、未登録として扱う
                        record = None
                else:
                    record = records[0]
                    if len(records) > 1:
                        is_ambiguous = True
                        candidates = records

        needs_update = True
        b_year = None
        sire_of_h = ''
        dam_of_h = ''
        birthplace_region = ''
        birthplace_detail = ''
        
        if record:
            dob = str(record.get('生年月日', '')).strip()
            s = str(record.get('父', '')).strip()
            d = str(record.get('母', '')).strip()
            b_reg = str(record.get('産地', '')).strip()
            b_det = str(record.get('地域', '')).strip()
            
            # 英馬名が未登録の馬も、編集リンクを表示して登録できるようにする
            name_en = str(record.get('英馬名', '') or '').strip()
            if dob and s and d and b_reg and name_en:
                needs_update = False

            b_year = extract_year(dob)
            sire_of_h = s
            dam_of_h = d
            birthplace_region = b_reg
            birthplace_detail = b_det
            
        age_when_born = None
        if b_year is not None and child_birth_year is not None:
            age_when_born = child_birth_year - b_year

        # 仔が英字の馬名で表示されている場合は、親も英馬名（登録されていれば）で表示する
        if child_is_english and record and not is_english_name(display_name):
            name_en = str(record.get('英馬名', '') or '').strip()
            if name_en:
                display_name = name_en

        pedigree[node_index] = {
            'name': display_name,
            'full_db_name': h_name,
            'birth_year': b_year,
            'age_when_born': age_when_born,
            'birthplace_region': birthplace_region,
            'birthplace_detail': birthplace_detail,
            'needs_update': needs_update,
            'is_ambiguous': is_ambiguous,
            'candidates': candidates
        }

        node_is_english = is_english_name(display_name)
        traverse(node_index * 2, sire_of_h, b_year, True, node_is_english)
        traverse(node_index * 2 + 1, dam_of_h, b_year, False, node_is_english)

    traverse(2, sire_name, base_birth_year, True, base_is_english)
    traverse(3, dam_name, base_birth_year, False, base_is_english)

    return pedigree

# ==============================================================================
# ルーティング (Controllers)
# ==============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        next_url = request.form.get('next_url')
        
        with sqlite3.connect(UsersDB) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT password FROM users WHERE username = ?", (username,))
            user = cursor.fetchone()
            
            if user and check_password_hash(user[0], password):
                session.clear()
                session.permanent = True
                session['login'] = True
                session['username'] = username

                if next_url and '/login' not in next_url:
                    return redirect(next_url)
                return redirect(url_for('index'))
            else:
                flash('ユーザー名またはパスワードが違います。')

    next_url = request.args.get('next')
    if not next_url and request.referrer and '/login' not in request.referrer:
        next_url = request.referrer
                
    return render_template('login.html', next_url=next_url)

@app.route('/logout')
def logout():
    session.clear()
    flash('ログアウトしました。')
    return redirect(url_for('login'))

@app.route('/')
@wait_for_sheets
def index():
    stable_filter = request.args.get('stable', '')
    raw_results = get_all_horses()
    
    results = []
    for h in raw_results:
        horse = list(h)
        # 状態が「抹消」の馬は一覧に表示しない
        if len(horse) > H_STATUS and str(horse[H_STATUS] or '').strip() == '抹消':
            continue
        if len(horse) > H_CLASS:
            horse[H_CLASS] = current_horse_class(horse)
        if len(horse) > H_BIRTH:
            birth = parse_full_date(horse[H_BIRTH])
            horse[H_BIRTH] = datetime(birth.year, birth.month, birth.day) if birth else datetime.now()
        results.append(horse)

    if stable_filter:
        results = [h for h in results if len(h) > H_STABLE and h[H_STABLE] == stable_filter]
        
    # URLボタンが出ている馬（JRA・NARのURLが無い馬）の数：JRAから登録するボタンに表示する
    missing_url_count = len([h for h in results
                             if not any(len(h) > c and str(h[c] or '').strip() for c in (H_JRA_URL, H_NAR_URL))])
    return render_template('index.html', 
                           missing_url_count=missing_url_count,
                           results=results, 
                           stables=get_stables_list(), 
                           current_year=datetime.now().year,
                           last_updated=get_latest_update_label())

@app.route('/api/fetch_jra_horse')
@login_required
def api_fetch_jra_horse():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({"error": "URLを入力してください。"}), 400
    try:
        data = fetch_jra_horse_info(url)
        if not data:
            return jsonify({"error": "ページから情報を取得できませんでした。"}), 404
        if data.get('kaku'):
            # 地方・海外所属の馬はJRAのタブでは登録できないようにする
            return jsonify({"error": JRA_KAKU_ERROR, "kaku": data['kaku']}), 400
        return jsonify(data), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"ページの取得に失敗しました: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"解析中にエラーが発生しました: {e}"}), 500

# --- 地方競馬情報サイト（keiba.go.jp）の競走馬情報ページ ---
NAR_ALLOWED_HOSTS = ("keiba.go.jp",)

def normalize_nar_company_name(text):
    """馬主名・生産牧場名の表記を揃える：（）→()、株式会社→(株)、有限会社→(有)、
    ）の後のスペースは除去し、他の全角スペースは半角スペースにする"""
    text = (text or '').replace('（', '(').replace('）', ')')
    text = text.replace('株式会社', '(株)').replace('有限会社', '(有)')
    text = re.sub(r'\)[\s\u3000]+', ')', text)
    text = text.replace('\u3000', ' ')
    return re.sub(r' {2,}', ' ', text).strip()

def normalize_nar_owner_name(text):
    """馬主名：全角スペースが2個以上続くところを半角スペース1個にし、他の全角スペースは除去する
    （例：「尾　田　　信　夫」→「尾田 信夫」）。そのあと生産牧場と同じ表記の統一を行う"""
    text = re.sub(r'\u3000{2,}', ' ', (text or '').strip())
    text = text.replace('\u3000', '')
    return normalize_nar_company_name(text)

def parse_nar_horse_page(soup):
    """地方競馬情報サイトの競走馬情報ページから、父・母・生年月日・馬主名・産地・生産牧場を取り出す"""
    def clean(el):
        return el.get_text(' ', strip=True).strip() if el else ''

    def raw_text(el):
        # 馬主名の全角スペースはそのまま残す（前後の空白・改行だけ除く）
        return el.get_text('', strip=False).strip(' \t\r\n') if el else ''

    def value_after_label(label):
        for td in soup.find_all('td'):
            if td.get_text(strip=True) == label:
                return td.find_next_sibling('td')
        return None

    result = {}
    # 馬名：<h4 class="odd_title">ミスエンピリカル</h4>（取得した馬が画面の馬名と合っているかの確認に使う）
    name_el = soup.find('h4', class_='odd_title')
    if clean(name_el):
        result['name'] = clean(name_el)
    sire_td = soup.find('td', class_='fathername')
    if clean(sire_td):
        result['sire'] = clean(sire_td)
    dam_td = value_after_label('母')
    if clean(dam_td):
        result['dam'] = clean(dam_td)

    m = re.search(r'(\d{4})\D+(\d{1,2})\D+(\d{1,2})', clean(value_after_label('生年月日')))
    if m:
        result['birth_year'], result['birth_month'], result['birth_day'] = (
            str(int(m.group(1))), str(int(m.group(2))), str(int(m.group(3))))

    owner = normalize_nar_owner_name(raw_text(value_after_label('馬主')))
    if owner:
        result['owner'] = owner
    birthplace = clean(value_after_label('産地')).replace(' ', '')
    if birthplace:
        result['birthplace'] = birthplace
    breeder = normalize_nar_company_name(raw_text(value_after_label('生産牧場')))
    if breeder:
        result['breeder'] = breeder
    return result

def fetch_nar_horse_info(url):
    from urllib.parse import urlparse
    parsed = urlparse(url or '')
    if parsed.scheme not in ("http", "https") or not any(
            parsed.netloc == h or parsed.netloc.endswith('.' + h) for h in NAR_ALLOWED_HOSTS):
        raise ValueError("地方競馬情報サイト（keiba.go.jp）のURLを入力してください。")
    resp = requests.get(url, headers=_JBIS_HEADERS, timeout=25)
    resp.raise_for_status()
    return parse_nar_horse_page(BeautifulSoup(resp.content, 'html.parser'))

# ==============================================================================
# レース結果（年ごとのファイル「{年}_レース結果」：レースシート＝1レース1行、成績シート＝1頭1行）
# ==============================================================================
RESULT_BOOK_NAME = "{year}_レース結果"
RACE_SHEET = "レース"
RUNNER_SHEET = "成績"
RACE_SHEET_COLUMNS = ["レースID", "年月日", "場", "回", "日", "R", "レース名", "グレード", "区分", "年齢条件",
                      "クラス", "記号", "重量", "距離", "コース", "コース詳細", "発走時刻", "天候", "芝", "ダート",
                      "頭数", "1着本賞金", "2着本賞金", "3着本賞金", "4着本賞金", "5着本賞金", "JRAのURL"]
RUNNER_SHEET_COLUMNS = ["レースID", "年月日", "場", "R", "レース名", "グレード", "区分", "年齢条件", "クラス",
                        "記号", "距離", "コース", "馬場", "頭数", "着順", "枠", "馬番", "馬名", "馬ID", "性別",
                        "年齢", "斤量", "減量", "騎手", "タイム", "着差", "通過順", "上り", "馬体重", "増減",
                        "調教師", "人気", "本賞金"]
RUNNER_COL = {name: i for i, name in enumerate(RUNNER_SHEET_COLUMNS)}
_result_book_keys = {}   # 年 → スプレッドシートのID（開くたびにファイルを検索しないよう覚えておく）
_result_book_missing = {}  # 年 → 見つからなかった時刻（無いファイルを何度も探さないよう、しばらく覚えておく）
RESULT_BOOK_MISSING_TTL = 600

def get_result_book(year, create=False):
    """年ごとのレース結果のファイルを開く。無ければ create=True の時だけ作成し、Horse_Dataと同じ人に共有する。
    無くて作らない場合は None を返す"""
    year = int(year)
    if year in _result_book_keys:
        try:
            return gc.open_by_key(_result_book_keys[year])
        except Exception:
            _result_book_keys.pop(year, None)
    name = RESULT_BOOK_NAME.format(year=year)
    if not create and time.time() - _result_book_missing.get(year, 0) < RESULT_BOOK_MISSING_TTL:
        return None
    try:
        book = gc.open(name)
    except gspread.SpreadsheetNotFound:
        if not create:
            _result_book_missing[year] = time.time()
            return None
        try:
            book = gc.create(name)
        except Exception as e:
            raise RuntimeError(
                f"「{name}」を作成できませんでした（{e}）。Googleドライブで「{name}」という名前のスプレッドシートを作り、"
                f"Horse_Dataと同じように共有してから、もう一度実行してください。")
        # Horse_Dataを共有している人にも同じように共有する（作成したファイルを見られるように）
        try:
            for perm in gc.open(horse_data).list_permissions():
                if perm.get('type') == 'user' and perm.get('emailAddress') and perm.get('role') in ('owner', 'writer'):
                    book.share(perm['emailAddress'], perm_type='user', role='writer', notify=False)
        except Exception as e:
            print(f"レース結果のファイルの共有に失敗しました: {e}")
    _result_book_keys[year] = book.id
    _result_book_missing.pop(year, None)
    return book

def _result_sheet(book, title, columns):
    try:
        ws = book.worksheet(title)
    except gspread.WorksheetNotFound:
        if title == RACE_SHEET and len(book.worksheets()) == 1 and book.sheet1.title in ('Sheet1', 'シート1'):
            ws = book.sheet1
            ws.update_title(title)
        else:
            ws = book.add_worksheet(title=title, rows=1000, cols=len(columns))
        ensure_columns(ws, len(columns))
        ws.update(range_name='A1', values=[columns])
    return ws

_result_sheets_cache = {}   # 年 → (レースシート, 成績シート)
_imported_ids_cache = {}    # 年 → (読み込んだ時刻, 取り込み済みのレースIDの集合)
IMPORTED_IDS_TTL = 600

def result_sheets(year, create=False):
    """年ごとのファイルのレースシート・成績シートを返す（ファイルが無く作らない場合は None）。
    続けて取り込む時にシートを開き直さない（Google SheetsのAPIの回数を減らす）よう覚えておく"""
    year = int(year)
    if year in _result_sheets_cache:
        return _result_sheets_cache[year]
    book = get_result_book(year, create=create)
    if not book:
        return None
    sheets = (_result_sheet(book, RACE_SHEET, RACE_SHEET_COLUMNS),
              _result_sheet(book, RUNNER_SHEET, RUNNER_SHEET_COLUMNS))
    _result_sheets_cache[year] = sheets
    return sheets

def forget_result_sheets(year):
    _result_sheets_cache.pop(int(year), None)
    _imported_ids_cache.pop(int(year), None)
    _result_book_keys.pop(int(year), None)

def with_sheets_retry(func, *args, **kwargs):
    """Google SheetsのAPIの回数制限（429）にかかった場合は、少し待ってやり直す"""
    for attempt in range(4):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status != 429 or attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))

def race_sheet_row(race):
    prizes = list(race.get('prizes') or []) + [''] * 5
    return [race['race_id'], race['date'].strftime('%Y%m%d'), race['venue'], race['kai'], race['nichi'], race['num'],
            race['name'], race['grade'], '障害' if race['jump'] else '平地', race['category'], race['class'],
            race['rule'], race['weight_rule'], race['distance'], race['surface'], race['course_detail'], race['time'],
            race['weather'], race['turf'], race['dirt'], race['runners_count']] + prizes[:5] + [race['url']]

def runner_sheet_row(race, r):
    going = race['turf'] if race['surface'] == '芝' else (race['dirt'] if race['surface'] == 'ダ'
                                                         else '/'.join(x for x in (race['turf'], race['dirt']) if x))
    return [race['race_id'], race['date'].strftime('%Y%m%d'), race['venue'], race['num'], race['name'], race['grade'],
            '障害' if race['jump'] else '平地', race['category'], race['class'], race['rule'], race['distance'],
            race['surface'], going, race['runners_count'], r['place'], r['waku'], r['num'], r['name'], r['horse_id'],
            r['sex'], r['age'], r['weight'], r['mark'], r['jockey'], r['time'], r['margin'], r['corners'], r['last'],
            r['body_weight'], r['body_diff'], r['trainer'], r['pop'], r['prize']]

def save_race_results(parsed_list, overwrite=False):
    """読み取ったレース結果を年ごとのファイルに書き込む。取り込み済みのレースは飛ばす（overwrite=True なら入れ替える）。
    (取り込んだレース, 飛ばしたレース) のラベル一覧を返す"""
    saved, skipped, saved_ids = [], [], set()
    by_year = {}
    for parsed in parsed_list:
        by_year.setdefault(parsed['race']['date'].year, []).append(parsed)
    for year, items in sorted(by_year.items()):
        try:
            ws_race, ws_runner = result_sheets(year, create=True)
            existing = set(with_sheets_retry(ws_race.col_values, 1)[1:])
        except Exception:
            # シートが消された場合などに備えて、覚えているシートを忘れてから開き直す
            forget_result_sheets(year)
            ws_race, ws_runner = result_sheets(year, create=True)
            existing = set(with_sheets_retry(ws_race.col_values, 1)[1:])
        race_rows, runner_rows, replace_ids, this_time = [], [], set(), set()
        for parsed in items:
            race = parsed['race']
            label = f"{race['date'].month}/{race['date'].day} {race['venue']}{race['num']}R"
            if not race['race_id']:
                skipped.append(f"{label}（レースIDを読み取れません）")
                continue
            if race['race_id'] in this_time:
                continue   # 同じレースが2回指定された場合
            if race['race_id'] in existing and not overwrite:
                skipped.append(f"{label}（取り込み済み）")
                continue
            if race['race_id'] in existing:
                replace_ids.add(race['race_id'])
            existing.add(race['race_id'])
            this_time.add(race['race_id'])
            race_rows.append(race_sheet_row(race))
            runner_rows.extend(runner_sheet_row(race, r) for r in parsed['runners'])
            saved.append(label)
            saved_ids.add(race['race_id'])
        if replace_ids:
            # 入れ替えるレースの古い行を消す（下の行から消して行番号がずれないようにする）
            # 同じレースの行は続いているため、続いている行はまとめて消す
            for ws in (ws_race, ws_runner):
                ids = ws.col_values(1)
                blocks = []
                for idx in range(2, len(ids) + 1):
                    if ids[idx - 1] in replace_ids:
                        if blocks and blocks[-1][1] == idx - 1:
                            blocks[-1][1] = idx
                        else:
                            blocks.append([idx, idx])
                for start, end in reversed(blocks):
                    ws.delete_rows(start, end)
        # すべて文字列のまま書き込む（数字の列も文字列にしておくと、成績シートのクエリで型が混ざらない）
        if race_rows:
            with_sheets_retry(ws_race.append_rows, [[str(v) for v in row] for row in race_rows],
                              value_input_option='RAW')
        if runner_rows:
            with_sheets_retry(ws_runner.append_rows, [[str(v) for v in row] for row in runner_rows],
                              value_input_option='RAW')
        _imported_ids_cache[year] = (time.time(), existing)
    if saved_ids:
        # 出走した登録馬のクラス・最新レースを計算し直す
        update_class_for_runners([p for p in parsed_list if p['race']['race_id'] in saved_ids])
    return saved, skipped

def fetch_jra_html(url, cname=None):
    """JRAのページを取得してHTMLを返す（cname を指定すると、ページのボタンと同じようにPOSTで開く）"""
    from urllib.parse import urlparse
    if not any(urlparse(url).netloc.endswith(h) for h in JRA_ALLOWED_HOSTS):
        raise ValueError("JRAのURLではありません。")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; keiba.log/1.0)"}
    if cname:
        resp = requests.post(url, data={'cname': cname}, headers=headers, timeout=20)
    else:
        resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.content.decode('cp932', errors='replace')

def fetch_race_results(urls):
    """レース結果のURLの一覧を取得して読み取る。(読み取った結果, 失敗の一覧) を返す"""
    from concurrent.futures import ThreadPoolExecutor

    def _one(url):
        try:
            return RR.parse_race_result_page(fetch_jra_html(url), url=url), None
        except Exception as e:
            return None, f"{url}（{e}）"

    parsed, failed = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result, err in pool.map(_one, urls):
            if result:
                parsed.append(result)
            if err:
                failed.append(err)
    return parsed, failed

def _gviz_quote(text):
    return '"' + str(text).replace('"', '') + '"'

def query_runner_rows(book, horse_ids, horse_names):
    """成績シートから、馬IDまたは馬名が一致する行だけを取り出す（Googleのクエリ機能で絞り込み、全行は読まない）"""
    import csv, io
    horse_ids = sorted({i for i in horse_ids if i})
    horse_names = sorted({n for n in horse_names if n})
    if not horse_ids and not horse_names:
        return []
    id_col, name_col = col_letter(RUNNER_COL['馬ID']), col_letter(RUNNER_COL['馬名'])
    conds = [f"{id_col} = {_gviz_quote(i)}" for i in horse_ids] + [f"{name_col} = {_gviz_quote(n)}" for n in horse_names]
    try:
        session = gc.http_client.session
        resp = session.get(f"https://docs.google.com/spreadsheets/d/{book.id}/gviz/tq",
                           params={'tqx': 'out:csv', 'sheet': RUNNER_SHEET, 'headers': '1',
                                   'tq': 'select * where ' + ' or '.join(conds)}, timeout=30)
        resp.raise_for_status()
        rows = list(csv.reader(io.StringIO(resp.content.decode('utf-8'))))
        if rows and rows[0] and rows[0][0] != RUNNER_SHEET_COLUMNS[0]:
            raise ValueError('クエリの結果の形式が違います')
        return [r for r in rows[1:] if r and r[0]]
    except Exception as e:
        # クエリが使えない場合は成績シートを全部読んで絞り込む
        print(f"成績シートのクエリに失敗したため全件を読み込みます: {e}")
        ids, names = set(horse_ids), set(horse_names)
        rows = book.worksheet(RUNNER_SHEET).get_all_values()[1:]
        return [r for r in rows if (len(r) > RUNNER_COL['馬ID'] and r[RUNNER_COL['馬ID']] in ids)
                or (len(r) > RUNNER_COL['馬名'] and r[RUNNER_COL['馬名']] in names)]

def runner_row_to_result(r):
    """成績シートの1行を出走成績（表示・収得賞金の計算用）にする"""
    r = list(r) + [''] * len(RUNNER_SHEET_COLUMNS)
    c = lambda name: str(r[RUNNER_COL[name]]).strip()
    race_date = parse_full_date(c('年月日'))
    if not race_date:
        return None
    prize = c('本賞金').replace(',', '')
    return {
        'race_id': c('レースID'), 'date': race_date, 'date_label': format_date_jp(race_date, weekday=True),
        'venue': c('場'), 'num': c('R'), 'name': c('レース名'), 'grade': c('グレード'),
        'jump': c('区分') == '障害', 'category': c('年齢条件'), 'class': c('クラス'), 'rule': c('記号'),
        'distance': c('距離'), 'surface': c('コース'), 'going': c('馬場'), 'runners': c('頭数'),
        'place': c('着順'), 'waku': c('枠'), 'horse_num': c('馬番'), 'jockey': c('騎手'), 'weight': c('斤量'),
        'mark': c('減量'), 'time': c('タイム'), 'margin': c('着差'), 'corners': c('通過順'), 'last': c('上り'),
        'body_weight': c('馬体重'), 'body_diff': c('増減'), 'pop': c('人気'),
        'prize': int(prize) if prize.isdigit() else 0,
        'horse_name': c('馬名'), 'horse_id': c('馬ID'),
    }

def collect_race_results(horses):
    """複数の馬の出走成績をまとめて集める。horses：[{'key', 'name', 'horse_id', 'birth_year'}]。
    {key: 出走成績の一覧（新しい順）} を返す"""
    if not horses:
        return {}
    this_year = datetime.now().year
    first_year = min(((h['birth_year'] or 2015) + 2) for h in horses)
    by_id = {h['horse_id']: h['key'] for h in horses if h.get('horse_id')}
    by_name = {}
    for h in horses:
        if h.get('name'):
            by_name.setdefault(h['name'], []).append(h['key'])
    found = {h['key']: {} for h in horses}
    for year in range(max(first_year, 2000), this_year + 1):
        book = get_result_book(year)
        if not book:
            continue
        # 馬IDで探し、馬IDが分からない馬は馬名で探す
        ids = [h['horse_id'] for h in horses if h.get('horse_id')]
        names = [h['name'] for h in horses if not h.get('horse_id') and h.get('name')]
        for i in range(0, max(len(ids), len(names)), 40):
            for row in query_runner_rows(book, ids[i:i + 40], names[i:i + 40]):
                res = runner_row_to_result(row)
                if not res:
                    continue
                keys = [by_id[res['horse_id']]] if res['horse_id'] in by_id else by_name.get(res['horse_name'], [])
                for key in keys:
                    found[key].setdefault(res['race_id'] or (res['date'], res['venue'], res['num']), res)
    order = lambda r: (r['date'], int(r['num']) if str(r['num']).isdigit() else 0)
    return {key: sorted(v.values(), key=order, reverse=True) for key, v in found.items()}

def get_horse_race_results(horse_name, horse_id='', birth_year=None):
    """競走馬の出走成績（新しい順）と、本賞金・収得賞金・クラスのまとめを返す"""
    results = collect_race_results([{'key': 0, 'name': horse_name, 'horse_id': horse_id,
                                     'birth_year': birth_year}])[0]
    summary = RR.summarize_results(results, birth_year=birth_year)
    return results, summary

def class_and_shutoku(summary, results):
    """Horsesシートに記録するクラスと収得賞金：最新レースが平地なら平地の、障害なら障害のもの"""
    kind = 'jump' if results and results[0]['jump'] else 'flat'
    return summary[kind]['class'], summary[kind]['shutoku']

def current_horse_class(row, today=None):
    """Horsesシートの行から現在のクラスを返す。記録したクラスが平地の条件クラス・オープンなら、
    記録した収得賞金から計算し直す（2歳・3歳の5月までは1勝クラスの次がオープンのため、6月になると変わる）"""
    cls = str(row[H_CLASS]).strip() if len(row) > H_CLASS and row[H_CLASS] else ''
    amount = str(row[H_SHUTOKU]).strip().replace(',', '') if len(row) > H_SHUTOKU and row[H_SHUTOKU] else ''
    if cls in ('1勝クラス', '2勝クラス', '3勝クラス', 'オープン') and amount.isdigit():
        birth_year = date_year(row[H_BIRTH]) if len(row) > H_BIRTH else None
        return RR.class_from_shutoku(int(amount), True, False, birth_year, today)
    return cls

def last_race_label(results):
    """Horsesシートに記録する最新レース（例：2026/10/4 東京5R メイクデビュー東京 1着）"""
    if not results:
        return ''
    r = results[0]
    d = r['date']
    place = f"{r['place']}着" if str(r['place']).isdigit() else r['place']
    name = r['name'] or f"{r['category']}{r['class']}"
    return f"{d.year}/{d.month}/{d.day} {r['venue']}{r['num']}R {name} {place}".strip()

def update_horse_class_columns(match=None, offset=0, limit=None):
    """Horsesシートのクラス・最新レースの列を、取り込んだレース結果から計算し直す。
    match：対象を絞る関数（Horsesの行を受け取り True なら対象）。offset・limit：対象のうち何頭目から何頭か。
    (対象の頭数, 更新した頭数) を返す"""
    ws = gc.open(horse_data).worksheet("Horses")
    data = with_sheets_retry(ws.get_all_values)
    rows = data[1:]
    targets = []
    for i, r in enumerate(rows):
        name = display_name_of_row(r)
        if not name or (match and not match(r)):
            continue
        targets.append((i, r, name))
    total = len(targets)
    targets = targets[offset:offset + limit] if limit else targets[offset:]
    if not targets:
        return total, 0
    horses = []
    for i, r, name in targets:
        horses.append({'key': i, 'name': name,
                       'horse_id': RR.horse_id_from_url(r[H_JRA_URL] if len(r) > H_JRA_URL else ''),
                       'birth_year': date_year(r[H_BIRTH]) if len(r) > H_BIRTH else None})
    results = collect_race_results(horses)
    updates = []
    for h in horses:
        res = results.get(h['key'], [])
        r = rows[h['key']]
        if res:
            new_class, shutoku = class_and_shutoku(RR.summarize_results(res, birth_year=h['birth_year']), res)
            shutoku = str(shutoku)
        else:
            new_class = '新馬' if h['birth_year'] and datetime.now().year - h['birth_year'] <= 3 else ''
            shutoku = ''
        new_values = [new_class, last_race_label(res), shutoku]
        old_values = [str(r[c]) if len(r) > c else '' for c in (H_CLASS, H_LAST_RACE, H_SHUTOKU)]
        if new_values != old_values:
            row_no = h['key'] + 2
            updates.append({'range': f"{col_letter(H_CLASS)}{row_no}:{col_letter(H_SHUTOKU)}{row_no}",
                            'values': [new_values]})
    updated = len(updates)
    header = data[0] if data else []
    if updates or len(header) <= H_SHUTOKU or header[H_SHUTOKU] != HORSE_COLUMNS[H_SHUTOKU]:
        ensure_columns(ws, len(HORSE_COLUMNS))
        updates.append({'range': f"{col_letter(H_CLASS)}1:{col_letter(H_SHUTOKU)}1",
                        'values': [[HORSE_COLUMNS[H_CLASS], HORSE_COLUMNS[H_LAST_RACE], HORSE_COLUMNS[H_SHUTOKU]]]})
        with_sheets_retry(ws.batch_update, updates)
    return total, updated

def update_class_for_runners(parsed_list):
    """取り込んだレースに出走した登録馬（Horses）のクラス・最新レースを計算し直す"""
    ids, names = set(), set()
    for parsed in parsed_list:
        for r in parsed['runners']:
            if r.get('horse_id'):
                ids.add(r['horse_id'])
            if r.get('name'):
                names.add(r['name'])
    if not ids and not names:
        return
    def _match(row):
        hid = RR.horse_id_from_url(row[H_JRA_URL] if len(row) > H_JRA_URL else '')
        return (hid and hid in ids) or display_name_of_row(row) in names
    try:
        update_horse_class_columns(_match)
    except Exception as e:
        print(f"クラス・最新レースの更新に失敗しました: {e}")

def imported_race_ids(year):
    """その年のレースシートに取り込み済みのレースIDの集合（しばらくの間は覚えておいた結果を返す）"""
    year = int(year)
    cached = _imported_ids_cache.get(year)
    if cached and time.time() - cached[0] < IMPORTED_IDS_TTL:
        return cached[1]
    try:
        sheets = result_sheets(year)
        ids = set(with_sheets_retry(sheets[0].col_values, 1)[1:]) if sheets else set()
    except Exception:
        forget_result_sheets(year)
        sheets = result_sheets(year)
        ids = set(with_sheets_retry(sheets[0].col_values, 1)[1:]) if sheets else set()
    if sheets:
        _imported_ids_cache[year] = (time.time(), ids)
    return ids

JRA_RESULT_MENU_ACTION = "https://www.jra.go.jp/JRADB/accessS.html"
JRA_RESULT_MENU_CNAME = "pw01skl00999999/B3"   # 競馬メニューの「レース結果」（開催選択のページ）

def fetch_live_kaisai_html():
    """JRAのサイトから「レース結果」の開催選択のページを取得する"""
    action, cname = JRA_RESULT_MENU_ACTION, JRA_RESULT_MENU_CNAME
    try:
        # 競馬メニューの「レース結果」のリンクと同じ操作で開く（リンク先が変わっていても追従できるように）
        menu = BeautifulSoup(fetch_jra_html("https://www.jra.go.jp/keiba/"), 'html.parser')
        for a in menu.find_all('a', onclick=True):
            m = re.search(r"doAction\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", a['onclick'])
            if m and a.get_text(strip=True) == 'レース結果' and 'accessS' in m.group(1):
                action, cname = 'https://www.jra.go.jp' + m.group(1), m.group(2)
                break
    except Exception as e:
        print(f"競馬メニューの取得に失敗したため、既定のリンク先を使います: {e}")
    return fetch_jra_html(action, cname=cname)

def kaisai_day_key(label, race_date):
    """開催選択のページの「4回東京2日」と日付から、レースIDの開催日の部分を作る"""
    m = re.fullmatch(r'(\d+)回(\S+?)(\d+)日', label or '')
    code = next((c for c, v in RR.VENUES.items() if m and v == m.group(2)), None)
    if not m or not code or not race_date:
        return ''
    return f"{code}{race_date.year}{int(m.group(1)):02d}{int(m.group(3)):02d}{race_date.strftime('%Y%m%d')}"

def scan_result_pages(html_list):
    """アップロード（またはJRAから取得）したページから、取り込む開催日の一覧を作る"""
    days = {}
    today = datetime.now().date()
    for html in html_list:
        soup = BeautifulSoup(html, 'html.parser')
        for item in RR.parse_kaisai_page(soup, today=today):
            key = kaisai_day_key(item['label'], item['date'])
            if key:
                days.setdefault(key, {'key': key, 'url': ''})
        for key, urls in RR.result_urls_by_day(soup).items():
            days.setdefault(key, {'key': key, 'url': ''})
            if not days[key]['url']:
                days[key]['url'] = urls[0]
    if not days:
        return []
    imported = {}
    for key in days:
        year = int(key[2:6])
        if year not in imported:
            imported[year] = {}
            for rid in imported_race_ids(year):
                k = RR.day_key(rid)
                imported[year][k] = imported[year].get(k, 0) + 1
    result = []
    for key, d in days.items():
        rid = f"pw01sde10{key[:10]}00{key[10:]}"
        info = RR.parse_race_id(rid)
        if not info:
            continue
        result.append({'key': key, 'url': d['url'], 'date': info['date'].isoformat(),
                       'label': RR.day_label(rid), 'imported': imported[int(key[2:6])].get(key, 0)})
    result.sort(key=lambda d: (d['date'], d['key']), reverse=True)
    return result

def import_result_day(url='', via='', key='', overwrite=False):
    """1開催日（1競馬場）の全レースの結果を取得して保存する。
    url が無い場合は、同じ週の別の日のレース結果のページ（via）から、その日へのリンクを探す"""
    discovered = {}

    def _discover(html):
        for k, urls in RR.result_urls_by_day(html).items():
            discovered.setdefault(k, urls[0])

    if not url:
        if not via:
            raise ValueError('この開催日のレース結果へのリンクが見つかりません。')
        via_html = fetch_jra_html(via)
        _discover(via_html)
        url = discovered.get(key, '')
        if not url:
            return {'found': False, 'discovered': discovered}
    html = fetch_jra_html(url)
    _discover(html)
    first = RR.parse_race_result_page(html, url=url)
    key = RR.day_key(first['race']['race_id']) or key
    day_urls = RR.result_urls_by_day(html).get(key) or [url]
    existing = imported_race_ids(first['race']['date'].year)
    todo = [u for u in day_urls if RR.race_id_from_cname(u) != first['race']['race_id']
            and (overwrite or RR.race_id_from_cname(u) not in existing)]
    parsed, failed = fetch_race_results(todo)
    if overwrite or first['race']['race_id'] not in existing:
        parsed.insert(0, first)
    already = len([u for u in day_urls if RR.race_id_from_cname(u) in existing]) if not overwrite else 0
    saved, skipped = save_race_results(parsed, overwrite=overwrite)
    return {'found': True, 'key': key, 'label': RR.day_label(first['race']['race_id']), 'races': len(day_urls),
            'saved': len(saved), 'already': already, 'skipped': skipped, 'failed': failed,
            'discovered': discovered, 'unregistered': safe_unregistered_runners(parsed)}

def safe_unregistered_runners(parsed):
    try:
        return unregistered_runners(parsed) if parsed else []
    except Exception as e:
        print(f"未登録の出走馬の確認に失敗しました: {e}")
        return []

def race_link_info(url):
    """レース結果のURLから、表示用の情報と取り込み済みかどうかを返す"""
    rid = RR.race_id_from_cname(url)
    info = RR.parse_race_id(rid)
    if not info:
        return None
    d = info['date']
    return {'url': url, 'race_id': rid, 'day_key': RR.day_key(rid), 'date': d.isoformat(),
            'label': f"{d.year}年{d.month}月{d.day}日 {info['venue']}{info['num']}R",
            'imported': rid in imported_race_ids(d.year)}

@app.route('/api/race_results/horse_targets', methods=['POST'])
@login_required
def api_race_results_horse_targets():
    """JRAのURLが登録されている馬の一覧（Horses・Sire・Damシートから、指定されたシートのみ）"""
    data = request.get_json(silent=True) or {}
    wanted = set(data.get('sheets') or ['Horses'])
    keyword = str(data.get('keyword') or '').strip()
    sh = gc.open(horse_data)
    targets, seen = [], set()
    for title, name_col, en_col, url_col in (('Horses', H_NAME, H_NAME_EN, H_JRA_URL),
                                             ('Sire', P_NAME, P_NAME_EN, P_JRA_URL),
                                             ('Dam', P_NAME, P_NAME_EN, P_JRA_URL)):
        if title not in wanted:
            continue
        try:
            rows = sh.worksheet(title).get_all_values()[1:]
        except gspread.WorksheetNotFound:
            continue
        for r in rows:
            url = r[url_col].strip() if len(r) > url_col else ''
            if not url or 'jra.go.jp' not in url or url in seen:
                continue
            name = display_name_of_row(r) if title == 'Horses' else (
                (r[name_col] if len(r) > name_col else '') or (r[en_col] if len(r) > en_col else ''))
            if keyword and keyword not in name and keyword not in (r[en_col] if len(r) > en_col else ''):
                continue
            seen.add(url)
            targets.append({'name': name, 'sheet': title, 'url': url})
    return jsonify({'targets': targets})

@app.route('/api/race_results/horse_races', methods=['POST'])
@login_required
def api_race_results_horse_races():
    """JRAの競走馬情報のページの「出走レース」から、各レースのレース結果のURLと取り込み済みかどうかを返す"""
    data = request.get_json(silent=True) or {}
    try:
        urls = RR.parse_horse_race_urls(fetch_jra_html(str(data.get('url') or '')))
        races = [info for info in (race_link_info(u) for u in urls) if info]
    except Exception as e:
        return jsonify({'error': f'競走馬のページを読み取れませんでした: {e}'}), 400
    return jsonify({'races': races})

@app.route('/api/race_results/import_races', methods=['POST'])
@login_required
def api_race_results_import_races():
    """指定したレースだけの結果を取得して保存する（同じ日の他のレースは取り込まない）"""
    data = request.get_json(silent=True) or {}
    urls = [u for u in (data.get('urls') or []) if RR.race_id_from_cname(u)][:20]
    overwrite = bool(data.get('overwrite'))
    try:
        if not overwrite:
            urls = [u for u in urls if not (race_link_info(u) or {}).get('imported')]
        parsed, failed = fetch_race_results(urls)
        saved, skipped = save_race_results(parsed, overwrite=overwrite)
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'saved': len(saved), 'skipped': skipped, 'failed': failed,
                    'unregistered': safe_unregistered_runners(parsed)})

# --- 開催日程（競馬番組）：年ごとのファイルの「番組」シート（1レース1行） ---
PROGRAM_SHEET = "番組"
PROGRAM_COLUMNS = ["年月日", "場", "回", "日", "R", "レース名", "グレード", "区分", "年齢条件", "クラス", "条件",
                   "距離", "コース", "コース詳細", "記号", "重量", "発走時刻"]
PROGRAM_COL = {name: i for i, name in enumerate(PROGRAM_COLUMNS)}
_program_cache = {}   # 年 → (読み込んだ時刻, 行の一覧)
PROGRAM_CACHE_TTL = 300

def program_sheet_row(race):
    return [race['date'].strftime('%Y%m%d'), race['venue'], race['kai'], race['nichi'], race['num'], race['name'],
            race['grade'], '障害' if race['jump'] else '平地', race['category'], race['class'], race['condition'],
            race['distance'], race['surface'], race['course_detail'], race['marks'], race['weight_rule'], race['time']]

def save_program(races):
    """番組を保存する。同じ日の番組が既にあれば入れ替える（番組は変更されることがあるため）。保存した日の一覧を返す"""
    by_year = {}
    for race in races:
        by_year.setdefault(race['date'].year, []).append(race)
    saved_dates = set()
    for year, items in sorted(by_year.items()):
        book = get_result_book(year, create=True)
        ws = _result_sheet(book, PROGRAM_SHEET, PROGRAM_COLUMNS)
        dates = {r['date'].strftime('%Y%m%d') for r in items}
        col = with_sheets_retry(ws.col_values, 1)
        blocks = []
        for idx in range(2, len(col) + 1):
            if col[idx - 1] in dates:
                if blocks and blocks[-1][1] == idx - 1:
                    blocks[-1][1] = idx
                else:
                    blocks.append([idx, idx])
        for start, end in reversed(blocks):
            with_sheets_retry(ws.delete_rows, start, end)
        items.sort(key=lambda r: (r['date'], r.get('order', 0), r['num']))
        with_sheets_retry(ws.append_rows, [[str(v) for v in program_sheet_row(r)] for r in items],
                          value_input_option='RAW')
        saved_dates |= {r['date'] for r in items}
        _program_cache.pop(year, None)
    return sorted(saved_dates)

def get_program_rows(year):
    """その年の番組シートの行（しばらくの間は覚えておく）"""
    year = int(year)
    cached = _program_cache.get(year)
    if cached and time.time() - cached[0] < PROGRAM_CACHE_TTL:
        return cached[1]
    rows = []
    book = get_result_book(year)
    if book:
        try:
            rows = book.worksheet(PROGRAM_SHEET).get_all_values()[1:]
        except gspread.WorksheetNotFound:
            rows = []
    rows = [list(r) + [''] * (len(PROGRAM_COLUMNS) - len(r)) for r in rows if r and r[0]]
    _program_cache[year] = (time.time(), rows)
    return rows

def program_race_from_row(r):
    """番組シートの1行を画面表示用にする"""
    c = lambda name: str(r[PROGRAM_COL[name]]).strip()
    dist = c('距離')
    dist_label = f"{int(dist):,}m" if dist.isdigit() else ''
    jump = c('区分') == '障害'
    detail = c('コース詳細')
    course = f"障 {dist_label}（{detail}）" if jump and detail else f"{detail or c('コース')} {dist_label}".strip()
    marks = c('記号')
    num = int(c('R')) if c('R').isdigit() else 0
    return {'date': parse_full_date(c('年月日')), 'venue': c('場'), 'kai': c('回'), 'nichi': c('日'),
            'num': f"{num}レース", 'race_num': num, 'name': c('レース名'), 'grade': c('グレード'), 'jump': jump,
            'category': c('年齢条件'), 'class': c('クラス'), 'condition': c('条件'),
            'course': course, 'distance': dist, 'surface': c('コース'), 'course_detail': detail,
            'marks': marks, 'note': '牝' if '牝' in marks else '', 'weight_rule': c('重量'), 'time': c('発走時刻')}

def get_program_for_dates(dates):
    """指定した日の番組を {日付: {競馬場: {レース番号: レース}}} で返す（競馬場は番組の表示順）"""
    wanted = set(dates)
    result = {}
    for year in sorted({d.year for d in wanted}):
        for r in get_program_rows(year):
            race = program_race_from_row(r)
            if race['date'] in wanted and race['venue']:
                result.setdefault(race['date'], {}).setdefault(race['venue'], {})[race['race_num']] = race
    return result

def find_program_race(race_date, venue, num):
    d = race_date if isinstance(race_date, date) else parse_full_date(race_date)
    if not d:
        return None
    return get_program_for_dates([d]).get(d, {}).get(venue, {}).get(int(num))

def fetch_program_days(start, end):
    """JRAの開催日程の日別ページを期間内の全日分取得して読み取る（開催の無い日はページが無い）"""
    from concurrent.futures import ThreadPoolExecutor
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def _one(d):
        try:
            return d, RR.parse_program_page(fetch_jra_html(RR.program_url(d)), page_date=d), None
        except requests.exceptions.HTTPError as e:
            if getattr(e.response, 'status_code', None) == 404:
                return d, [], None
            return d, [], f"{d.month}/{d.day}（{e}）"
        except Exception as e:
            return d, [], f"{d.month}/{d.day}（{e}）"

    races, failed = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for d, day_races, err in pool.map(_one, days):
            races.extend(day_races)
            if err:
                failed.append(err)
    return races, failed

def _add_program_order(races):
    """番組の表示順（競馬場の並び）を覚えておく"""
    venues = {}
    for r in races:
        order = venues.setdefault(r['date'], [])
        if r['venue'] not in order:
            order.append(r['venue'])
        r['order'] = order.index(r['venue'])
    return races

def program_result_message(saved_dates, failed):
    labels = [f"{d.month}/{d.day}" for d in saved_dates]
    return {'saved_dates': [d.isoformat() for d in saved_dates], 'labels': labels, 'failed': failed}

@app.route('/api/program/fetch', methods=['POST'])
@login_required
def api_program_fetch():
    """期間（最大14日）の番組をJRAの開催日程のページから取得して保存する"""
    data = request.get_json(silent=True) or {}
    start, end = parse_full_date(data.get('start')), parse_full_date(data.get('end'))
    if not start or not end or end < start:
        return jsonify({'error': '期間を正しく指定してください。'}), 400
    end = min(end, start + timedelta(days=13))
    try:
        races, failed = fetch_program_days(start, end)
        saved = save_program(_add_program_order(races)) if races else []
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(program_result_message(saved, failed))

@app.route('/api/program/upload', methods=['POST'])
@login_required
def api_program_upload():
    """開催日程の日別ページを保存したファイル（.mhtml／.html、複数可）から番組を取り込む"""
    races, failed = [], []
    for f in request.files.getlist('program_files'):
        if not f or not f.filename:
            continue
        try:
            day_races = RR.parse_program_page(_extract_html_from_upload(f.read()))
            if not day_races:
                failed.append(f"{f.filename}（番組が見つかりません）")
            races.extend(day_races)
        except Exception as e:
            failed.append(f"{f.filename}（{e}）")
    if not races:
        return jsonify({'error': '番組を読み取れませんでした。' + ' '.join(failed)}), 400
    try:
        saved = save_program(_add_program_order(races))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(program_result_message(saved, failed))

@app.route('/api/race_results/update_classes', methods=['POST'])
@login_required
def api_race_results_update_classes():
    """Horsesシートの全馬（抹消を除く）のクラス・最新レースを計算し直す（offset から limit 頭ずつ）"""
    data = request.get_json(silent=True) or {}
    offset, limit = int(data.get('offset') or 0), min(int(data.get('limit') or 40), 100)
    include_cancelled = bool(data.get('include_cancelled'))
    match = None if include_cancelled else (
        lambda r: str(r[H_STATUS] if len(r) > H_STATUS else '').strip() != '抹消')
    try:
        total, updated = update_horse_class_columns(match, offset=offset, limit=limit)
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'total': total, 'next': offset + limit, 'updated': updated})

# ==============================================================================
# JRAの競走馬検索と、未登録の馬の自動登録
# ==============================================================================
JRA_SEARCH_ACTION = "https://www.jra.go.jp/JRADB/accessR.html"
JRA_SEARCH_CNAME = "pw02uliD19999"   # 競馬メニューの「競走馬検索」
_jra_search_form = {}   # 検索のページのフォーム（送り先・項目）を覚えておく

def _js_function_body(js, name):
    """JavaScriptの function name(...) { … } の中身を取り出す"""
    m = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', js)
    if not m:
        return ''
    depth, i = 1, m.end()
    while i < len(js) and depth:
        depth += {'{': 1, '}': -1}.get(js[i], 0)
        i += 1
    return js[m.end():i - 1]

def load_jra_search_form():
    """競走馬検索のページを開き、「検索」ボタン（doSearch）と同じ送信内容（送り先・項目）を調べる"""
    from urllib.parse import urljoin
    if _jra_search_form and time.time() - _jra_search_form.get('time', 0) < 3600:
        return _jra_search_form
    html = fetch_jra_html(JRA_SEARCH_ACTION, cname=JRA_SEARCH_CNAME)
    soup = BeautifulSoup(html, 'html.parser')
    name_input = soup.find('input', attrs={'name': 'iv_h_name'}) or soup.find('input', id='iv_h_name')
    if name_input is None:
        raise ValueError('競走馬検索のページに馬名の入力欄が見つかりません。')
    form = name_input.find_parent('form')
    fields = {}
    for el in (form or soup).find_all(['input', 'select', 'textarea']):
        key = el.get('name')
        if not key:
            continue
        if el.name == 'select':
            opt = el.find('option', selected=True) or el.find('option')
            fields[key] = opt.get('value', opt.get_text(strip=True)) if opt else ''
        elif el.get('type', '').lower() in ('checkbox', 'radio'):
            if el.has_attr('checked'):
                fields[key] = el.get('value', 'on')
        elif el.get('type', '').lower() not in ('button', 'submit', 'image', 'reset'):
            fields[key] = el.get('value', '')
    action = urljoin(JRA_SEARCH_ACTION, form.get('action')) if form is not None and form.get('action') else JRA_SEARCH_ACTION
    # 「検索」ボタンの処理（doSearch）から、送り先と cname を読み取る
    scripts = [sc.get_text() for sc in soup.find_all('script') if not sc.get('src')]
    for sc in soup.find_all('script', src=True):
        src = urljoin(JRA_SEARCH_ACTION, sc['src'])
        if '/JRADB/' in src:
            try:
                scripts.append(requests.get(src, timeout=15).content.decode('cp932', errors='replace'))
            except Exception:
                pass
    body = next((b for b in (_js_function_body(js, 'doSearch') for js in scripts) if b), '')
    m = re.search(r"doAction\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", body)
    if m:
        action, fields['cname'] = urljoin(JRA_SEARCH_ACTION, m.group(1)), m.group(2)
    else:
        am = re.search(r"\.action\s*=\s*['\"]([^'\"]+)['\"]", body)
        if am:
            action = urljoin(JRA_SEARCH_ACTION, am.group(1))
        cm = re.search(r"['\"](pw\d{2}[a-z]{3}[0-9A-Za-z]*(?:/[0-9A-Fa-f]{2})?)['\"]", body)
        if cm:
            fields['cname'] = cm.group(1)
    _jra_search_form.clear()
    _jra_search_form.update({'time': time.time(), 'action': action, 'fields': fields})
    return _jra_search_form

def parse_jra_search_results(html):
    """競走馬検索の結果のページから [{'name', 'url', 'icons', 'kaku', 'sex', 'age', 'trainer', 'area'}] を返す。
    「該当データはありませんでした」の場合は空の一覧"""
    soup = BeautifulSoup(html, 'html.parser')
    results, found_table = [], False
    for table in soup.find_all('table'):
        caption = table.find('caption')
        if not caption or '検索結果' not in caption.get_text():
            continue
        found_table = True
        for tr in table.select('tbody tr'):
            td = tr.select_one('td.horse')
            if not td:
                continue
            a = next((x for x in td.find_all('a') if _jra_link_url(x)), None)
            icons = [img.get('alt', '') for img in td.select('img[alt]')]
            age_text = (tr.select_one('td.age').get_text(strip=True) if tr.select_one('td.age') else '')
            am = re.match(r'(牡|牝|せん|セン|騸)(\d+)', age_text)
            trainer_td = tr.select_one('td.trainer')
            results.append({
                'name': a.get_text(strip=True) if a else td.get_text(strip=True),
                'url': _jra_link_url(a),
                'icons': icons,
                'kaku': next((i for i in icons if i in JRA_KAKU_ICONS), ''),
                'sex': ('せん' if am.group(1) in ('せん', 'セン', '騸') else am.group(1)) if am else '',
                'age': int(am.group(2)) if am else None,
                'trainer': (trainer_td.select_one('.cell.left') or trainer_td).get_text(strip=True) if trainer_td else '',
                'area': (trainer_td.select_one('.icon').get_text(strip=True) if trainer_td and trainer_td.select_one('.icon') else ''),
            })
    if not found_table and '該当データはありませんでした' not in soup.get_text():
        raise ValueError('競走馬検索の結果のページを読み取れませんでした。')
    return results

def search_jra_horses(name):
    """JRAの競走馬検索で馬名を検索する（競馬メニューの「競走馬検索」に馬名を入力して「検索」を押すのと同じ）"""
    form = load_jra_search_form()
    fields = dict(form['fields'])
    fields['iv_h_name'] = name
    data = {k: (v.encode('cp932', errors='replace') if isinstance(v, str) else v) for k, v in fields.items()}
    resp = requests.post(form['action'], data=data,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; keiba.log/1.0)"}, timeout=20)
    resp.raise_for_status()
    try:
        return parse_jra_search_results(resp.content.decode('cp932', errors='replace'))
    except ValueError:
        _jra_search_form.clear()   # ページの形式が変わった場合に備えて、次回は検索のページを読み直す
        raise

def _same_name(a, b):
    norm = lambda t: re.sub(r'[\s　]', '', str(t or '')).lower()
    return bool(norm(a)) and norm(a) == norm(b)

def jra_info_conflicts(info, known):
    """競走馬情報（fetch_jra_horse_info）と、分かっている情報（known）が食い違うか"""
    known = known or {}
    birth = format_date8(info['birth_year'], info['birth_month'], info['birth_day']) if info.get('birth_year') else ''
    kb = str(known.get('birth') or '')
    if kb and birth:
        if len(kb) >= 8 and kb[:8] != birth:
            return True
        if kb[:4] != birth[:4]:
            return True
    if known.get('born_before') and info.get('birth_year') and int(info['birth_year']) >= int(known['born_before']):
        return True
    for key in ('sire', 'dam'):
        k, v = known.get(key), info.get(key)
        if k and v and is_english_name(k) == is_english_name(v) and not _same_name(k, v):
            return True
    if known.get('gender') and info.get('gender'):
        g = {'せん': '牡'}.get(info['gender'], info['gender'])
        if {'せん': '牡'}.get(known['gender'], known['gender']) != g:
            return True
    if known.get('cancel_date') and info.get('cancel_date') and known['cancel_date'] != info['cancel_date']:
        return True
    return False

class AutoRegistrar:
    """未登録の馬をJRAの競走馬情報から登録する（Horsesに無い馬は新規登録、URLが無い馬はURLと空欄の項目を埋める）。
    親（父・母・母の父）も、未登録・情報不足ならリンク先（リンクが無ければ競走馬検索）から登録する"""
    MAX_PARENT_DEPTH = 4

    def __init__(self):
        self.sh = gc.open(horse_data)
        self.ws = {t: self.sh.worksheet(t) for t in ('Horses', 'Sire', 'Dam')}
        self.data = {t: with_sheets_retry(ws.get_all_values) for t, ws in self.ws.items()}
        self.header = {t: (list(d[0]) if d else []) for t, d in self.data.items()}
        self.rows = {t: [list(r) for r in d[1:]] for t, d in self.data.items()}
        self.new_rows = {t: [] for t in self.ws}
        self.updated = {t: {} for t in self.ws}   # 行番号（0始まり）→ 行
        self.deleted_horses = set()   # Damシートに移すHorsesシートの行（id）
        self.cancel_rows = []
        self.stables = None
        self.info_cache = {}
        self.log = []

    # --- 共通 ---
    def info(self, url):
        if url not in self.info_cache:
            self.info_cache[url] = fetch_jra_horse_info(url)
        return self.info_cache[url]

    def all_rows(self, t):
        return self.rows[t] + self.new_rows[t]

    def find_rows(self, t, name):
        return [r for r in self.all_rows(t) if row_matches_name(r, name) and id(r) not in self.deleted_horses]

    def in_any_sheet(self, name):
        return any(self.find_rows(t, name) for t in ('Horses', 'Sire', 'Dam'))

    def search_url(self, name, known):
        """競走馬検索で馬名が完全一致する馬を探す。(URL, 見送った理由) を返す"""
        try:
            results = search_jra_horses(name)
        except Exception as e:
            return None, f'競走馬検索に失敗（{e}）'
        exact = [r for r in results if _same_name(r['name'], name)]
        if not exact:
            return None, '該当データなし'
        candidates = [r for r in exact if not r['kaku'] and r['url']]
        if not candidates:
            return None, '地方または海外所属の馬'
        if len(candidates) == 1 and not known:
            return candidates[0]['url'], ''
        # 同名の馬が複数いる場合（または照合できる情報がある場合）は、生年月日・父母・抹消日などで照合する
        matched = []
        for c in candidates:
            try:
                if not jra_info_conflicts(self.info(c['url']), known):
                    matched.append(c)
            except Exception:
                continue
        if len(matched) == 1:
            return matched[0]['url'], ''
        return None, ('同名の馬が複数いて特定できません' if matched else '情報が一致する馬がいません')

    def record_cancel(self, name, info):
        if info.get('status') == '抹消' and info.get('cancel_date'):
            self.cancel_rows.append([info['cancel_date'], name])

    # --- 競走馬（Horses） ---
    def horse_values(self, info):
        if self.stables is None:
            self.stables = get_stables_list()
        values = jra_info_to_columns(info, self.stables)
        values[H_STATUS] = info.get('status', '')
        return values

    def register_horse(self, name, url='', known=None):
        """Horsesに無い馬を登録する／URLが無い馬はURLと空欄の項目を埋める。結果の文を返す"""
        rows = self.find_rows('Horses', name)
        if rows and any(str(r[c]).strip() for r in rows for c in (H_JRA_URL, H_NAR_URL) if len(r) > c):
            return None   # 登録済み（URLあり）
        if not rows:
            # Sire・Damシートにある馬は、URLが無ければURLと空欄の項目を埋める
            for t in ('Sire', 'Dam'):
                p_rows = self.find_rows(t, name)
                if not p_rows:
                    continue
                if any(str(r[c]).strip() for r in p_rows for c in (P_JRA_URL, P_NAR_URL) if len(r) > c):
                    return None
                return self.fill_parent_url(t, p_rows[0], name, url, known)
        if not url:
            url, reason = self.search_url(name, known)
            if not url:
                return f"{name}：スキップ（{reason}）"
        info = self.info(url)
        if info.get('kaku'):
            return f"{name}：スキップ（地方または海外所属の馬）"
        if info.get('name') and not _same_name(info['name'], name) and not _same_name(info.get('name_en'), name):
            return f"{name}：スキップ（リンク先の馬名が「{info.get('name')}」で一致しません）"
        if info.get('gender') == '牝' and info.get('has_offspring'):
            # 産駒がいる牝馬（繁殖牝馬）は、競走馬（Horses）ではなく母馬（Dam）として登録する。
            # Horsesシートにある馬（URLが無い馬）は、Horsesシートから消してDamシートに移す
            moved = rows[0] if rows else None
            row = [''] * len(PARENT_COLUMNS)
            if moved is not None:
                moved = list(moved) + [''] * len(HORSE_COLUMNS)
                for h_col, p_col in ((H_NAME, P_NAME), (H_NAME_EN, P_NAME_EN), (H_BIRTH, P_BIRTH), (H_SIRE, P_SIRE),
                                     (H_DAM, P_DAM), (H_OWNER, P_OWNER), (H_REGION, P_REGION),
                                     (H_DETAIL, P_DETAIL), (H_BREEDER, P_BREEDER), (H_NAR_URL, P_NAR_URL)):
                    row[p_col] = str(moved[h_col] or '').strip()
                self.deleted_horses.add(id(rows[0]))
            elif is_english_name(name):
                row[P_NAME_EN] = name
            else:
                row[P_NAME] = name
            for col, val in self.parent_values(info).items():
                if not row[col]:
                    row[col] = val
            row[P_JRA_URL] = url
            existing = self.find_rows('Dam', name)
            if existing:
                # Damシートに同じ馬がいれば、空欄の項目だけ埋める
                target = existing[0]
                target.extend([''] * (len(PARENT_COLUMNS) - len(target)))
                for col in range(len(PARENT_COLUMNS)):
                    if not str(target[col]).strip() and row[col]:
                        target[col] = row[col]
                if target in self.rows['Dam']:
                    self.updated['Dam'][self.rows['Dam'].index(target)] = target
            else:
                self.new_rows['Dam'].append(row)
            self.record_cancel(name, info)
            parents = self.register_parents(info, date_year(row[P_BIRTH]), 1)
            label = "Horsesシートから母馬（Dam）に移動（産駒あり）" if moved is not None else "母馬として登録（産駒あり）"
            return f"{name}：{label}" + (f"（血統：{'、'.join(parents)}）" if parents else '')
        values = self.horse_values(info)
        if rows:
            row = rows[0]
            idx = self.rows['Horses'].index(row) if row in self.rows['Horses'] else None
            row.extend([''] * (len(HORSE_COLUMNS) - len(row)))
            row[H_JRA_URL] = url
            for col, val in values.items():
                if col != H_STATUS and not str(row[col]).strip():
                    row[col] = val
            if info.get('status') == '抹消' and row[H_STATUS] != '抹消':
                row[H_STATUS] = '抹消'
            if idx is not None:
                self.updated['Horses'][idx] = row
            message = f"{name}：URLを登録"
        else:
            row = [''] * len(HORSE_COLUMNS)
            if is_english_name(name):
                row[H_NAME_EN] = name
            else:
                row[H_NAME] = name
            for col, val in values.items():
                row[col] = val
            row[H_JRA_URL] = url
            row[H_REG_STATUS] = row[H_STATUS]
            self.new_rows['Horses'].append(row)
            message = f"{name}：登録"
        self.record_cancel(name, info)
        parents = self.register_parents(info, date_year(row[H_BIRTH]))
        return message + (f"（血統：{'、'.join(parents)}）" if parents else '')

    # --- 親（Sire・Dam） ---
    def fill_parent_url(self, t, row, name, url, known):
        """Sire・DamシートでURLが無い馬に、URLと空欄の項目を埋める"""
        known = dict(known or {}, gender='牡' if t == 'Sire' else '牝')
        if not url:
            url, reason = self.search_url(name, known)
            if not url:
                return f"{name}：スキップ（{reason}）"
        info = self.info(url)
        if info.get('kaku'):
            return f"{name}：スキップ（地方または海外所属の馬）"
        row.extend([''] * (len(PARENT_COLUMNS) - len(row)))
        for col, val in self.parent_values(info).items():
            if not str(row[col]).strip():
                row[col] = val
        row[P_JRA_URL] = url
        if row in self.rows[t]:
            self.updated[t][self.rows[t].index(row)] = row
        self.record_cancel(name, info)
        parents = self.register_parents(info, date_year(row[P_BIRTH]), 1)
        return f"{name}：{'父馬' if t == 'Sire' else '母馬'}のURLを登録" + (f"（血統：{'、'.join(parents)}）" if parents else '')

    def parent_record(self, t, name, child_birth_year):
        """血統表と同じように、仔より前に生まれた同名の馬（生年が分からない馬も含む）を探す"""
        rows = self.find_rows(t, name)
        if child_birth_year:
            birth = lambda r: date_year(r[P_BIRTH]) if len(r) > P_BIRTH else None
            before = [r for r in rows if birth(r) and birth(r) < child_birth_year]
            unknown = [r for r in rows if not birth(r)]
            rows = before or unknown
        return rows[0] if rows else None

    @staticmethod
    def parent_needs_update(row):
        """血統表に「編集」のリンクが表示される状態か（未登録、または生年月日・父・母・産地・英馬名のどれかが空欄）"""
        if row is None:
            return True
        row = list(row) + [''] * len(PARENT_COLUMNS)
        return not all(str(row[c]).strip() for c in (P_BIRTH, P_SIRE, P_DAM, P_REGION, P_NAME_EN))

    def parent_values(self, info):
        values = {P_NAME_EN: info.get('name_en', ''), P_SIRE: info.get('sire', ''), P_DAM: info.get('dam', ''),
                  P_OWNER: info.get('owner', ''), P_BREEDER: info.get('breeder', '')}
        if info.get('birth_year'):
            values[P_BIRTH] = format_date8(info['birth_year'], info['birth_month'], info['birth_day'])
        if info.get('birthplace'):
            match = find_birthplace_match(info['birthplace'])
            if match:
                values[P_REGION], values[P_DETAIL] = match['region'], match['detail'] or ''
            else:
                values[P_REGION] = info['birthplace']
        return {k: v for k, v in values.items() if v}

    def register_parent(self, t, name, url, child_birth_year, depth):
        """親を登録する（既にある場合は空欄の項目を埋める）。登録した馬の文の一覧を返す"""
        row = self.parent_record(t, name, child_birth_year)
        if not self.parent_needs_update(row):
            return []
        known = {'gender': '牡' if t == 'Sire' else '牝', 'born_before': child_birth_year}
        if not url:
            url, reason = self.search_url(name, known)
            if not url:
                return []
        try:
            info = self.info(url)
        except Exception as e:
            return [f"{name}（取得に失敗：{e}）"]
        if info.get('kaku') or jra_info_conflicts(info, known):
            return []
        values = self.parent_values(info)
        if row is not None:
            row.extend([''] * (len(PARENT_COLUMNS) - len(row)))
            for col, val in values.items():
                if not str(row[col]).strip():
                    row[col] = val
            if not str(row[P_JRA_URL]).strip():
                row[P_JRA_URL] = url
            if row in self.rows[t]:
                self.updated[t][self.rows[t].index(row)] = row
            done = [f"{name}を更新"]
        else:
            row = [''] * len(PARENT_COLUMNS)
            if is_english_name(name):
                row[P_NAME_EN] = name
            else:
                row[P_NAME] = name
            for col, val in values.items():
                row[col] = val
            row[P_JRA_URL] = url
            self.new_rows[t].append(row)
            done = [f"{name}を登録"]
        self.record_cancel(name, info)
        if depth < self.MAX_PARENT_DEPTH:
            done += self.register_parents(info, date_year(row[P_BIRTH]), depth + 1)
        return done

    def register_parents(self, info, birth_year, depth=0):
        """父・母（母がJRAに無くても母の父にリンクがあれば母の父）を登録する"""
        done = []
        for t, name_key, url_key in (('Sire', 'sire', 'sire_url'), ('Dam', 'dam', 'dam_url'),
                                     ('Sire', 'dam_sire', 'dam_sire_url')):
            name = (info.get(name_key) or '').strip()
            if not name:
                continue
            # 母の父は、リンクがある場合だけ確認する（母のページからたどれない場合のため）
            if name_key == 'dam_sire' and not info.get(url_key):
                continue
            dam_row_birth = None
            if name_key == 'dam_sire':
                dam_row = self.parent_record('Dam', info.get('dam', ''), birth_year)
                dam_row_birth = date_year(dam_row[P_BIRTH]) if dam_row is not None else None
            done += self.register_parent(t, name, info.get(url_key, ''),
                                         dam_row_birth or birth_year, depth)
        return done

    # --- まとめて書き込む ---
    def save(self):
        added_horses = []
        updated_horses = {display_name_of_row(r) for r in self.updated['Horses'].values()}
        if self.deleted_horses:
            # Damシートに移した馬をHorsesシートから消すため、Horsesシートは全体を書き直す
            ws = self.ws['Horses']
            header = self.header['Horses'] + [''] * max(0, len(HORSE_COLUMNS) - len(self.header['Horses']))
            rows = [r for r in self.rows['Horses'] if id(r) not in self.deleted_horses] + self.new_rows['Horses']
            rows.sort(key=lambda r: display_name_of_row(r))
            ensure_columns(ws, len(header))
            ws.clear()
            with_sheets_retry(ws.update, range_name='A1', values=[header] + rows)
            added_horses = [display_name_of_row(r) for r in self.new_rows['Horses']]
            self.updated['Horses'], self.new_rows['Horses'] = {}, []
        for t, ws in self.ws.items():
            columns = HORSE_COLUMNS if t == 'Horses' else PARENT_COLUMNS
            if self.updated[t]:
                ensure_columns(ws, len(columns))
                last = col_letter(len(columns) - 1)
                with_sheets_retry(ws.batch_update, [
                    {'range': f"A{idx + 2}:{last}{idx + 2}", 'values': [list(row)[:len(columns)]]}
                    for idx, row in self.updated[t].items()])
            if self.new_rows[t]:
                ensure_columns(ws, len(columns))
                with_sheets_retry(ws.append_rows, [r[:len(columns)] for r in self.new_rows[t]],
                                  value_input_option='RAW')
                sort_and_resize_table(ws, sort_col_index=0)
                if t == 'Horses':
                    added_horses = [display_name_of_row(r) for r in self.new_rows['Horses']]
        if self.cancel_rows:
            ws_cancel = get_or_create_worksheet(self.sh, "抹消", ["年月日", "馬名"])
            existing = {r[1] for r in ws_cancel.get_all_values()[1:] if len(r) > 1}
            rows = [r for r in self.cancel_rows if r[1] not in existing]
            if rows:
                ws_cancel.append_rows(rows)
        # 登録した馬・URLを登録した馬のクラス・最新レースを計算する
        names = set(added_horses) | updated_horses
        if names:
            try:
                update_horse_class_columns(lambda r: display_name_of_row(r) in names)
            except Exception as e:
                print(f"クラス・最新レースの更新に失敗しました: {e}")

def unregistered_runners(parsed_list):
    """レース結果の出走馬のうち、Horses・Sire・Damのどのシートにも無い馬（地方・海外所属の馬を除く）"""
    sh = gc.open(horse_data)
    names = set()
    for t in ('Horses', 'Sire', 'Dam'):
        for r in sh.worksheet(t).get_all_values()[1:]:
            names.update(n.strip() for n in r[:2] if n and n.strip())
    items, seen = [], set()
    for parsed in parsed_list:
        for r in parsed['runners']:
            name = r.get('name', '').strip()
            if not name or name in names or name in seen or not r.get('horse_url'):
                continue
            if any(i in JRA_KAKU_ICONS for i in r.get('icons', [])):
                continue
            seen.add(name)
            items.append({'name': name, 'url': r['horse_url']})
    return items

@app.route('/api/auto_register', methods=['POST'])
@login_required
def api_auto_register():
    """未登録の馬（またはURLが無い馬）をJRAの競走馬情報から登録する。items：[{'name', 'url', 'known'}]（1回に5頭まで）"""
    data = request.get_json(silent=True) or {}
    items = (data.get('items') or [])[:5]
    try:
        reg = AutoRegistrar()
        results = []
        for item in items:
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            try:
                msg = reg.register_horse(name, str(item.get('url') or ''), item.get('known') or None)
                results.append({'name': name, 'message': msg or f"{name}：登録済み",
                                'ok': bool(msg) and 'スキップ' not in msg})
            except Exception as e:
                results.append({'name': name, 'message': f"{name}：失敗（{e}）", 'ok': False})
        reg.save()
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'results': results})

@app.route('/api/auto_register/targets')
@login_required
def api_auto_register_targets():
    """自動登録の対象：source=index は競走馬一覧でURLボタンが出ている馬、
    source=weekly は週間レポートでHorsesシートに無い馬（date の週）"""
    source = request.args.get('source', '')
    sh = gc.open(horse_data)
    horses = sh.worksheet('Horses').get_all_values()[1:]
    items = []
    if source == 'index':
        for r in horses:
            name = display_name_of_row(r)
            status = r[H_STATUS].strip() if len(r) > H_STATUS else ''
            if not name or status == '抹消' or any(len(r) > c and r[c].strip() for c in (H_JRA_URL, H_NAR_URL)):
                continue
            known = {'birth': r[H_BIRTH] if len(r) > H_BIRTH else '', 'sire': r[H_SIRE] if len(r) > H_SIRE else '',
                     'dam': r[H_DAM] if len(r) > H_DAM else '', 'gender': r[H_GENDER] if len(r) > H_GENDER else ''}
            items.append({'name': name, 'known': {k: v for k, v in known.items() if v}})
    elif source == 'weekly':
        base = parse_full_date(request.args.get('date', '')) or datetime.now().date()
        week_start = base - timedelta(days=base.weekday())
        week_end = week_start + timedelta(days=6)
        in_week = lambda d: d is not None and week_start <= d <= week_end
        registered = {n.strip() for r in horses for n in r[:2] if n and n.strip()}
        parents = set()   # Sire・Damシートにあり、URLも登録されている馬（URLが無い馬は対象にする）
        for t in ('Sire', 'Dam'):
            for r in sh.worksheet(t).get_all_values()[1:]:
                if any(len(r) > c and r[c].strip() for c in (P_JRA_URL, P_NAR_URL)):
                    parents.update(n.strip() for n in r[:2] if n and n.strip())
        found = {}
        def _add(name, known=None):
            name = (name or '').strip()
            if name and name not in registered and name not in parents and name not in found:
                found[name] = known or {}
        for title, date_col, name_col in (('抹消', 0, 1), ('Changes', 0, 1), ('放牧入厩', 0, 1), ('放牧入厩', 2, 1)):
            try:
                for r in sh.worksheet(title).get_all_values()[1:]:
                    d = parse_full_date(r[date_col] if len(r) > date_col else '')
                    if in_week(d) and len(r) > name_col:
                        _add(r[name_col], {'cancel_date': d.strftime('%Y%m%d')} if title == '抹消' else None)
            except gspread.WorksheetNotFound:
                pass
        items = [{'name': n, 'known': k} for n, k in sorted(found.items())]
    return jsonify({'items': items})

@app.route('/api/race_results/scan', methods=['POST'])
@login_required
def api_race_results_scan():
    """開催選択・レース選択・レース結果のページ（アップロード）またはJRAのサイトから、取り込む開催日の一覧を返す"""
    html_list = []
    try:
        if request.form.get('live') == '1':
            html_list.append(fetch_live_kaisai_html())
        else:
            for f in request.files.getlist('race_files'):
                if f and f.filename:
                    html_list.append(_extract_html_from_upload(f.read()))
            if not html_list:
                return jsonify({'error': 'ファイルが選択されていません。'}), 400
        days = scan_result_pages(html_list)
    except Exception as e:
        return jsonify({'error': f'ページの読み取りに失敗しました: {e}'}), 400
    if not days:
        return jsonify({'error': 'レース結果へのリンクが見つかりませんでした。JRAの「レース結果」の開催選択・'
                                 'レース選択・レース結果のいずれかのページを指定してください。'}), 400
    return jsonify({'days': days})

@app.route('/api/race_results/import_day', methods=['POST'])
@login_required
def api_race_results_import_day():
    data = request.get_json(silent=True) or {}
    try:
        result = import_result_day(url=data.get('url', ''), via=data.get('via', ''), key=data.get('key', ''),
                                   overwrite=bool(data.get('overwrite')))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)

@app.route('/api/fetch_nar_horse')
@login_required
def api_fetch_nar_horse():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({"error": "URLを入力してください。"}), 400
    try:
        data = fetch_nar_horse_info(url)
        if not data:
            return jsonify({"error": "ページから情報を取得できませんでした。"}), 404
        return jsonify(data), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"ページの取得に失敗しました: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"解析中にエラーが発生しました: {e}"}), 500

@app.route('/api/fetch_other_horse')
@login_required
def api_fetch_other_horse():
    p_name = request.args.get('p_name', '').strip()
    p_type = request.args.get('p_type', 'Sire').strip()
    direct_url = request.args.get('url', '').strip() or None
    # 画面で産地に「海外」を選んでいる場合は、父・母を英字で取得する
    overseas = request.args.get('region', '').strip() == '海外'
    if not p_name and not direct_url:
        return jsonify({"error": "馬名またはURLが取得できませんでした。"}), 400
    try:
        data = fetch_other_horse_info(p_name, p_type, direct_url=direct_url, overseas=overseas)
        if not data.get('sire') and not data.get('dam'):
            return jsonify({"error": "ページから情報を取得できませんでした。"}), 404
        return jsonify(data), 200
    except JbisMultipleMatchError as e:
        return jsonify({"error": str(e)}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"ページの取得に失敗しました: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"解析中にエラーが発生しました: {e}"}), 500

@app.route('/api/parse_equibase_text', methods=['POST'])
@login_required
def api_parse_equibase_text():
    """add_parent：equibaseのページからコピーした文章を読み取って、入力欄に反映する値を返す"""
    text = (request.get_json(silent=True) or {}).get('text', '')
    if not text.strip():
        return jsonify({"error": "equibaseのページのデータを貼り付けてください。"}), 400
    try:
        data = parse_equibase_text(text)
    except Exception as e:
        return jsonify({"error": f"読み取り中にエラーが発生しました: {e}"}), 500
    if not any(data.get(k) for k in ('origin_code', 'birth_year', 'owner', 'breeder')):
        return jsonify({"error": "データから情報を読み取れませんでした。"}), 404
    return jsonify(data), 200

@app.route('/add_horse')
@login_required
@wait_for_sheets
def add_horse_page():
    return render_template('add_horse.html', 
                           stables=get_stables_list(), 
                           default_year=datetime.now().year - 3,
                           # 週間レポートのURLボタンから開いた場合は馬名を入力済みにする
                           prefill_name=request.args.get('name', '').strip())

def resolve_registration_status(status, birth_year, reg_year, reg_month):
    """
    競走馬登録（早期特例登録制度）の時期から、登録時の状態を決定する。
      ・1歳9月・10月の登録             → 「早期」
      ・1歳11月〜2歳2月の登録          → 状態が「放牧」なら「早期」（それ以外は従来通り）
      ・上記以外／登録日未入力         → 状態はそのまま
    ※ 馬齢は「登録年 − 生年」で数える。
    """
    try:
        birth_year = int(birth_year)
        reg_year = int(reg_year)
        reg_month = int(reg_month)
    except (TypeError, ValueError):
        return status

    age = reg_year - birth_year
    if age == 1 and reg_month in (9, 10):
        return '早期'
    if (age == 1 and reg_month >= 11) or (age == 2 and reg_month <= 2):
        if status == '放牧':
            return '早期'
    return status

def _extract_html_from_upload(file_bytes):
    """
    アップロードされたファイルがMHTML形式（Webページの保存ファイル）ならHTML本文を取り出し、
    単なるHTMLファイルならそのまま文字列にして返す。
    """
    head = file_bytes[:2000].lower()
    if b'mime-version' in head or b'content-type: multipart/related' in head:
        msg = email.message_from_bytes(file_bytes)
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                charset = part.get_content_charset() or 'cp932'
                payload = part.get_payload(decode=True)
                try:
                    return payload.decode(charset, errors='replace')
                except (LookupError, UnicodeDecodeError):
                    return payload.decode('cp932', errors='replace')
        raise ValueError('MHTMLファイルの中にHTML本文が見つかりませんでした。')
    for enc in ('cp932', 'utf-8'):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode('cp932', errors='replace')


# --- 産地（産地／地域）の判定用データ ---
# add_horse.html／add_parent.html／edit_horse.html のJS版 locationData・
# OVERSEAS_ABBREVIATION_MAP と同じ内容。どちらかを変更した場合はもう一方も合わせて変更すること。
LOCATION_DATA = {
    "北海道": {
        "日高振興局": [
            "浦河町", "えりも町", "荻伏村", "様似町", "様似村",
            "静内町", "新ひだか町", "新冠町", "新冠村", "日高町",
            "日高村", "平取町", "幌泉町", "幌泉村", "三石町",
            "三石村", "門別町", "門別村", "平取村"
        ],
        "胆振総合振興局": [
            "厚真町", "厚真村", "安平町", "安平村", "虻田町", "追分町",
            "追分村", "白老町", "白老村", "伊達市", "伊達町",
            "洞爺湖町", "洞爺村", "苫小牧市", "苫小牧町", "豊浦町", "豊浦村", "登別市",
            "登別町", "早来町", "早来村", "穂別町", "穂別村",
            "幌別町", "幌別村", "むかわ町", "鵡川町", "鵡川村",
            "室蘭市"
        ],
        "十勝総合振興局": [
            "池田町", "帯広市", "清水町", "清水村", "士幌町", "士幌村", "大樹町", "大樹村", "幕別町", "幕別村"
        ],
        "石狩振興局": [
            "千歳市", "千歳町"
        ],
        "渡島総合振興局": [
            "函館市", "森町"
        ],
        "根室振興局": [
            "別海町", "別海村"
        ]
    },
    "青森県": [], "福島県": [], "茨城県": [], "栃木県": [], "群馬県": [],
    "千葉県": [], "長野県": [], "熊本県": [], "大分県": [], "宮崎県": [], "鹿児島県": [],
    "海外": {
        "北米": ["アメリカ", "カナダ", "メキシコ"],
        "欧州": ["イギリス", "アイルランド", "フランス", "ドイツ", "イタリア", "デンマーク", "スウェーデン"],
        "オセアニア": ["オーストラリア", "ニュージーランド"],
        "南米": ["アルゼンチン", "チリ", "ブラジル", "ウルグアイ", "ペルー"],
        "中東": ["アラブ首長国連邦", "サウジアラビア"],
        "アフリカ": ["南アフリカ"]
    }
}

OVERSEAS_ABBREVIATION_MAP = {
    '米': 'アメリカ',
    '加': 'カナダ',
    '英': 'イギリス',
    '愛': 'アイルランド',
    '仏': 'フランス',
    '独': 'ドイツ',
    '豪': 'オーストラリア',
    '新': 'ニュージーランド',
    '亜': 'アルゼンチン',
}

def find_birthplace_match(raw):
    """
    JRAの「産地」表記を、産地（詳細：市区町村・国名など）→産地（第1階層：都道府県／海外）→
    海外の略称（米・英・仏など）の優先順位でLOCATION_DATAと突き合わせ、
    {'region':..., 'detail':...} を返す。一致しなければNone。
    （add_horse.htmlのfindBirthplaceMatch関数と同じロジック）
    """
    if not raw:
        return None
    text = raw.strip()

    # 1. 産地（詳細：市区町村・国名など）に一致するか
    for region, region_data in LOCATION_DATA.items():
        if isinstance(region_data, list):
            if text in region_data:
                return {'region': region, 'detail': text}
        elif isinstance(region_data, dict):
            for group_list in region_data.values():
                if text in group_list:
                    return {'region': region, 'detail': text}

    # 2. 産地（第1階層）そのものに一致するか
    if text in LOCATION_DATA:
        return {'region': text, 'detail': None}

    # 3. 海外の略称（米・英・仏など）を正式名称に変換して「海外」から選ぶ
    if text in OVERSEAS_ABBREVIATION_MAP:
        return {'region': '海外', 'detail': OVERSEAS_ABBREVIATION_MAP[text]}

    return None

def parse_jra_registration_page(html_text):
    """
    JRAの「競走馬登録」ページ（HTML／MHTMLで保存したもの）を解析し、
    (registration_str, [horse_dict, ...]) を返す。
    registration_str は "YYYYMMDD" 形式（見出しの日付が見つからなければ空文字）。
    horse_dict のキー: name, gender, birth(=YYYYMMDD), area(拠点), stable(厩舎名),
                       region(産地), detail(地域), breeder(生産牧場), owner(馬主名), sire, dam
    """
    soup = BeautifulSoup(html_text, 'html.parser')

    registration_str = ''
    main_div = soup.select_one('caption .main') or soup.find('div', class_='main')
    if main_div:
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', main_div.get_text())
        if m:
            registration_str = format_date8(m.group(1), m.group(2), m.group(3))

    horses = []
    for tr in soup.select('tbody tr'):
        name_el = tr.find('th', attrs={'scope': 'row'})
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name:
            continue

        tds = tr.find_all('td')
        if len(tds) < 3:
            continue

        # 性別・毛色（毛色は使わない）・生年月日
        lines = [l.strip() for l in tds[0].get_text(separator='\n').split('\n') if l.strip()]
        gender = lines[0].split('・')[0].strip() if lines else ''
        birth_date_str = ''
        if len(lines) > 1:
            m2 = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', lines[1])
            if m2:
                birth_date_str = format_date8(m2.group(1), m2.group(2), m2.group(3))

        # 預託きゅう舎（拠点・厩舎名）／産地・生産牧場／馬主名
        stable_lines = [l.strip() for l in tds[1].get_text(separator='\n').split('\n') if l.strip()]
        area = stable_name = region = detail = breeder = owner = ''
        if len(stable_lines) > 0:
            m3 = re.match(r'[（(](.+?)[）)]\s*(.+)', stable_lines[0])
            if m3:
                area, stable_name = m3.group(1).strip(), m3.group(2).strip()
            else:
                stable_name = stable_lines[0]
        if len(stable_lines) > 1:
            # 「産地 生産牧場」の形式（最初の空白で分割）。生産牧場名に空白を含む馬は手直しが必要な場合がある
            parts = stable_lines[1].split(None, 1)
            if len(parts) == 2:
                raw_place, breeder = parts[0].strip(), parts[1].strip()
                match = find_birthplace_match(raw_place)
                if match:
                    region = match['region']
                    detail = match['detail'] or ''
                else:
                    # 一致しない場合は産地にそのまま入れておく（地域は空欄）
                    region = raw_place
            else:
                breeder = stable_lines[1]
        if len(stable_lines) > 2:
            owner = stable_lines[2].strip()

        # 父・母（<div class="parent"><span>父</span><span>母</span></div>）
        sire = dam = ''
        parent_div = tr.find('div', class_='parent')
        if parent_div:
            spans = parent_div.find_all('span')
            if len(spans) > 0:
                sire = spans[0].get_text(strip=True)
            if len(spans) > 1:
                dam = spans[1].get_text(strip=True)

        horses.append({
            'name': name, 'gender': gender, 'birth': birth_date_str,
            'area': area, 'stable': stable_name, 'region': region, 'detail': detail,
            'breeder': breeder, 'owner': owner, 'sire': sire, 'dam': dam,
        })

    return registration_str, horses


@app.route('/import_horses', methods=['GET', 'POST'])
@login_required
def import_horses():
    """JRAの「競走馬登録」ページ（HTML／MHTML保存）をアップロードして一括登録する"""
    if request.method == 'GET':
        return render_template('import_horses.html', import_status=get_import_status(),
                               schedule_note=IMPORT_SCHEDULE_NOTE)

    file = request.files.get('mhtml_file')
    if not file or not file.filename:
        flash('ファイルが選択されていません。')
        return redirect('/import_horses')

    try:
        html_text = _extract_html_from_upload(file.read())
        if detect_jra_page_kind(html_text) in ('trainer', 'cancel'):
            flash(check_jra_page_kind(html_text, 'registration'))
            return redirect('/import_horses')
        registration_str, parsed_horses = parse_jra_registration_page(html_text)
    except Exception as e:
        flash(f'ファイルの解析に失敗しました: {e}')
        return redirect('/import_horses')

    if not parsed_horses:
        flash('馬のデータが見つかりませんでした。ファイルの形式をご確認ください。')
        return redirect('/import_horses')

    def _blank(v):
        return v is None or str(v).strip() == ''

    try:
        sh = gc.open(horse_data)
        ws = sh.worksheet("Horses")
        data = ws.get_all_values()
        headers = list(data[0]) if data else []
        rows = [list(r) for r in data[1:]]

        while len(headers) < len(HORSE_COLUMNS):
            headers.append('')
        for idx in (H_REGISTRATION, H_REG_STATUS, H_CLASS, H_LAST_RACE, H_SHUTOKU):
            if _blank(headers[idx]):
                headers[idx] = HORSE_COLUMNS[idx]
        ensure_columns(ws, len(HORSE_COLUMNS))

        name_to_index = {r[H_NAME]: i for i, r in enumerate(rows) if r and r[H_NAME]}

        reg_y, reg_m = '', ''
        if registration_str:
            reg_parts = split_date(registration_str)
            reg_y = str(reg_parts[0]) if reg_parts else ''
            reg_m = str(reg_parts[1]) if reg_parts and reg_parts[1] else ''

        added = updated = skipped = 0

        for h in parsed_horses:
            name = h['name']

            if name not in name_to_index:
                # 新規登録：状態・URLは空欄のまま（早期登録の時期に該当する場合のみ状態を「早期」にする）
                birth_year = date_year(h['birth']) or ''
                status = resolve_registration_status('', birth_year, reg_y, reg_m)

                new_row = [''] * len(headers)
                new_row[H_NAME] = name
                new_row[H_GENDER] = h['gender']
                new_row[H_BIRTH] = h['birth']
                new_row[H_SIRE] = h['sire']
                new_row[H_DAM] = h['dam']
                new_row[H_OWNER] = h['owner']
                new_row[H_AREA] = h['area']
                new_row[H_STABLE] = h['stable']
                new_row[H_STATUS] = status
                new_row[H_REGION] = h['region']
                new_row[H_DETAIL] = h['detail']
                new_row[H_BREEDER] = h['breeder']
                new_row[H_REGISTRATION] = registration_str
                new_row[H_REG_STATUS] = status
                rows.append(new_row)
                name_to_index[name] = len(rows) - 1
                added += 1
            else:
                # 既存馬：空欄の項目だけ埋める（状態・URLは対象外＝既存の値を空欄にしない）
                row = rows[name_to_index[name]]
                while len(row) < len(headers):
                    row.append('')

                field_map = {
                    H_GENDER: h['gender'], H_BIRTH: h['birth'], H_SIRE: h['sire'], H_DAM: h['dam'],
                    H_OWNER: h['owner'], H_AREA: h['area'], H_STABLE: h['stable'],
                    H_REGION: h['region'], H_DETAIL: h['detail'], H_BREEDER: h['breeder'],
                }
                changed = False
                for col_idx, new_val in field_map.items():
                    if _blank(row[col_idx]) and new_val:
                        row[col_idx] = new_val
                        changed = True

                # 競走馬登録日が未入力だった場合のみ設定し、早期登録の時期に該当すれば状態を更新する
                if _blank(row[H_REGISTRATION]) and registration_str:
                    row[H_REGISTRATION] = registration_str
                    birth_year = date_year(row[H_BIRTH]) or ''
                    new_status = resolve_registration_status(row[H_STATUS], birth_year, reg_y, reg_m)
                    if new_status != row[H_STATUS]:
                        row[H_STATUS] = new_status
                    if _blank(row[H_REG_STATUS]):
                        row[H_REG_STATUS] = row[H_STATUS]
                    changed = True

                if changed:
                    updated += 1
                else:
                    skipped += 1

        rows.sort(key=lambda r: display_name_of_row(r))
        ws.clear()
        ws.update(range_name='A1', values=[headers] + rows)

        record_import_date('import_registration', registration_str)
        msg = f"インポート完了：新規 {added}件／更新 {updated}件／スキップ {skipped}件"
        if registration_str:
            msg += f"（登録日：{format_date_jp(registration_str)}）"
        flash(msg)
        return redirect('/import_horses')
    except Exception as e:
        flash(f'登録処理中にエラーが発生しました: {e}')
        return redirect('/import_horses')


def detect_jra_page_kind(html_text):
    """保存されたJRAのページの種類を判定する：'trainer'（管理馬一覧）／'cancel'（抹消一覧）／
    'registration'（競走馬登録）／''（不明）"""
    soup = BeautifulSoup(html_text, 'html.parser')
    if soup.select_one('#meikan_management'):
        return 'trainer'
    if soup.select_one('#entry_erasure'):
        return 'cancel'
    caption = soup.select_one('caption .main')
    caption_text = caption.get_text(strip=True) if caption else ''
    title = soup.title.get_text(strip=True) if soup.title else ''
    if '抹消一覧' in caption_text or '抹消一覧' in title:
        return 'cancel'
    if '管理馬一覧' in title:
        return 'trainer'
    if '競走馬登録' in caption_text or '競走馬登録' in title:
        return 'registration'
    return ''

JRA_PAGE_KIND_LABEL = {'trainer': '管理馬一覧', 'cancel': '抹消一覧', 'registration': '競走馬登録'}

def check_jra_page_kind(html_text, expected):
    """アップロードされたページが想定と違う種類なら、エラーメッセージを返す（問題なければ None）。
    例：管理馬一覧のファイルを抹消一覧の欄で取り込むと、一覧の馬がすべて抹消になってしまうため"""
    kind = detect_jra_page_kind(html_text)
    if kind == expected:
        return None
    detected = JRA_PAGE_KIND_LABEL.get(kind)
    msg = f"{JRA_PAGE_KIND_LABEL[expected]}のページではありません。"
    if detected:
        msg += f"選ばれたファイルは「{detected}」のページです。取り込む欄を確認してください。"
    return msg + "（何も変更していません）"

def parse_jra_trainer_horses_page(html_text):
    """
    JRAの調教師「管理馬一覧」ページ（HTML／MHTMLで保存したもの）を解析し、
    (trainer_name, as_of_date, [horse_dict, ...]) を返す。
    as_of_date は見出し「管理馬一覧（2026年10月8日現在）」の日付（"YYYYMMDD"。見つからなければ空文字）。
    horse_dict のキー: name, url, gender(性齢から馬齢を除いたもの), status(管理馬の状態欄), early(早期特例登録馬か)
    """
    soup = BeautifulSoup(html_text, 'html.parser')

    # 見出し「<span class="opt">管理馬一覧</span>相沢 郁<span class="kana">…</span>」から調教師名を取得
    trainer_name = ''
    txt_span = soup.select_one('h1 span.txt')
    if txt_span:
        trainer_name = ''.join(txt_span.find_all(string=True, recursive=False)).strip()

    as_of_date = ''
    header = soup.select_one('div.contents_header h2')
    if header:
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', header.get_text())
        if m:
            as_of_date = format_date8(m.group(1), m.group(2), m.group(3))

    horses = []
    for content in soup.select('div.content.mt10'):
        for table in content.find_all('table'):
            caption = table.select_one('caption .main')
            label = caption.get_text(strip=True) if caption else ''
            early = label.startswith('早期特例登録馬')
            if not early and not label.startswith('管理馬'):
                continue
            for tr in table.select('tbody tr'):
                th = tr.find('th', attrs={'scope': 'row'})
                link = th.find('a') if th else None
                if not link or not link.get_text(strip=True):
                    continue
                tds = [td.get_text(strip=True) for td in tr.find_all('td')]
                horses.append({
                    'name': link.get_text(strip=True),
                    'url': requests.compat.urljoin('https://www.jra.go.jp/', link.get('href', '')),
                    'gender': re.sub(r'\d', '', tds[0]) if tds else '',
                    # 管理馬の状態欄（放牧 or 空欄）。早期特例登録馬の表には状態欄が無い
                    'status': tds[1] if len(tds) > 1 else '',
                    'early': early,
                })
    return trainer_name, as_of_date, horses

def resolve_trainer_page_status(current, horse):
    """管理馬一覧の内容から、Horsesシートの状態（I列）の新しい値を決める"""
    if horse['early']:
        # 早期特例登録馬は「早期」にする。競走馬登録画面でJRAのURLから取得すると、ページの放牧マーク
        # （span.rest）で「放牧」になって登録されるため、「放牧」も「早期」に直す（「入厩」はそのまま）
        return '早期' if current in ('', '放牧', '早期') else current
    if horse['status'] == '放牧':
        return '放牧' if current in ('入厩', '早期', '') else current
    if horse['status'] == '':
        return '入厩' if current in ('放牧', '早期', '') else current
    return current

def jra_info_to_columns(info, all_stables):
    """fetch_jra_horse_info の結果を Horsesシートの列番号 → 値 の辞書に変換する"""
    values = {
        H_NAME_EN: info.get('name_en', ''),
        H_GENDER: info.get('gender', ''), H_SIRE: info.get('sire', ''), H_DAM: info.get('dam', ''),
        H_OWNER: info.get('owner', ''), H_BREEDER: info.get('breeder', ''),
    }
    if info.get('birth_year'):
        values[H_BIRTH] = format_date8(info['birth_year'], info['birth_month'], info['birth_day'])
    if info.get('trainer_raw'):
        t_name, t_area = parse_trainer_field(info['trainer_raw'])
        match = find_stable_match(all_stables, t_area, t_name)
        if match:
            values[H_AREA] = match['area']
            values[H_STABLE] = match['display_name'].split('・', 1)[1] if '・' in match['display_name'] else match['display_name']
        else:
            values[H_AREA], values[H_STABLE] = t_area, t_name
    if info.get('birthplace'):
        match = find_birthplace_match(info['birthplace'])
        if match:
            values[H_REGION], values[H_DETAIL] = match['region'], match['detail'] or ''
        else:
            # 一致しない場合は産地にそのまま入れておく（地域は空欄）
            values[H_REGION] = info['birthplace']
    return {k: v for k, v in values.items() if v}

@app.route('/import_trainer_horses', methods=['POST'])
@login_required
def import_trainer_horses():
    """JRAの調教師「管理馬一覧」ページ（HTML／MHTML保存）をアップロードして、状態の更新と未登録馬の登録を行う"""
    from concurrent.futures import ThreadPoolExecutor

    file = request.files.get('trainer_file')
    if not file or not file.filename:
        flash('ファイルが選択されていません。')
        return redirect('/import_horses')

    try:
        html_text = _extract_html_from_upload(file.read())
        page_error = check_jra_page_kind(html_text, 'trainer')
        if page_error:
            flash(page_error)
            return redirect('/import_horses')
        trainer_name, as_of_date, parsed_horses = parse_jra_trainer_horses_page(html_text)
    except Exception as e:
        flash(f'ファイルの解析に失敗しました: {e}')
        return redirect('/import_horses')

    if not parsed_horses:
        flash('管理馬が見つかりませんでした。ファイルの形式をご確認ください。')
        return redirect('/import_horses')

    def _blank(v):
        return v is None or str(v).strip() == ''

    # 空欄を埋める対象の列（状態・競走馬登録は対象外）
    fill_cols = [H_NAME_EN, H_GENDER, H_BIRTH, H_SIRE, H_DAM, H_OWNER, H_AREA, H_STABLE,
                 H_REGION, H_DETAIL, H_BREEDER]

    try:
        sh = gc.open(horse_data)
        ws = sh.worksheet("Horses")
        data = ws.get_all_values()
        headers = list(data[0]) if data else []
        rows = [list(r) for r in data[1:]]
        while len(headers) < len(HORSE_COLUMNS):
            headers.append('')
        for idx in (H_REG_STATUS, H_CLASS, H_LAST_RACE, H_SHUTOKU):
            if not headers[idx]:
                headers[idx] = HORSE_COLUMNS[idx]
        ensure_columns(ws, len(HORSE_COLUMNS))
        for r in rows:
            while len(r) < len(headers):
                r.append('')
        name_to_index = {r[H_NAME]: i for i, r in enumerate(rows) if r and r[H_NAME]}

        # 一覧の全馬のリンク先をまとめて並列取得する（新規登録・空欄補完・馬主変更／去勢の判定に使う）
        def _fetch(h):
            try:
                return h['name'], fetch_jra_horse_info(h['url']), None
            except Exception as e:
                return h['name'], None, e

        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = {name: (info, err) for name, info, err in pool.map(_fetch, parsed_horses)}

        all_stables = get_stables_list()
        trainer_match = find_stable_match(all_stables, '', trainer_name) if trainer_name else None
        # 放牧入厩・Changesシートに記録する日付は、見出し「管理馬一覧（2026年10月8日現在）」の日付
        today_str = as_of_date or today8()

        added, status_changed, filled, failed = [], [], [], []
        pasture_changes = []
        changes_rows, change_msgs = [], []
        restored = []  # 抹消になっていたが管理馬一覧に載っていた馬

        for h in parsed_horses:
            name = h['name']
            info, err = fetched.get(name, (None, None))
            if err is not None:
                failed.append(f"{name}（{err}）")

            if name not in name_to_index:
                if not info:
                    if err is None:
                        failed.append(f"{name}（情報を取得できませんでした）")
                    continue
                values = jra_info_to_columns(info, all_stables)
                values.setdefault(H_GENDER, h['gender'])
                if trainer_match:
                    values.setdefault(H_AREA, trainer_match['area'])
                    values.setdefault(H_STABLE, trainer_match['display_name'].split('・', 1)[1])
                new_row = [''] * len(headers)
                new_row[H_NAME] = name
                for col, val in values.items():
                    new_row[col] = val
                new_row[H_STATUS] = resolve_trainer_page_status('', h)
                new_row[H_JRA_URL] = h['url']
                new_row[H_REG_STATUS] = new_row[H_STATUS]
                rows.append(new_row)
                name_to_index[name] = len(rows) - 1
                added.append(name)
                continue

            row = rows[name_to_index[name]]

            # 状態（I列）の更新
            current = row[H_STATUS].strip()
            if current == '抹消':
                # 管理馬一覧に載っている＝現役なので、抹消は誤り。一覧の内容から状態を決め直す
                new_status = resolve_trainer_page_status('', h)
                restored.append(name)
            else:
                new_status = resolve_trainer_page_status(current, h)
            if new_status != current:
                row[H_STATUS] = new_status
                status_changed.append(f"{name}：{current or '空欄'}→{new_status}")
                pasture_changes.append((name, current, new_status))

            # 転厩：管理馬一覧の調教師（一致する厩舎が無ければリンク先の調教師名）と拠点・厩舎が異なる場合
            new_stable = None
            if trainer_match:
                new_stable = (trainer_match['area'], trainer_match['display_name'].split('・', 1)[1])
            elif info and info.get('trainer_raw'):
                t_name, t_area = parse_trainer_field(info['trainer_raw'])
                match = find_stable_match(all_stables, t_area, t_name)
                if match:
                    new_stable = (match['area'], match['display_name'].split('・', 1)[1])
            if new_stable and not _blank(row[H_STABLE]) and (row[H_AREA], row[H_STABLE]) != new_stable:
                current_full = f"{row[H_AREA]}・{row[H_STABLE]}" if row[H_AREA] else row[H_STABLE]
                new_full = f"{new_stable[0]}・{new_stable[1]}"
                row[H_AREA], row[H_STABLE] = new_stable
                changes_rows.append([today_str, name, '転厩', current_full, new_full])
                change_msgs.append(f"{name}：転厩 {current_full}→{new_full}")

            # 馬主変更（馬主名が空欄の場合は下の空欄補完で埋める）
            new_owner = info.get('owner') if info else None
            if new_owner and not _blank(row[H_OWNER]) and row[H_OWNER] != new_owner:
                changes_rows.append([today_str, name, '馬主変更', row[H_OWNER], new_owner])
                change_msgs.append(f"{name}：馬主変更 {row[H_OWNER]}→{new_owner}")
                row[H_OWNER] = new_owner

            # 去勢
            if info and row[H_GENDER] == '牡' and info.get('gender') == 'せん':
                row[H_GENDER] = 'せん'
                changes_rows.append([today_str, name, '去勢', '牡', 'せん'])
                change_msgs.append(f"{name}：去勢")

            # 空欄の項目だけ埋める（URLはリンク先を取得できなくても埋める）
            changed = False
            if _blank(row[H_JRA_URL]):
                row[H_JRA_URL] = h['url']
                changed = True
            if info:
                for col, val in jra_info_to_columns(info, all_stables).items():
                    if col in fill_cols and _blank(row[col]):
                        row[col] = val
                        changed = True
            if changed:
                filled.append(name)

        rows.sort(key=lambda r: display_name_of_row(r))
        ws.clear()
        ws.update(range_name='A1', values=[headers] + rows)

        record_pasture_changes(sh, pasture_changes, today_str)
        if restored:
            # 誤って記録された抹消シートの行も取り除く
            try:
                ws_cancel = sh.worksheet("抹消")
                cancel_data = ws_cancel.get_all_values()
                kept = [r for r in cancel_data[1:] if not (len(r) > 1 and r[1] in restored)]
                if len(kept) != len(cancel_data) - 1:
                    ws_cancel.clear()
                    ws_cancel.update(range_name='A1', values=[cancel_data[0]] + kept)
            except gspread.WorksheetNotFound:
                pass
        if changes_rows:
            sh.worksheet("Changes").append_rows(changes_rows)

        record_import_date('import_trainer', as_of_date)
        flash(f"管理馬一覧の取り込み完了（{trainer_name}・{format_date_jp(today_str)}現在・{len(parsed_horses)}頭）："
              f"新規 {len(added)}件／状態更新 {len(status_changed)}件／変更 {len(change_msgs)}件／空欄補完 {len(filled)}件")
        if added:
            flash("新規登録：" + "、".join(added))
        if status_changed:
            flash("状態更新：" + "、".join(status_changed))
        if restored:
            flash("抹消を解除（管理馬一覧に載っているため）：" + "、".join(restored))
        if change_msgs:
            flash("変更（Changesシートに記録）：" + "、".join(change_msgs))
        if filled:
            flash("空欄補完：" + "、".join(filled))
        if failed:
            flash("情報の取得に失敗：" + "、".join(failed))
        return redirect('/import_horses')
    except Exception as e:
        flash(f'登録処理中にエラーが発生しました: {e}')
        return redirect('/import_horses')


def parse_jra_cancel_page(html_text):
    """
    JRAの「抹消一覧」ページ（HTML／MHTMLで保存したもの）を解析し、
    (cancel_date_str, [馬名, ...]) を返す。cancel_date_str は見出し「抹消一覧 2026年10月8日抹消分」の
    "YYYYMMDD"（見つからなければ空文字）。
    """
    soup = BeautifulSoup(html_text, 'html.parser')
    root = soup.select_one('#entry_erasure') or soup

    cancel_date_str = ''
    caption = root.select_one('caption .main')
    if caption:
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', caption.get_text())
        if m:
            cancel_date_str = format_date8(m.group(1), m.group(2), m.group(3))

    names = []
    for th in root.select('tbody th[scope="row"]'):
        name = th.get_text(strip=True)
        if name:
            names.append(name)
    return cancel_date_str, names

@app.route('/import_cancel_horses', methods=['POST'])
@login_required
def import_cancel_horses():
    """JRAの「抹消一覧」ページ（HTML／MHTML保存）をアップロードして、登録済みの馬の状態を抹消にする"""
    files = [f for f in request.files.getlist('cancel_file') if f and f.filename]
    if not files:
        flash('ファイルが選択されていません。')
        return redirect('/import_horses')

    # 複数のファイル（日ごとの抹消一覧）をまとめて取り込む。抹消一覧ではないファイルは飛ばす
    entries = {}        # 馬名 → 抹消日
    file_summaries = []
    cancel_page_dates = []  # 見出しから読み取れた抹消日（最終更新日の記録に使う）
    for file in files:
        try:
            html_text = _extract_html_from_upload(file.read())
            page_error = check_jra_page_kind(html_text, 'cancel')
            if page_error:
                flash(f"{file.filename}：{page_error}")
                continue
            cancel_date_str, names = parse_jra_cancel_page(html_text)
        except Exception as e:
            flash(f'{file.filename}：ファイルの解析に失敗しました: {e}')
            continue
        date_str = cancel_date_str or today8()
        if cancel_date_str:
            cancel_page_dates.append(cancel_date_str)
        for name in names:
            entries.setdefault(name, date_str)
        file_summaries.append(f"{format_date_jp(date_str)}抹消分 {len(names)}頭")

    if not entries:
        flash('抹消馬が見つかりませんでした。ファイルの形式をご確認ください。')
        return redirect('/import_horses')

    try:
        sh = gc.open(horse_data)
        ws = sh.worksheet("Horses")
        data = ws.get_all_values()

        from concurrent.futures import ThreadPoolExecutor

        ws_cancel = get_or_create_worksheet(sh, "抹消", ["年月日", "馬名"])
        cancel_sheet_names = {r[1] for r in ws_cancel.get_all_values()[1:] if len(r) > 1 and r[1]}

        cancelled, already, unregistered = [], [], []
        cancel_rows = []
        status_updates = []  # 状態（I列）の書き込みは途中で止まらないよう最後にまとめて1回で行う
        # 抹消シートに名前が無く、リンク先から抹消日を取得する馬：(馬名, URL, Horsesシートの抹消馬か)
        backfill_targets = []
        registered_names = set()
        for i, row in enumerate(data[1:], start=2):
            name = display_name_of_row(row)
            if not name:
                continue
            registered_names.add(name)
            current = row[H_STATUS].strip() if len(row) > H_STATUS else ''
            if name not in entries:
                if current == '抹消' and name not in cancel_sheet_names:
                    backfill_targets.append((name, row[H_JRA_URL].strip() if len(row) > H_JRA_URL else '', True))
                continue
            date_str = entries[name]
            if name not in cancel_sheet_names:
                cancel_rows.append([date_str, name])
                cancel_sheet_names.add(name)
            if current == '抹消':
                already.append(name)
                continue
            status_updates.append({'range': f'{col_letter(H_STATUS)}{i}', 'values': [['抹消']]})
            cancelled.append(name)

        # 未登録の馬も抹消シートに記録する（週間レポートの抹消馬に、リンクなしで表示する）
        for name, date_str in entries.items():
            if name in registered_names:
                continue
            unregistered.append(name)
            if name not in cancel_sheet_names:
                cancel_rows.append([date_str, name])
                cancel_sheet_names.add(name)

        # Sire・DamシートでJRAのURL（J列）がある馬も対象にする（抹消されていなければ何もしない）
        target_names = {t[0] for t in backfill_targets} | cancel_sheet_names
        for sheet_name in ("Sire", "Dam"):
            try:
                parent_rows = sh.worksheet(sheet_name).get_all_values()[1:]
            except gspread.WorksheetNotFound:
                continue
            for r in parent_rows:
                p_name = display_name_of_row(r)
                p_url = r[P_JRA_URL].strip() if len(r) > P_JRA_URL else ''
                if p_name and p_name not in target_names and is_jra_url(p_url):
                    backfill_targets.append((p_name, p_url, False))
                    target_names.add(p_name)

        # 抹消シートに名前が無い馬は、リンク先（URL）から抹消日を取得して記録する。
        # 一度に大量に取得するとタイムアウトするため、1回の取り込みで処理する頭数に上限を設ける
        backfill_limit = 50
        backfill_targets.sort(key=lambda t: not t[1])  # URLのある馬を先に処理する
        backfilled, backfill_failed = [], []
        targets = backfill_targets[:backfill_limit]

        def _fetch_cancel_date(target):
            name, url, is_cancelled_horse = target
            if not url:
                return name, None, 'URLが未入力'
            try:
                info = fetch_jra_horse_status(url)
            except Exception as e:
                return name, None, str(e)
            if info.get('status') != '抹消' or not info.get('cancel_date'):
                # Sire・Damの馬が抹消されていない場合はエラー扱いにしない
                return name, None, '抹消日を取得できません' if is_cancelled_horse else None
            return name, info['cancel_date'], None

        with ThreadPoolExecutor(max_workers=8) as pool:
            for name, cancel_date, err in pool.map(_fetch_cancel_date, targets):
                if err:
                    backfill_failed.append(f"{name}（{err}）")
                elif cancel_date:
                    cancel_rows.append([cancel_date, name])
                    backfilled.append(f"{name}（{format_date_jp(cancel_date)}）")

        if status_updates:
            ws.batch_update(status_updates)
        if cancel_rows:
            ws_cancel.append_rows(cancel_rows)

        record_import_date('import_cancel', max(cancel_page_dates) if cancel_page_dates else '')
        msg = f"抹消一覧の取り込み完了（{'、'.join(file_summaries)}）：抹消 {len(cancelled)}件"
        if already:
            msg += f"／抹消済み {len(already)}件"
        msg += f"／未登録 {len(unregistered)}件"
        flash(msg)
        if cancelled:
            flash("抹消（抹消シートに記録）：" + "、".join(cancelled))
        if unregistered:
            flash("未登録の馬（抹消シートに記録、週間レポートの厩舎の列にURLボタンを表示）：" + "、".join(unregistered))
        if backfilled:
            flash("抹消日を取得して抹消シートに記録：" + "、".join(backfilled))
        if backfill_failed:
            flash("抹消日の取得に失敗：" + "、".join(backfill_failed))
        if len(backfill_targets) > backfill_limit:
            flash(f"抹消日を確認する馬が残り{len(backfill_targets) - backfill_limit}頭あります。もう一度取り込むと続きを処理します。")
        return redirect('/import_horses')
    except Exception as e:
        flash(f'登録処理中にエラーが発生しました: {e}')
        return redirect('/import_horses')


@app.route('/weekly')
@wait_for_sheets
def weekly():
    """登録馬・抹消馬・放牧入厩・変更を週単位（月曜〜日曜）で一覧表示する（stable指定時はその厩舎の馬だけ）"""
    base = parse_full_date(request.args.get('date', '')) or datetime.now().date()
    stable_filter = request.args.get('stable', '').strip()
    week_start = base - timedelta(days=base.weekday())
    week_end = week_start + timedelta(days=6)

    def in_week(d):
        return d is not None and week_start <= d <= week_end

    def cell(row, i):
        return row[i].strip() if len(row) > i and row[i] else ''

    registered, cancelled, pasture, changes = [], [], [], []
    horse_info = {}   # Horsesシートの馬：馬名 → 性別・生年・厩舎・URLの有無
    parent_info = {}  # Sire・Damシートの馬（Horsesシートに無い馬の性別・URLボタンの判定に使う）
    try:
        sh = gc.open(horse_data)

        horses_data = sh.worksheet("Horses").get_all_values()[1:]
        if not horses_data:
            sheet_not_ready("Horsesシートのデータが0件です")
        for r in horses_data:
            name = display_name_of_row(r)
            if not name:
                continue
            horse_info[name] = {
                'gender': cell(r, H_GENDER), 'birth_year': date_year(cell(r, H_BIRTH)),
                'stable': f"{cell(r, H_AREA)}・{cell(r, H_STABLE)}" if cell(r, H_AREA) else cell(r, H_STABLE),
                'has_url': bool(cell(r, H_JRA_URL) or cell(r, H_NAR_URL)),
            }
            reg_date = parse_full_date(cell(r, H_REGISTRATION))
            if in_week(reg_date):
                # 状態は登録した時の値（O列）で固定する。O列が無い馬は、競走馬登録日と生年から
                # 登録時の状態（早期／空欄）を求める
                status = cell(r, H_REG_STATUS)
                if not status:
                    status = resolve_registration_status('', date_year(cell(r, H_BIRTH)) or '',
                                                         reg_date.year, reg_date.month)
                registered.append({'date': reg_date, 'name': name,
                                   'sire': cell(r, H_SIRE), 'dam': cell(r, H_DAM), 'status': status})

        for p_type, gender in (('Sire', '牡'), ('Dam', '牝')):
            try:
                for r in sh.worksheet(p_type).get_all_values()[1:]:
                    info = {'type': p_type, 'gender': gender, 'birth_year': date_year(cell(r, P_BIRTH)),
                            'has_url': bool(cell(r, P_JRA_URL) or cell(r, P_NAR_URL))}
                    # 和名・英馬名のどちらで記録されていても見つかるようにする
                    for name in (cell(r, P_NAME), cell(r, P_NAME_EN)):
                        if name and name not in parent_info:
                            parent_info[name] = info
            except gspread.WorksheetNotFound:
                pass

        # Changesシートには馬名変更・去勢・転厩・馬主変更を記録する（抹消は抹消シート）
        for r in sh.worksheet("Changes").get_all_values()[1:]:
            d = parse_full_date(cell(r, 0))
            if in_week(d) and cell(r, 2) != '抹消':
                changes.append({'date': d, 'name': cell(r, 1), 'type': cell(r, 2), 'old': cell(r, 3), 'new': cell(r, 4)})

        cancelled_keys = set()
        try:
            for r in sh.worksheet("抹消").get_all_values()[1:]:
                d = parse_full_date(cell(r, 0))
                if in_week(d) and (d, cell(r, 1)) not in cancelled_keys:
                    cancelled_keys.add((d, cell(r, 1)))
                    cancelled.append({'date': d, 'name': cell(r, 1), 'type': '抹消', 'old': '', 'new': '抹消'})
        except gspread.WorksheetNotFound:
            pass

        try:
            for r in sh.worksheet("放牧入厩").get_all_values()[1:]:
                out_date, in_date = parse_full_date(cell(r, 0)), parse_full_date(cell(r, 2))
                if in_week(out_date):
                    pasture.append({'date': out_date, 'name': cell(r, 1), 'type': '放牧'})
                if in_week(in_date):
                    # 入厩は元の状態も表示する（D列が無い記録は、放牧日がある行なら放牧から）
                    before = cell(r, 3) or ('放牧' if out_date else '')
                    label = f"{before}→入厩" if before in ('放牧', '早期') else '入厩'
                    pasture.append({'date': in_date, 'name': cell(r, 1), 'type': label})
        except gspread.WorksheetNotFound:
            pass
    except SheetNotReady:
        raise
    except Exception as e:
        sheet_not_ready(f"週間レポートのデータを読み込めませんでした: {e}")
        flash(f"データの読み込みに失敗しました: {e}")

    for items in (registered, cancelled, pasture, changes):
        for item in items:
            # Horsesシートに登録されている馬だけ詳細ページへのリンクを付ける。厩舎の列には、
            # 未登録の馬は競走馬の登録、URLが無い馬は編集画面へ進む「URL」ボタンを表示する
            # （Sire・Damシートにある馬は親馬の登録画面へ進む）
            info = horse_info.get(item['name'])
            parent = None if info else parent_info.get(item['name'])
            item['linkable'] = info is not None
            item['parent_type'] = parent['type'] if parent else ''
            item['has_url'] = (info or parent or {}).get('has_url', False)
            item['stable'] = info['stable'] if info else ''
            item['gender'] = (info or parent or {}).get('gender', '')
            birth_year = (info or parent or {}).get('birth_year')
            item['age'] = f"{item['date'].year - birth_year}歳" if birth_year else ''
            # 年は見出しの週でわかるため、日付は月日と曜日だけ表示する
            item['date_label'] = f"{item['date'].month}月{item['date'].day}日({JP_WEEKDAYS[item['date'].weekday()]})"
        items.sort(key=lambda x: (x['date'], x['name']))

    if stable_filter:
        def _match(item):
            # 転厩は、転厩元・転厩先のどちらかがその厩舎なら表示する
            return item['stable'] == stable_filter or (
                item.get('type') == '転厩' and stable_filter in (item.get('old'), item.get('new')))
        registered, cancelled, pasture, changes = (
            [i for i in items if _match(i)] for items in (registered, cancelled, pasture, changes))

    # Horsesシートに無い馬（Sire・Damシートにある馬はURLが無い場合だけ）：JRAから登録するボタンに表示する
    unregistered_count = len({i['name'] for items in (registered, cancelled, pasture, changes) for i in items
                              if not i['linkable'] and not (i['parent_type'] and i['has_url'])})
    return render_template('weekly.html',
                           unregistered_count=unregistered_count,
                           registered=registered, cancelled=cancelled, pasture=pasture, changes=changes,
                           week_label=f"{format_date_jp(week_start, weekday=True)}〜{format_date_jp(week_end, weekday=True)}",
                           stables=get_stables_list(), stable=stable_filter,
                           date_for_input=base.strftime('%Y-%m-%d'),
                           prev_week=(week_start - timedelta(days=7)).strftime('%Y%m%d'),
                           next_week=(week_start + timedelta(days=7)).strftime('%Y%m%d'),
                           import_status=get_import_status(), schedule_note=IMPORT_SCHEDULE_NOTE)


@app.route('/add_horse', methods=['POST'])
@login_required
def add_horse():
    try:
        sh = gc.open(horse_data)
        ws = sh.worksheet("Horses")
        name = (request.form.get('name') or '').strip()
        name_en = (request.form.get('name_en') or '').strip()
        # 馬名が英字の場合はB列（英馬名）に記録する
        if is_english_name(name):
            name, name_en = '', name
        y, m, d = request.form.get('year'), request.form.get('month'), request.form.get('day')
        birth_date_str = format_date8(y, m, d)

        lookup_name = name or name_en
        if any(row_matches_name(r, lookup_name) for r in ws.get_all_values()[1:]):
            flash(f"エラー：『{lookup_name}』は既に登録されています。")
            return redirect('/add_horse')
            
        # 競走馬登録日（未入力可）。年・月が揃っている場合のみ、状態（早期）の判定に使う
        reg_y = (request.form.get('reg_year') or '').strip()
        reg_m = (request.form.get('reg_month') or '').strip()
        reg_d = (request.form.get('reg_day') or '').strip()
        registration_str = format_date8(reg_y, reg_m, reg_d) if reg_y and reg_m else ''
        status = resolve_registration_status(request.form.get('status'), y, reg_y, reg_m)

        # 競走馬登録列（P列）・登録時状態列（Q列）の見出しが無ければ追加する
        header = ws.row_values(1)
        if len(header) < len(HORSE_COLUMNS):
            ensure_columns(ws, len(HORSE_COLUMNS))
            ws.update(range_name=f'{col_letter(H_REGISTRATION)}1:{col_letter(H_REG_STATUS)}1',
                      values=[[HORSE_COLUMNS[H_REGISTRATION], HORSE_COLUMNS[H_REG_STATUS]]])

        jra_url, nar_url = split_horse_urls(request.form.get('jra_url'), request.form.get('nar_url'))
        kaku_error = jra_url_kaku_error(jra_url)
        if kaku_error:
            flash(f"エラー：{kaku_error}")
            return redirect('/add_horse')
        new_row = [''] * len(HORSE_COLUMNS)
        new_row[H_NAME] = name
        new_row[H_NAME_EN] = name_en
        new_row[H_GENDER] = request.form.get('gender')
        new_row[H_BIRTH] = birth_date_str
        new_row[H_SIRE] = request.form.get('sire')
        new_row[H_DAM] = request.form.get('dam')
        new_row[H_OWNER] = request.form.get('owner')
        new_row[H_AREA] = request.form.get('area')
        new_row[H_STABLE] = request.form.get('stable_name')
        new_row[H_STATUS] = status
        new_row[H_REGION] = request.form.get('birthplace_region')
        new_row[H_DETAIL] = request.form.get('birthplace_detail')
        new_row[H_BREEDER] = request.form.get('breeder')
        new_row[H_JRA_URL] = jra_url
        new_row[H_NAR_URL] = nar_url
        new_row[H_REGISTRATION] = registration_str
        new_row[H_REG_STATUS] = status   # 登録時状態（週間レポートの登録馬に表示する）
        ws.append_row(new_row)
        sort_and_resize_table(ws, sort_col_index=0)
        record_cancel_date_from_url(sh, lookup_name, jra_url)
        return redirect(f"/horse/{lookup_name}")
    except Exception as e:
        flash(f"エラーが発生しました: {e}")
        return redirect('/add_horse')

@app.route('/register')
@login_required
def register():
    """データ登録のトップ：競走馬の登録／厩舎の登録／HTMLのアップロードのボタンを表示する"""
    return render_template('register.html')

@app.route('/add_stable', methods=['GET', 'POST'])
@login_required
def add_stable():
    if request.method == 'GET':
        return render_template('add_stable.html')

    name = (request.form.get('stable_name') or '').strip()
    # ヨミガナはカタカナに変換し、空白はそのまま残す（前後の空白だけ除く）
    kana = hira_to_kana((request.form.get('kana') or '').strip())
    area = request.form.get('area')
    year, month, day = request.form.get('year'), request.form.get('month'), request.form.get('day')
    birth_date_str = format_date8(year, month, day) if year and month and day else ""
    license_year = (request.form.get('license_year') or '').strip()

    # 開業年月日・引退年月日（任意。生年月日と同じく年・月・日で入力）
    def _optional_date(prefix):
        y = (request.form.get(prefix + 'year') or '').strip()
        m = (request.form.get(prefix + 'month') or '').strip()
        d = (request.form.get(prefix + 'day') or '').strip()
        return format_date8(y, m, d if m else '') if y else ''
    opening = _optional_date('opening_')
    retirement = _optional_date('retirement_')

    if not (name and kana and birth_date_str and license_year and area in ["美浦", "栗東"]):
        flash("厩舎名・ヨミガナ・拠点・生年月日・免許取得年を入力してください。")
        return redirect('/add_stable')

    try:
        sh = gc.open(horse_data)
        sheet = sh.worksheet(area)
        existing_names = sheet.col_values(1)

        if name in existing_names[1:]:
            flash(f"エラー: {name}厩舎は既に{area}に登録されています。")
            return redirect('/add_stable')

        # 馬房数（G列）・臨時貸付（H列）・貸付終了（I列）は「馬房数を変更する」で管理する
        sheet.append_row([
            name,
            kana,
            birth_date_str,
            license_year,
            opening,      # E列: 開業
            retirement,   # F列: 引退
        ])

        sort_and_resize_table(sheet, sort_col_index=1, key=stable_kana_key)
        flash(f"{area}・{name}厩舎を登録しました。", 'success')
    except Exception as e:
        flash(f"厩舎の追加に失敗しました: {e}")
    return redirect('/add_stable')

@app.route('/stable_capacity')
@login_required
def stable_capacity():
    """美浦／栗東の厩舎ごとの馬房数・臨時貸付などを一覧表示する（引退済みの厩舎は除く）"""
    area = request.args.get('area', '美浦')
    if area not in ("美浦", "栗東"):
        area = "美浦"

    stables = []
    try:
        rows = gc.open(horse_data).worksheet(area).get_all_values()[1:]
        today = datetime.now().date()

        def cell(r, i):
            return r[i].strip() if len(r) > i and r[i] else ''

        for r in rows:
            if not cell(r, 0):
                continue
            retirement = parse_full_date(cell(r, 5))
            if retirement and retirement <= today:
                continue
            stables.append({
                'name': cell(r, 0), 'kana': cell(r, 1), 'birth_date': cell(r, 2),
                'license_year': cell(r, 3), 'opening': cell(r, 4), 'capacity': cell(r, 6),
                'temp_loan': cell(r, 7), 'loan_end': cell(r, 8),
            })
        stables.sort(key=lambda x: stable_kana_key(x['kana']))
    except Exception as e:
        flash(f"厩舎データの読み込みに失敗しました: {e}")

    return render_template('stable_capacity.html', area=area, stables=stables)

def is_nar_url(url):
    """地方競馬情報サイト（keiba.go.jp）のURLかどうか"""
    from urllib.parse import urlparse
    netloc = urlparse(url or '').netloc
    return any(netloc == h or netloc.endswith('.' + h) for h in NAR_ALLOWED_HOSTS)

def split_horse_urls(jra_url, nar_url=''):
    """JRAのURL欄に keiba.go.jp のURLが入力された場合は、NARのURLとして扱う。(JRAのURL, NARのURL) を返す"""
    jra_url, nar_url = (jra_url or '').strip(), (nar_url or '').strip()
    if is_nar_url(jra_url):
        nar_url, jra_url = nar_url or jra_url, ''
    return jra_url, nar_url

def jra_url_kaku_error(url):
    """JRAのURLの馬が地方・海外所属ならエラーの文を返す（ページを取得できない場合は止めない）"""
    if not is_jra_url(url):
        return None
    try:
        return JRA_KAKU_ERROR if fetch_jra_horse_info(url).get('kaku') else None
    except Exception:
        return None

def is_jra_url(url):
    """JRA公式サイトのURLかどうか"""
    from urllib.parse import urlparse
    parsed = urlparse(url or '')
    return parsed.scheme in ("http", "https") and any(parsed.netloc.endswith(h) for h in JRA_ALLOWED_HOSTS)

def record_cancel_date_from_url(sh, horse_name, url):
    """抹消シートに名前が無ければ、JRAのURLのリンク先から抹消日を取得して抹消シートに記録する。
    記録した抹消日を返す（抹消されていない・取得できない場合はNone）。保存処理を止めないよう例外は外に出さない"""
    try:
        if not horse_name or not is_jra_url(url):
            return None
        ws_cancel = get_or_create_worksheet(sh, "抹消", ["年月日", "馬名"])
        if any(len(r) > 1 and r[1] == horse_name for r in ws_cancel.get_all_values()[1:]):
            return None
        info = fetch_jra_horse_status(url)
        if info.get('status') == '抹消' and info.get('cancel_date'):
            ws_cancel.append_row([info['cancel_date'], horse_name])
            return info['cancel_date']
    except Exception as e:
        print(f"抹消日の記録に失敗しました（{horse_name}）: {e}")
    return None

def record_pasture_change(sh, horse_name, old_status, new_status, today_str):
    """放牧／入厩の切り替えを履歴（放牧入厩シート）に残す（「早期」→「入厩」の切り替えも含む）"""
    record_pasture_changes(sh, [(horse_name, old_status, new_status)], today_str)

def record_pasture_changes(sh, changes, today_str):
    """複数頭の放牧／入厩の切り替えをまとめて放牧入厩シートに記録する。
    Googleの1分あたりの読み書き回数の上限にかからないよう、読み込み1回・書き込みは最大2回にまとめる。
    changes: [(馬名, 旧状態, 新状態), ...]"""
    changes = [(n, o, nw) for n, o, nw in changes
               if o in ('放牧', '入厩', '早期') and nw in ('放牧', '入厩')]
    if not changes:
        return
    ws_pasture = get_or_create_worksheet(sh, "放牧入厩", ["放牧年月日", "馬名", "入厩年月日", "入厩前の状態"])
    pasture_data = ws_pasture.get_all_values()
    # D列：入厩した時の元の状態（早期→入厩・放牧→入厩を週間レポートに表示するため）
    ensure_columns(ws_pasture, 4)
    header = pasture_data[0] if pasture_data else []
    if len(header) < 4 or not header[3]:
        ws_pasture.update(range_name='D1', values=[['入厩前の状態']])

    append_rows, cell_updates = [], []
    for horse_name, old_status, new_status in changes:
        if new_status == '放牧':
            append_rows.append([today_str, horse_name, ''])
            continue
        # 入厩：入厩日が空欄の最新の放牧の行に入厩日を入れる（無ければ入厩日だけの行を追加）
        open_row_idx = None
        for idx in range(len(pasture_data) - 1, 0, -1):
            prow = pasture_data[idx]
            p_name = prow[1] if len(prow) > 1 else ''
            p_checkin = prow[2] if len(prow) > 2 else ''
            if p_name == horse_name and not p_checkin:
                open_row_idx = idx
                break
        if open_row_idx is not None:
            cell_updates.append({'range': f'C{open_row_idx + 1}:D{open_row_idx + 1}',
                                 'values': [[today_str, old_status]]})
        else:
            append_rows.append(['', horse_name, today_str, old_status])

    if cell_updates:
        ws_pasture.batch_update(cell_updates)
    if append_rows:
        ws_pasture.append_rows(append_rows)

# ==============================================================================
# 英馬名の列（B列）・NARのURL列の追加と、同一馬のまとめ（移行作業）
# ==============================================================================
NAME_SHEETS = ("Horses", "Sire", "Dam")
_name_columns_ready = False

def name_columns_ready():
    """Horses・Sire・DamシートのB列が「英馬名」になっている（列の追加が済んでいる）か"""
    global _name_columns_ready
    if _name_columns_ready:
        return True
    try:
        sh = gc.open(horse_data)
        for title in NAME_SHEETS:
            try:
                header = sh.worksheet(title).row_values(1)
            except gspread.WorksheetNotFound:
                continue
            if len(header) < 2 or header[1] != '英馬名':
                return False
        _name_columns_ready = True
    except Exception:
        return True  # シートを確認できない場合は各画面のエラー表示に任せる
    return _name_columns_ready

@app.before_request
def require_name_columns():
    """列の追加が済むまでは、古い列の並びのまま読み書きしないよう移行画面へ案内する"""
    if request.endpoint in (None, 'static', 'login', 'logout', 'migrate_names', 'migrate_name_columns'):
        return None
    if not name_columns_ready():
        if request.path.startswith('/api/'):
            return jsonify({"error": "スプレッドシートの列の追加が必要です。「英馬名の移行」画面で実行してください。"}), 409
        return redirect(url_for('migrate_names'))
    return None

def convert_row_to_name_columns(row, is_horses):
    """古い列の並びの1行を、英馬名（B列）・NARのURL列を追加した並びに変換する。
    英字の馬名はB列に移し、JRAのURL列にある keiba.go.jp のURLはNARのURL列に移す"""
    row = list(row)
    url_idx = 12 if is_horses else 8   # 古い並びでのURL列
    base = row + [''] * max(0, url_idx + 1 - len(row))
    new_row = [base[0], ''] + base[1:url_idx] + [base[url_idx], ''] + base[url_idx + 1:]
    name_col, en_col = 0, 1
    jra_col = H_JRA_URL if is_horses else P_JRA_URL
    nar_col = H_NAR_URL if is_horses else P_NAR_URL
    if is_english_name(new_row[name_col]):
        new_row[en_col], new_row[name_col] = new_row[name_col].strip(), ''
    if is_nar_url(new_row[jra_col]):
        new_row[nar_col], new_row[jra_col] = new_row[jra_col], ''
    return new_row

def migrate_sheet_to_name_columns(sh, title):
    """1枚のシートの列を追加する。実行前にシートを複製してバックアップを残す。変換した行数を返す"""
    ws = sh.worksheet(title)
    data = ws.get_all_values()
    if data and len(data[0]) > 1 and data[0][1] == '英馬名':
        return 0
    is_horses = title == "Horses"
    columns = HORSE_COLUMNS if is_horses else PARENT_COLUMNS
    sh.duplicate_sheet(ws.id, new_sheet_name=f"{title}_移行前_{datetime.now().strftime('%Y%m%d%H%M')}")

    old_header = data[0] if data else []
    header = convert_row_to_name_columns(old_header, is_horses)
    for i, name in enumerate(columns):
        while len(header) <= i:
            header.append('')
        header[i] = name
    rows = [convert_row_to_name_columns(r, is_horses) for r in data[1:]]
    width = max([len(header)] + [len(r) for r in rows])
    ensure_columns(ws, width)
    # 列が増えるだけで行数は変わらないため、消さずにそのまま上書きする
    ws.update(range_name='A1', values=[header] + rows)
    return len(rows)

@app.route('/migrate_names')
@login_required
def migrate_names():
    """英馬名の移行：①列の追加 ②JRAのURLから英馬名の取得 ③同一馬のまとめ"""
    ready = name_columns_ready()
    targets = get_name_fetch_targets() if ready else []
    groups = find_duplicate_horse_groups() if ready else []
    return render_template('migrate_names.html', ready=ready, target_count=len(targets),
                           group_count=len(groups))

@app.route('/migrate_names/columns', methods=['POST'])
@login_required
def migrate_name_columns():
    global _name_columns_ready
    try:
        sh = gc.open(horse_data)
        results = []
        for title in NAME_SHEETS:
            try:
                count = migrate_sheet_to_name_columns(sh, title)
            except gspread.WorksheetNotFound:
                continue
            results.append(f"{title} {count}行")
        _name_columns_ready = False
        name_columns_ready()
        flash("英馬名・NARのURLの列を追加しました（" + "、".join(results) + "）。元のシートは「〇〇_移行前_日時」として残しています。")
    except Exception as e:
        flash(f"列の追加に失敗しました: {e}")
    return redirect(url_for('migrate_names'))

def get_name_fetch_targets():
    """JRAのURLがあり、英馬名（または和名）が空欄の馬：[(シート名, 行番号, URL), ...]"""
    targets = []
    sh = gc.open(horse_data)
    for title in NAME_SHEETS:
        try:
            data = sh.worksheet(title).get_all_values()
        except gspread.WorksheetNotFound:
            continue
        jra_col = H_JRA_URL if title == "Horses" else P_JRA_URL
        for i, r in enumerate(data[1:], start=2):
            url = r[jra_col].strip() if len(r) > jra_col else ''
            name_ja = r[0].strip() if r else ''
            name_en = r[1].strip() if len(r) > 1 else ''
            if is_jra_url(url) and (not name_en or not name_ja):
                targets.append((title, i, url))
    return targets

def fetch_jra_names(url):
    """JRAの競走馬情報ページから (馬名, 英馬名) を取得する"""
    soup = _fetch_jra_soup(url)
    name = ''
    opt_span = soup.find('span', class_='opt', string=lambda s: s and '競走馬情報' in s)
    if opt_span:
        name = _text_after(opt_span) or ''
    name_en_span = soup.select_one('span.name_en')
    name_en_raw = name_en_span.get_text(strip=True) if name_en_span else ''
    if name_en_raw and name.endswith(name_en_raw):
        name = name[:-len(name_en_raw)].strip()
    return name.strip(), _strip_country_suffix(name_en_raw) or ''

@app.route('/api/name_fetch_targets')
@login_required
def api_name_fetch_targets():
    targets = get_name_fetch_targets()
    return jsonify({"targets": [{"sheet": t, "row": r, "url": u} for t, r, u in targets]})

@app.route('/api/fetch_names', methods=['POST'])
@login_required
def api_fetch_names():
    """指定された馬（最大20頭）のJRAのURLから英馬名を取得し、空欄の馬名・英馬名を埋める"""
    from concurrent.futures import ThreadPoolExecutor
    items = (request.get_json(silent=True) or {}).get('targets', [])[:20]
    sh = gc.open(horse_data)
    sheets = {}
    results = []

    def _fetch(item):
        try:
            return item, fetch_jra_names(item['url']), None
        except Exception as e:
            return item, None, str(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = list(pool.map(_fetch, items))

    updates_by_sheet = {}
    for item, names, err in fetched:
        title, row_no = item.get('sheet'), int(item.get('row', 0))
        if title not in NAME_SHEETS or row_no < 2:
            continue
        if err or not names:
            results.append({"row": row_no, "sheet": title, "error": err or '取得できませんでした'})
            continue
        if title not in sheets:
            sheets[title] = sh.worksheet(title)
            sheets[title + ':data'] = sheets[title].get_all_values()
        data = sheets[title + ':data']
        row = data[row_no - 1] if row_no - 1 < len(data) else []
        jra_col = H_JRA_URL if title == "Horses" else P_JRA_URL
        # 行の並びが変わっていないか、URLで確認してから書き込む
        if len(row) <= jra_col or row[jra_col].strip() != item['url']:
            results.append({"row": row_no, "sheet": title, "error": '行が移動したため飛ばしました'})
            continue
        name_ja, name_en = names
        new_a = row[0].strip() or (name_ja if name_ja and not is_english_name(name_ja) else '')
        new_b = (row[1].strip() if len(row) > 1 else '') or name_en
        updates_by_sheet.setdefault(title, []).append({'range': f'A{row_no}:B{row_no}', 'values': [[new_a, new_b]]})
        results.append({"row": row_no, "sheet": title, "name": new_a or new_b, "name_en": new_b})

    for title, updates in updates_by_sheet.items():
        sheets[title].batch_update(updates)
    return jsonify({"results": results})

def find_duplicate_horse_groups():
    """母と生年が同じで同一馬と思われる行のまとまりを、Horses・Sire・Damシートごとに探す。
    [{'sheet', 'key', 'rows': [(行番号, 行)], 'columns': [{'index', 'label', 'values', 'conflict'}]}, ...]"""
    groups = []
    sh = gc.open(horse_data)
    for title in NAME_SHEETS:
        try:
            data = sh.worksheet(title).get_all_values()
        except gspread.WorksheetNotFound:
            continue
        if not data:
            continue
        header = data[0]
        is_horses = title == "Horses"
        dam_col, birth_col = (H_DAM, H_BIRTH) if is_horses else (P_DAM, P_BIRTH)
        buckets = {}
        for i, r in enumerate(data[1:], start=2):
            dam = r[dam_col].strip() if len(r) > dam_col else ''
            year = date_year(r[birth_col]) if len(r) > birth_col else None
            if dam and year and display_name_of_row(r):
                buckets.setdefault((dam, year), []).append((i, r))
        for (dam, year), rows in buckets.items():
            if len(rows) < 2:
                continue
            width = max(len(header), max(len(r) for _, r in rows))
            columns = []
            for c in range(width):
                values = []
                for _, r in rows:
                    v = r[c].strip() if len(r) > c else ''
                    if v and v not in values:
                        values.append(v)
                # 生年月日は「2015」と「20150401」のように詳しさが違うだけなら、詳しい方を使う
                if c == birth_col and len({date_year(v) for v in values}) == 1:
                    values = [max(values, key=len)]
                columns.append({'index': c, 'label': header[c] if c < len(header) and header[c] else col_letter(c),
                                'values': values, 'conflict': len(values) > 1})
            groups.append({'sheet': title, 'key': f"{dam}|{year}", 'dam': dam, 'year': year,
                           'rows': rows, 'columns': columns,
                           'signature': ','.join(f"{i}:{display_name_of_row(r)}" for i, r in rows)})
    return groups

@app.route('/merge_horses', methods=['GET', 'POST'])
@login_required
def merge_horses():
    """同一馬と思われる行を1行にまとめる。値が異なる項目は、どちらを優先するか画面で選ぶ"""
    if request.method == 'GET':
        try:
            groups = find_duplicate_horse_groups()
        except Exception as e:
            flash(f"データの読み込みに失敗しました: {e}")
            groups = []
        return render_template('merge_horses.html', groups=groups)

    try:
        groups = find_duplicate_horse_groups()
        sh = gc.open(horse_data)
        merged_names = []
        by_sheet = {}
        for g_idx, g in enumerate(groups):
            field = f"g{g_idx}"
            if request.form.get(field + '_merge') != '1':
                continue
            # 画面を表示した後にシートが変わっていたら、そのまとまりは飛ばす
            if request.form.get(field + '_sig') != g['signature']:
                flash(f"{g['sheet']}：{g['dam']}の{g['year']}年生は、表示後に内容が変わったため飛ばしました。")
                continue
            merged = []
            for col in g['columns']:
                if col['conflict']:
                    try:
                        merged.append(col['values'][int(request.form.get(f"{field}_c{col['index']}", 0))])
                    except (ValueError, IndexError):
                        merged.append(col['values'][0])
                else:
                    merged.append(col['values'][0] if col['values'] else '')
            by_sheet.setdefault(g['sheet'], []).append((g, merged))
            merged_names.append(display_name_of_row(merged))

        for title, items in by_sheet.items():
            ws = sh.worksheet(title)
            data = ws.get_all_values()
            remove_rows, replace = set(), {}
            for g, merged in items:
                row_numbers = [i for i, _ in g['rows']]
                replace[row_numbers[0]] = merged
                remove_rows.update(row_numbers[1:])
            new_data = [data[0]]
            for i, r in enumerate(data[1:], start=2):
                if i in remove_rows:
                    continue
                new_data.append(replace.get(i, r))
            width = max(len(r) for r in data + new_data)
            new_data = [list(r) + [''] * (width - len(r)) for r in new_data]
            ws.update(range_name='A1', values=new_data)
            if len(data) > len(new_data):
                ws.batch_clear([f"A{len(new_data) + 1}:{col_letter(width - 1)}{len(data)}"])
            sort_and_resize_table(ws, sort_col_index=0)

        flash(f"{len(merged_names)}頭をまとめました" + ("：" + "、".join(merged_names) if merged_names else "。"))
    except Exception as e:
        flash(f"まとめる処理に失敗しました: {e}")
    return redirect(url_for('merge_horses'))

@app.route('/api/get_target_horses', methods=['GET'])
@login_required
def get_target_horses():
    """1. 更新対象となる馬の行番号（row_index）リストを取得するAPI"""
    try:
        sh = gc.open(horse_data)
        ws_horses = sh.worksheet("Horses")
        horses_data = ws_horses.get_all_values()

        target_row_indexes = []
        for i, row in enumerate(horses_data[1:], start=2):  # 1行目はヘッダーなので2行目から
            status = row[H_STATUS] if len(row) > H_STATUS else ""
            url = row[H_JRA_URL] if len(row) > H_JRA_URL else ""
            if status == "抹消" or not url:
                continue
            target_row_indexes.append(i)  # 行番号をIDとして保持

        return jsonify({
            "status": "success",
            "total": len(target_row_indexes),
            "horse_ids": target_row_indexes
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"対象リストの取得に失敗しました: {e}"}), 500


@app.route('/api/update_single_horse/<int:row_index>', methods=['POST'])
@login_required
def update_single_horse(row_index):
    """2. 指定された行番号の馬1頭の情報をスクレイピングして即時スプレッドシート更新するAPI"""
    try:
        sh = gc.open(horse_data)
        ws_horses = sh.worksheet("Horses")
        
        # 対象行のデータを取得
        row = ws_horses.row_values(row_index)
        status = row[H_STATUS] if len(row) > H_STATUS else ""
        url = row[H_JRA_URL] if len(row) > H_JRA_URL else ""

        if status == "抹消" or not url:
            return jsonify({"status": "skipped", "message": "対象外の馬です"})

        horse_name = display_name_of_row(row)
        current_gender = row[H_GENDER] if len(row) > H_GENDER else ""
        current_owner = row[H_OWNER] if len(row) > H_OWNER else ""
        current_area = row[H_AREA] if len(row) > H_AREA else ""
        current_stable = row[H_STABLE] if len(row) > H_STABLE else ""

        # JRA公式サイトから最新情報を取得
        info = fetch_jra_horse_status(url)

        ws_changes = sh.worksheet("Changes")
        all_stables = get_stables_list()
        today_str = today8()

        # ① 抹消判定
        if info.get('status') == '抹消':
            ws_horses.update(f'{col_letter(H_STATUS)}{row_index}', [['抹消']])
            ws_cancel = get_or_create_worksheet(sh, "抹消", ["年月日", "馬名"])
            ws_cancel.append_row([info.get('cancel_date', ''), horse_name])
            return jsonify({"status": "success", "result": "抹消", "horse_name": horse_name})

        # ② 放牧／入厩
        new_status = info.get('status', '入厩')
        # 状態が「早期」の馬は、入厩になるまで「放牧」に変更しない
        if status == '早期' and new_status == '放牧':
            new_status = status
        if new_status != status:
            ws_horses.update(f'{col_letter(H_STATUS)}{row_index}', [[new_status]])

            record_pasture_change(sh, horse_name, status, new_status, today_str)

        # ③ 去勢判定
        if current_gender == '牡' and info.get('gender') == 'せん':
            ws_horses.update(f'{col_letter(H_GENDER)}{row_index}', [['せん']])
            ws_changes.append_row([today_str, horse_name, '去勢'])

        # ④-1 馬主名変更
        new_owner = info.get('owner')
        if new_owner and new_owner != current_owner:
            ws_horses.update(f'{col_letter(H_OWNER)}{row_index}', [[new_owner]])
            ws_changes.append_row([today_str, horse_name, '馬主変更', current_owner, new_owner])

        # ④-2 調教師名（厩舎）変更
        trainer_raw = info.get('trainer_raw')
        if trainer_raw:
            t_name, t_area = parse_trainer_field(trainer_raw)
            match = find_stable_match(all_stables, t_area, t_name)
            if match:
                new_area = match['area']
                new_stable = match['display_name'].split('・', 1)[1] if '・' in match['display_name'] else match['display_name']
                if new_stable != current_stable or new_area != current_area:
                    current_full = f"{current_area}・{current_stable}" if current_area else current_stable
                    new_full = f"{new_area}・{new_stable}" if new_area else new_stable
                    ws_horses.update(f'{col_letter(H_AREA)}{row_index}:{col_letter(H_STABLE)}{row_index}',
                                     [[new_area, new_stable]])
                    ws_changes.append_row([today_str, horse_name, '転厩', current_full, new_full])

        return jsonify({"status": "success", "horse_name": horse_name})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/finish_update_horses', methods=['POST'])
@login_required
def finish_update_horses():
    """3. 全ての更新完了後に最終更新日時を設定するAPI"""
    try:
        set_last_updated(datetime.now().strftime('%Y%m%d %H:%M'))
        return jsonify({"status": "success", "message": "データ更新が完了しました。"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/add_parent', methods=['GET', 'POST'])
@login_required
def add_parent():
    if request.method == 'POST':
        origin, p_type, p_name = request.form.get('origin'), request.form.get('p_type'), request.form.get('p_name')
        try:
            sh = gc.open(horse_data)
            try:
                ws = sh.worksheet(p_type)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title=p_type, rows=100, cols=len(PARENT_COLUMNS))
                ws.append_row(PARENT_COLUMNS)

            y, m, d = request.form.get('year'), request.form.get('month'), request.form.get('day')
            birth_date_str = format_date8(y, m, d) if y and m and d else (str(y) if y else "")

            # 馬名は和名ならA列、英字ならB列（英馬名）。画面で入力したもう一方の表記も記録する
            p_name = (p_name or '').strip()
            if is_english_name(p_name):
                name_ja, name_en = (request.form.get('name_ja') or '').strip(), p_name
            else:
                name_ja, name_en = p_name, (request.form.get('name_en') or '').strip()
            jra_url, nar_url = split_horse_urls(request.form.get('jra_url'), request.form.get('nar_url'))
            kaku_error = jra_url_kaku_error(jra_url)
            if kaku_error:
                flash(f"エラー：{kaku_error}")
                return redirect(url_for('add_parent', p_type=p_type, p_name=p_name, origin=origin or ''))

            # 「同名の馬を登録する」がチェックされている場合は、既存の同名データを
            # 上書きせず、常に新規レコードとして追加する
            force_new = request.form.get('force_new') == '1'

            data = ws.get_all_values()
            found_idx = None
            if not force_new:
                for target in (p_name, name_ja, name_en):
                    found_idx = next((i + 1 for i, row in enumerate(data) if i > 0 and row_matches_name(row, target)), None)
                    if found_idx:
                        break

            new_row = [''] * len(PARENT_COLUMNS)
            if found_idx:
                old = list(data[found_idx - 1]) + [''] * len(PARENT_COLUMNS)
                # 名前は入力が空欄なら既存の値を残す
                name_ja = name_ja or old[P_NAME].strip()
                name_en = name_en or old[P_NAME_EN].strip()
                nar_url = nar_url or old[P_NAR_URL].strip()
            new_row[P_NAME] = name_ja
            new_row[P_NAME_EN] = name_en
            new_row[P_BIRTH] = birth_date_str
            new_row[P_SIRE] = request.form.get('sire')
            new_row[P_DAM] = request.form.get('dam')
            new_row[P_OWNER] = request.form.get('owner')
            new_row[P_REGION] = request.form.get('birthplace_region')
            new_row[P_DETAIL] = request.form.get('birthplace_detail')
            new_row[P_BREEDER] = request.form.get('breeder')
            new_row[P_JRA_URL] = jra_url
            new_row[P_NAR_URL] = nar_url

            ensure_columns(ws, len(PARENT_COLUMNS))
            if found_idx:
                ws.update(range_name=f'A{found_idx}:{col_letter(len(PARENT_COLUMNS) - 1)}{found_idx}', values=[new_row])
            else:
                ws.append_row(new_row)
            sort_and_resize_table(ws, sort_col_index=0)
            record_cancel_date_from_url(sh, p_name, jra_url)
        except Exception as e:
            flash(f"エラーが発生しました: {e}")
        return redirect(f"/horse/{origin}") if origin else redirect('/')

    # GET時のデータ復元
    p_type, p_name = request.args.get('p_type', 'Sire'), request.args.get('p_name', '')
    existing_data = {
        "year": "", "month": "", "day": "", "sire": "", "dam": "", 
        "owner": "", "birthplace_region": "", "birthplace_detail": "", "breeder": "", "URL": "",
        "nar_url": "", "name_ja": "", "name_en": ""
    }
    existing_found = False  # 同名の馬が既に登録されていて、そのデータを読み込んだか
    try:
        sh = gc.open(horse_data)
        ws = sh.worksheet(p_type)
        row = next((r for r in ws.get_all_values()[1:] if row_matches_name(r, p_name)), None)
        if row:
            existing_found = True
            row = list(row) + [''] * len(PARENT_COLUMNS)
            parts = split_date(row[P_BIRTH])
            if parts:
                existing_data["year"] = str(parts[0])
                if parts[1]: existing_data["month"] = str(parts[1])
                if parts[2]: existing_data["day"] = str(parts[2])

            existing_data["name_ja"] = row[P_NAME]
            existing_data["name_en"] = row[P_NAME_EN]
            existing_data["sire"] = row[P_SIRE]
            existing_data["dam"] = row[P_DAM]
            existing_data["owner"] = row[P_OWNER]
            existing_data["birthplace_region"] = row[P_REGION]
            existing_data["birthplace_detail"] = row[P_DETAIL]
            existing_data["breeder"] = row[P_BREEDER]
            existing_data["URL"] = row[P_JRA_URL]
            existing_data["nar_url"] = row[P_NAR_URL]
    except: pass
    return render_template(
        'add_parent.html', 
        p_type=p_type, 
        p_name=p_name, 
        p_name_is_english=is_english_name(p_name),
        existing_found=existing_found,
        origin=request.args.get('origin', ''),
        data=existing_data,
        initial_tab=decide_parent_url_tab(p_name, p_type, request.args.get('child', ''))
        )

def decide_parent_url_tab(p_name, p_type, child_name=''):
    """親馬の登録画面で最初に表示するタブを決める。
    仔の産地が海外以外で、仔の抹消日または生年が1986年以降なら「jra」、それ以外は「other」。
    仔は child_name（無ければ父・母がこの馬になっている馬）をHorses・Sire・Damシートから探す"""
    def cell(r, i):
        return r[i].strip() if len(r) > i and r[i] else ''

    # 馬名がカタカナで10文字以上の馬は外国の馬（JRAの馬名は9文字まで）なので「その他」にする
    katakana = re.sub(r'[\s　]', '', p_name or '')
    if _KATAKANA_NAME_RE.match(p_name or '') and len(katakana) >= 10:
        return 'other'

    try:
        sh = gc.open(horse_data)
        # (シート名, 生年月日の列, 父の列, 母の列, 産地の列)
        layouts = [("Horses", H_BIRTH, H_SIRE, H_DAM, H_REGION),
                   ("Sire", P_BIRTH, P_SIRE, P_DAM, P_REGION), ("Dam", P_BIRTH, P_SIRE, P_DAM, P_REGION)]
        child = None
        for title, birth_col, sire_col, dam_col, region_col in layouts:
            try:
                rows = sh.worksheet(title).get_all_values()[1:]
            except gspread.WorksheetNotFound:
                continue
            parent_col = sire_col if p_type == 'Sire' else dam_col
            for r in rows:
                if (child_name and row_matches_name(r, child_name)) or (
                        not child_name and p_name and cell(r, parent_col) == p_name):
                    child = (display_name_of_row(r), date_year(cell(r, birth_col)), cell(r, region_col))
                    break
            if child:
                break
        if not child:
            return 'other'

        name, birth_year, region = child
        cancel_year = None
        try:
            for r in sh.worksheet("抹消").get_all_values()[1:]:
                if cell(r, 1) == name:
                    cancel_year = date_year(cell(r, 0))
                    break
        except gspread.WorksheetNotFound:
            pass

        if region != '海外' and any(y and y >= 1986 for y in (cancel_year, birth_year)):
            return 'jra'
    except Exception:
        pass
    return 'other'

@app.route('/update_horse', methods=['POST'])
@login_required
def update_horse():
    try:
        new_name = request.form.get('name')
        y, m, d = request.form.get('year'), request.form.get('month'), request.form.get('day')
        birth_date_str = format_date8(y, m, d)
        
        sh = gc.open(horse_data)
        ws = sh.worksheet("Horses")
        data = ws.get_all_values()
        
        # 競走馬登録日（edit_horse.html に reg_year/reg_month/reg_day 欄がある場合のみ更新する）
        has_reg_fields = any(k in request.form for k in ('reg_year', 'reg_month', 'reg_day'))
        reg_y = (request.form.get('reg_year') or '').strip()
        reg_m = (request.form.get('reg_month') or '').strip()
        reg_d = (request.form.get('reg_day') or '').strip()
        registration_str = format_date8(reg_y, reg_m, reg_d) if reg_y and reg_m else ''

        old_name = request.form.get('old_name')
        new_name_en = (request.form.get('name_en') or '').strip()
        # 馬名が英字の場合はB列（英馬名）に記録する（A列は空欄）
        name_ja = '' if is_english_name(new_name) else (new_name or '').strip()
        if not name_ja:
            new_name_en = (new_name or '').strip() or new_name_en
        jra_url, nar_url = split_horse_urls(request.form.get('jra_url'), request.form.get('nar_url'))
        old_row = next((r for r in data[1:] if row_matches_name(r, old_name)), None)
        if old_row is not None and jra_url != (old_row[H_JRA_URL] if len(old_row) > H_JRA_URL else ''):
            kaku_error = jra_url_kaku_error(jra_url)
            if kaku_error:
                return f"エラー：{kaku_error}", 400

        for i, row in enumerate(data):
            if i > 0 and row_matches_name(row, old_name):
                status = request.form.get('status')
                old_status = row[H_STATUS] if len(row) > H_STATUS else ''
                # 状態が「早期」の馬は、edit_horseでの手動編集でも「放牧」には変更できないようにする
                # （「入厩」への変更は許可する）
                if old_status == '早期' and status == '放牧':
                    status = '早期'
                old_registration = row[H_REGISTRATION] if len(row) > H_REGISTRATION else ''
                # 競走馬登録日が新たに入力・変更された場合は、早期登録の状態判定を適用する
                if has_reg_fields and registration_str and registration_str != old_registration:
                    status = resolve_registration_status(status, y, reg_y, reg_m)
                update_values = [[
                    name_ja, new_name_en, request.form.get('gender'), birth_date_str,
                    request.form.get('sire'), request.form.get('dam'),
                    request.form.get('owner'), request.form.get('location'),
                    request.form.get('stable_name'), status,
                    request.form.get('birthplace_region'), request.form.get('birthplace_detail'),
                    request.form.get('breeder'),
                    jra_url, nar_url
                ]]
                # 馬名〜NARのURL（A〜O列）を更新する
                ws.update(f'A{i+1}:{col_letter(H_NAR_URL)}{i+1}', update_values)
                # 馬名を変更した場合はChangesシートに記録する
                if old_name and new_name and new_name != old_name:
                    sh.worksheet("Changes").append_row([today8(), new_name, '馬名変更', old_name, new_name])
                # 競走馬登録（P列）：入力欄がある場合のみ更新（無い場合は既存の値を保持）
                if has_reg_fields:
                    reg_col = col_letter(H_REGISTRATION)
                    if len(data[0]) <= H_REGISTRATION:
                        ensure_columns(ws, len(HORSE_COLUMNS))
                        ws.update(range_name=f'{reg_col}1', values=[[HORSE_COLUMNS[H_REGISTRATION]]])
                    # update_acell は数値として解釈されるため、他の日付と同じく文字列のまま書き込む
                    ws.update(f'{reg_col}{i+1}', [[registration_str]])
                record_cancel_date_from_url(sh, new_name, jra_url)
                break
        return redirect(f"/horse/{new_name}")
    except Exception as e:
        return f"エラーが発生しました: {e}", 400

def ensure_columns(ws, count):
    """シートの列数が count 未満なら列を追加する（列数を超える範囲への書き込みはエラーになるため）"""
    try:
        if ws.col_count < count:
            ws.add_cols(count - ws.col_count)
    except Exception:
        pass

def get_or_create_worksheet(sh, title, headers):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=100, cols=len(headers))
        ws.append_row(headers)
        return ws

@app.route('/save_change', methods=['POST'])
@login_required
def save_change():
    horse_name = request.form.get('horse_name')
    change_type = request.form.get('change_type')
    
    t_year = request.form.get('t_year')
    t_month = request.form.get('t_month')
    t_day = request.form.get('t_day')
    date_str = format_date8(t_year, t_month, t_day) if t_year else ""
        
    old_val = request.form.get('old_val', '')
    new_val = request.form.get('new_val', '')
        
    try:
        sh = gc.open(horse_data)
        ws_horses = sh.worksheet("Horses")
        horses_data = ws_horses.get_all_values()
        
        target_idx = next((i + 1 for i, row in enumerate(horses_data) if i > 0 and row_matches_name(row, horse_name)), -1)
                
        if target_idx != -1:
            if change_type == "転厩":
                parts = new_val.split('・')
                new_area = parts[0] if len(parts) > 1 else ""
                new_stable = parts[1] if len(parts) > 1 else new_val
                ws_horses.update(f'{col_letter(H_AREA)}{target_idx}:{col_letter(H_STABLE)}{target_idx}',
                                 [[new_area, new_stable]])
            elif change_type == "馬主変更":
                ws_horses.update(f'{col_letter(H_OWNER)}{target_idx}', [[new_val]])
            
            ws_changes = sh.worksheet("Changes")
            ws_changes.append_row([date_str, horse_name, change_type, old_val, new_val])
            
            # ソート処理 (年月日昇順、空欄は最後、その後馬名昇順)
            data = ws_changes.get_all_values()
            if len(data) > 1:
                headers = data[0]
                rows = data[1:]
                
                def get_sort_key(r):
                    d = str(r[0]).strip() if len(r) > 0 else ""
                    name = str(r[1]).strip() if len(r) > 1 else ""
                    parts = split_date(d) if d else None
                    if not parts:
                        d_sort = "99999999"
                    else:
                        d_sort = f"{parts[0]:04d}{parts[1] or 0:02d}{parts[2] or 0:02d}"
                    return (d_sort, name)
                    
                rows.sort(key=get_sort_key)
                ws_changes.clear()
                ws_changes.update(range_name='A1', values=[headers] + rows)
            
            flash(f"『{horse_name}』の{change_type}処理が完了しました。")
        else:
            flash("対象の馬が見つかりませんでした。")
    except Exception as e:
        flash(f"エラーが発生しました: {e}")
        
    return redirect(url_for('horse_detail', name=horse_name, active_tab='profile'))

@app.route('/update_specific_parent', methods=['POST'])
@login_required
def update_specific_parent():
    data = request.json
    child_name = data.get('child_name')
    parent_col = data.get('parent_col') # '父' または '母'
    new_parent_name = data.get('new_parent_name')
    
    try:
        wb = gc.open('Horse_Data')
        target_sheets = ['Horses', 'Sire', 'Dam']
        updated = False
        
        for sheet_name in target_sheets:
            try:
                ws = wb.worksheet(sheet_name)
            except gspread.WorksheetNotFound:
                continue
                
            records = ws.get_all_records()
            if not records:
                continue
                
            headers = ws.row_values(1)
            
            for i, r in enumerate(records):
                if record_matches_name(r, child_name):
                    if parent_col in headers:
                        col_idx = headers.index(parent_col) + 1
                        row_idx = i + 2 
                        ws.update_cell(row_idx, col_idx, new_parent_name)
                        updated = True
                        break
            if updated:
                break
        
        if updated:
            return {"status": "success"}
        else:
            return {"status": "error", "message": "対象の仔馬データが見つかりませんでした。"}
            
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.route('/transfer_stable', methods=['POST'])
@login_required
def transfer_stable(name):
    horse_name = request.form.get('horse_name')
    new_area = request.form.get('area')
    new_stable_name = request.form.get('stable_name')
    
    t_year = request.form.get('t_year')
    t_month = request.form.get('t_month')
    t_day = request.form.get('t_day')
    
    # 年月日を結合（入力がない場合は空欄にする）
    date_str = ""
    if t_year or t_month or t_day:
        date_str = format_date8(t_year, t_month, t_day) if t_year else ""
        
    try:
        sh = gc.open(horse_data)
        ws_horses = sh.worksheet("Horses")
        horses_data = ws_horses.get_all_values()
        
        old_area = ""
        old_stable = ""
        target_idx = -1
        
        # 該当馬を検索し、行番号と旧厩舎情報を取得
        for i, row in enumerate(horses_data):
            if i > 0 and row_matches_name(row, horse_name):
                target_idx = i + 1
                old_area = row[H_AREA] if len(row) > H_AREA else ""
                old_stable = row[H_STABLE] if len(row) > H_STABLE else ""
                break
                
        if target_idx != -1:
            # Horsesシートの更新 (H列=拠点, I列=厩舎)
            ws_horses.update(f'{col_letter(H_AREA)}{target_idx}:{col_letter(H_STABLE)}{target_idx}', [[new_area, new_stable_name]])
            
            # Changesシートに記録
            ws_changes = sh.worksheet("Changes")
            old_full = f"{old_area}・{old_stable}" if old_area else old_stable
            new_full = f"{new_area}・{new_stable_name}"
            ws_changes.append_row([date_str, horse_name, "転厩", old_full, new_full])
            
            flash(f"『{horse_name}』の転厩処理が完了しました。")
        else:
            flash("対象の馬が見つかりませんでした。")
    except Exception as e:
        flash(f"エラーが発生しました: {e}")
        
    return redirect(url_for('horse_detail', name=name, active_tab='profile'))

@app.route('/horse/<name>')
def horse_detail_redirect(name):
    # デフォルトはプロフィールタブへリダイレクト
    return redirect(url_for('horse_detail', name=name, active_tab='profile'))

@app.route('/horse/<name>/<active_tab>')
@wait_for_sheets
def horse_detail(name, active_tab):
    if active_tab not in ['profile', 'races']:
        return redirect(url_for('horse_detail', name=name, active_tab='profile'))
    all_horses = get_all_horses()
    horse = next((h for h in all_horses if len(h)>0 and (h[H_NAME].strip() == name.strip() or row_matches_name(h, name))), None)

    if horse is None:
        # 読み込み待ちの画面で1秒ごとに読み込み直し、5回で見つからなければ
        # 再読み込みするかホームに戻るかを選ぶ画面にする
        raise SheetNotReady(f"Horsesシートに『{name}』が見つかりません")

    # 出走予定（Entryシートの今日以降のレース）。レース名・条件・コースは番組から
    horse_races = []
    try:
        today = datetime.now().date()
        records = gc.open(horse_data).worksheet("Entry").get_all_values()
        for row in records[1:]:
            if len(row) < 4 or row[3] != name:
                continue
            r_date = parse_full_date(row[0])
            if not r_date or r_date < today:
                continue
            r_num = int(row[2]) if str(row[2]).isdigit() else 0
            race = find_program_race(r_date, row[1], r_num) if r_num else None
            horse_races.append({
                'sort_key': (r_date, r_num),
                'date_label': format_date_jp(r_date, weekday=True),
                'date': r_date.isoformat(),
                'venue': row[1],
                'num': r_num,
                'name': (race['name'] or '-') if race else '-',
                'condition': race['condition'] if race else '-',
                'course': race['course'] if race else '-',
                'status': (row[4] if len(row) > 4 else '') or '-',
            })
        horse_races.sort(key=lambda r: r['sort_key'])
    except Exception as e:
        print(f"スプレッドシート(Entry)の読み込みエラー: {e}")

    changes_history = []
    try:
        sh = gc.open(horse_data)
        ws_changes = sh.worksheet("Changes")
        records = ws_changes.get_all_records()
        for r in records:
            # 該当馬の変更履歴をすべて取得（馬主変更・転厩含む）
            if str(r.get("馬名", "")) == name:
                changes_history.append(r)
    except Exception:
        pass

    sire_info, dam_info = {"line1": "不明", "line2": "不明"}, {"line3": "不明", "line4": "不明"}
    try:
        sh = gc.open(horse_data)
        if horse and len(horse) > H_SIRE and horse[H_SIRE]:
            try:
                s_data = sh.worksheet("Sire").get_all_values()[1:]
                s_row = next((r for r in s_data if row_matches_name(r, horse[H_SIRE])), None)
                if s_row: sire_info.update({"line1": s_row[P_SIRE] if len(s_row)>P_SIRE else "不明", 
                                            "line2": s_row[P_DAM] if len(s_row)>P_DAM else "不明"})
            except gspread.WorksheetNotFound: pass
        if horse and len(horse) > H_DAM and horse[H_DAM]:
            try:
                d_data = sh.worksheet("Dam").get_all_values()[1:]
                d_row = next((r for r in d_data if row_matches_name(r, horse[H_DAM])), None)
                if d_row: dam_info.update({"line3": d_row[P_SIRE] if len(d_row)>P_SIRE else "不明", 
                                           "line4": d_row[P_DAM] if len(d_row)>P_DAM else "不明"})
            except gspread.WorksheetNotFound: pass
    except: pass

    if horse and len(horse) > H_BIRTH and isinstance(horse[H_BIRTH], str):
        horse = list(horse)
        birth = parse_full_date(horse[H_BIRTH])
        if birth:
            horse[H_BIRTH] = datetime(birth.year, birth.month, birth.day)
    
    base_birth_year = None
    if isinstance(horse[H_BIRTH], datetime):
        base_birth_year = horse[H_BIRTH].year
    elif isinstance(horse[H_BIRTH], str):
        base_birth_year = extract_year(horse[H_BIRTH])
    elif isinstance(horse[H_BIRTH], dict) and 'year' in horse[H_BIRTH]:
        base_birth_year = int(horse[H_BIRTH]['year'])
    
    pedigree_data = get_5gen_pedigree(horse[H_SIRE], horse[H_DAM], base_birth_year, gc,
                                      base_is_english=is_english_name(horse[H_NAME]))

    # 兄弟馬・近親馬（インブリードの下に表示）：ページ初期表示時にサーバー側で計算しておくことで
    # クライアント側の追加fetchによる表示の遅延・ちらつきを無くす
    dam_node = pedigree_data.get(3)
    granddam_node = pedigree_data.get(7)
    ggranddam_node = pedigree_data.get(15)

    def _horse_blank(idx):
        return len(horse) > idx and (horse[idx] is None or str(horse[idx]).strip() == '')

    horse_status = horse[H_STATUS].strip() if len(horse) > H_STATUS and horse[H_STATUS] else ''
    horse_has_url = any(len(horse) > c and str(horse[c] or '').strip() for c in (H_JRA_URL, H_NAR_URL))
    horse_has_alert = _horse_blank(H_OWNER) or _horse_blank(H_REGION) or _horse_blank(H_BREEDER)

    family_table1, family_table2 = [], []
    if dam_node and dam_node.get('full_db_name'):
        family_table1, family_table2 = compute_family_tables(
            horse[H_NAME], horse[H_GENDER], base_birth_year or 0, horse[H_SIRE],
            horse_status, horse_has_url, horse_has_alert,
            dam_node.get('name', ''),
            dam_node.get('full_db_name', ''),
            dam_node.get('birth_year'),
            granddam_node.get('name', '') if granddam_node else '',
            granddam_node.get('full_db_name', '') if granddam_node else '',
            granddam_node.get('birth_year') if granddam_node else None,
            ggranddam_node.get('name', '') if ggranddam_node else '',
            ggranddam_node.get('full_db_name', '') if ggranddam_node else '',
            ggranddam_node.get('birth_year') if ggranddam_node else None
        )

    # 出走履歴（年ごとのレース結果のファイルから、馬IDまたは馬名で探す）と本賞金・収得賞金・クラス
    race_results, race_summary_data = [], RR.summarize_results([])
    if active_tab == 'races':
        try:
            horse_id = RR.horse_id_from_url(horse[H_JRA_URL] if len(horse) > H_JRA_URL else '')
            race_results, race_summary_data = get_horse_race_results(horse[H_NAME], horse_id, base_birth_year)
        except Exception as e:
            print(f"レース結果の読み込みエラー: {e}")

    return render_template('horse_detail.html', 
                           horse=horse, 
                           horse_races=horse_races,
                           race_results=race_results,
                           race_summary_data=race_summary_data,
                           family_table1=family_table1,
                           family_table2=family_table2,
                           sire_info=sire_info, 
                           dam_info=dam_info, 
                           current_year=datetime.now().year,
                           pedigree=pedigree_data,
                           changes_history=changes_history,
                           stables=get_stables_list(),
                           active_tab=active_tab
                           )

@app.route('/edit_horse/<name>')
@login_required
@wait_for_sheets
def edit_horse(name):
    horse = next((h for h in get_all_horses() if len(h)>0 and (h[H_NAME] == name or row_matches_name(h, name))), None)
    if horse is None:
        raise SheetNotReady(f"Horsesシートに『{name}』が見つかりません")
    return render_template('edit_horse.html', horse=horse, stables=get_stables_list(), current_year=datetime.now().year) if horse else ("Horse not found", 404)

def racing_week_start(d):
    """開催週の初日（火曜日）。土日と、祝日の月曜日の開催を同じ週にまとめる"""
    return d - timedelta(days=(d.weekday() - 1) % 7)

def format_month_day(d, weekday=True):
    """日付を年なしの「10月18日(日)」にする"""
    return f"{d.month}月{d.day}日" + (f"({JP_WEEKDAYS[d.weekday()]})" if weekday else '')

def short_race_label(text):
    """最新レースの表示：今年のレースは年を省く（例：2026/10/4 東京5R … → 10/4 東京5R …）"""
    text = str(text or '').strip()
    prefix = f"{datetime.now().year}/"
    return text[len(prefix):] if text.startswith(prefix) else text

app.jinja_env.filters['short_race'] = short_race_label
app.jinja_env.filters['mdate'] = lambda v, weekday=True: (
    format_month_day(v, weekday) if isinstance(v, date) else
    (format_month_day(parse_full_date(v), weekday) if parse_full_date(v) else str(v or '')))

@app.route('/races')
def race_list():
    """番組表：開催週（火〜月）ごとに、開催日・競馬場を選んでその日の番組を表示する"""
    today = datetime.now().date()
    base = parse_full_date(request.args.get('date') or '') or today
    week_start = racing_week_start(base)
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    program = get_program_for_dates(week_days)
    race_days = sorted(program)

    # 表示する開催日：指定された日（無ければ今日以降で最初の開催日、それも無ければ週の最後の開催日）
    if base in program:
        selected = base
    else:
        selected = next((d for d in race_days if d >= base), race_days[-1] if race_days else None)

    venues = list(program.get(selected, {}).keys()) if selected else []
    venue = request.args.get('venue', '')
    if venue not in venues:
        venue = venues[0] if venues else ''
    day_races = program.get(selected, {}).get(venue, {}) if selected else {}
    first = next(iter(day_races.values()), None)
    heading = f"{first['kai']}回{venue}{first['nichi']}日" if first else ''

    return render_template('race_list.html',
                           date=selected.isoformat() if selected else '',
                           selected=selected,
                           date_for_input=(selected or base).isoformat(),
                           venue=venue,
                           day_races=day_races,
                           available_venues=venues,
                           display_dates=[{'value': d.isoformat(), 'label': format_month_day(d)} for d in race_days],
                           week_label=f"{format_month_day(week_days[0], False)}〜{format_month_day(week_days[-1], False)}",
                           week_start=week_start.isoformat(),
                           week_end=week_days[-1].isoformat(),
                           prev_week=(week_start - timedelta(days=7)).isoformat(),
                           next_week=(week_start + timedelta(days=7)).isoformat(),
                           search_text=heading)

def horse_class_of(h, today=None):
    """Horsesシートに記録したクラス。記録が無ければ、3歳以下の馬は「新馬」とみなす"""
    text = current_horse_class(h, today)
    if text:
        return text
    birth_year = date_year(h[H_BIRTH]) if len(h) > H_BIRTH else None
    if birth_year and datetime.now().year - birth_year <= 3:
        return '新馬'
    return ''

def class_allowed(race_class, horse_class):
    """レースのクラスに出走できるか（新馬は未出走の馬、未勝利は未勝利・未出走の馬、オープンは全馬）"""
    if race_class == '新馬':
        return horse_class == '新馬'
    if race_class == '未勝利':
        return horse_class in ('新馬', '未勝利')
    if race_class == 'オープン':
        return True
    return race_class == horse_class

def age_allowed(category, age):
    """レースの年齢条件（2歳／3歳／3歳以上／4歳以上）に合う馬齢か（馬齢が分からなければ合うとみなす）"""
    m = re.match(r'(\d)歳(以上)?', category or '')
    if not m or not isinstance(age, int):
        return True
    return age >= int(m.group(1)) if m.group(2) else age == int(m.group(1))

def read_entries(race_date, venue, num):
    """Entryシートから、指定したレースにエントリーした馬の一覧を返す"""
    entries = []
    try:
        rows = gc.open(horse_data).worksheet("Entry").get_all_values()[1:]
    except Exception:
        return entries
    for row in rows:
        if len(row) < 4 or parse_full_date(row[0]) != race_date or row[1] != venue:
            continue
        if not str(row[2]).isdigit() or int(row[2]) != int(num):
            continue
        entries.append({'name': row[3], 'status': row[4] if len(row) > 4 else '',
                        'num': row[5] if len(row) > 5 else '', 'rank': row[6] if len(row) > 6 else ''})
    return entries

@app.route('/edit_race')
def race_detail():
    race_date = parse_full_date(request.args.get('date') or '') or datetime.now().date()
    venue = request.args.get('venue', '')
    r_num_target = request.args.get('num', default=1, type=int)

    target_race_data = find_program_race(race_date, venue, r_num_target)
    search_text = f"{target_race_data['kai']}回{venue}{target_race_data['nichi']}日" if target_race_data else "番組が見つかりません"

    available_horses_with_class = []
    if target_race_data:
        for h in get_all_horses():
            if len(h) <= H_STABLE or str(h[H_STATUS] if len(h) > H_STATUS else '').strip() == '抹消':
                continue
            b_year = date_year(h[H_BIRTH])
            calculated_age = race_date.year - b_year if b_year else "不明"
            current_class = horse_class_of(h, race_date)
            if not age_allowed(target_race_data['category'], calculated_age):
                continue
            if target_race_data['jump'] or class_allowed(target_race_data['class'], current_class):
                available_horses_with_class.append({
                    'name': h[H_NAME], 'gender': h[H_GENDER], 'age': calculated_age,
                    'class': current_class or '不明', 'stable': f"{h[H_AREA]}・{h[H_STABLE]}"
                })

    return render_template('race_detail.html',
                           date=race_date.isoformat(), venue=venue, race=target_race_data,
                           available_horses=available_horses_with_class,
                           all_horses=get_all_horses(),
                           entered_horses=read_entries(race_date, venue, r_num_target) if target_race_data else [],
                           search_text=search_text)

@app.route('/save_entry', methods=['POST'])
@login_required
def save_entry():
    data = request.json
    
    # データの受け取り
    race_date = data.get('date') # 例: "2026-05-28"
    venue = data.get('venue')
    race_num = int(data.get('race_num'))
    horse_name = data.get('horse_name')
    entry_type = data.get('entry_type')
    horse_num = data.get('horse_num') or ""
    horse_rank = data.get('horse_rank') or ""

    status_label = {"estimated": "想定", "special": "特別", "final": "確定"}.get(entry_type, "想定")
    
    try:
        sh = gc.open(horse_data)
        ws_entry = sh.worksheet("Entry")
        records = ws_entry.get_all_values()
        
        found_idx = -1
        # 重複チェック (race_date, venue, race_num, horse_name が一致する行を探す)
        if len(records) > 1:
            for i, row in enumerate(records[1:], start=2): # ヘッダーが1行目のため start=2
                if len(row) >= 4:
                    r_date = row[0]
                    r_venue = row[1]
                    try:
                        r_num = int(row[2])
                    except ValueError:
                        r_num = 0
                    h_name = row[3]
                    
                    if r_date == race_date and r_venue == venue and r_num == race_num and h_name == horse_name:
                        found_idx = i
                        break

        if found_idx != -1:
            # 既に存在する場合はステータス、馬番、着順のみ更新
            ws_entry.update(f'E{found_idx}:G{found_idx}', [[status_label, horse_num, horse_rank]])
        else:
            # 存在しない場合は新規行として追加
            ws_entry.append_row([race_date, venue, race_num, horse_name, status_label, horse_num, horse_rank])

        return {"status": "success"}, 200
    except Exception as e:
        print(f"Entry save error: {e}")
        return {"status": "error", "message": str(e)}, 500

def compute_family_tables(horse_name, horse_gender, horse_birth_year, horse_sire_disp,
                           horse_status, horse_has_url, horse_has_alert,
                           dam_name, dam_full_name, dam_birth_year,
                           granddam_name, granddam_full_name, granddam_birth_year,
                           ggranddam_name='', ggranddam_full_name='', ggranddam_birth_year=None):
    """
    「兄弟馬」テーブルと「近親馬」テーブル用のデータを組み立てる。
    近親馬テーブルには、祖母を起点とした伯父叔父・伯母叔母・いとこ・いとこ甥いとこ姪に続けて、
    曾祖母を起点とした大伯父大叔母・伯従父叔従母・はとこも表示する。
    Horsesシートに加えて、Sireシート・Damシートに載っている馬（種牡馬・繁殖牝馬として登録されている
    だけの馬）も血縁馬として拾い上げる。SireシートとDamシートから拾った馬にはリンクを付けない。

    戻り値は (兄弟馬のテーブル一覧, 近親馬のテーブル一覧) のタプル。各テーブルは
      {'label': '母：〇〇（2015）', 'rows': [馬の行, ...]}（画面では見出しの後に「からの3代子孫」を付ける）
    で、近親馬は祖母からの3代子孫と曾祖母からの3代子孫の2つのテーブルに分かれる。
    馬の行は {'kind': 'row', 'relation':.., 'name':.., ..., 'children': [その馬の仔の行, ...]} で、
    仔（2代目）・孫（3代目）は親の行の children に入れ子で入る。
    """
    if not dam_full_name:
        return [], []

    all_horses = get_all_horses()

    # 甥・姪／いとこ判定用：ある馬名から「その馬自身の母欄の値」の候補群を引けるようにしておく
    name_candidates = {}
    for h in all_horses:
        if len(h) > H_DAM and h[H_NAME]:
            name_candidates.setdefault(h[H_NAME], []).append({'母': h[H_DAM], '生年月日': h[H_BIRTH]})

    sire_records, dam_records = [], []
    try:
        sh = gc.open(horse_data)
        dam_records = normalize_name_records(sh.worksheet('Dam').get_all_records())
        sire_records = normalize_name_records(sh.worksheet('Sire').get_all_records())
    except Exception as e:
        sheet_not_ready(f"Sire・Damシートを読み込めませんでした: {e}")
    if not dam_records or not sire_records:
        sheet_not_ready("Sire・Damシートのデータが0件です")

    # --- 和名と英馬名の対応：同じ馬の和名・英馬名を同じ馬として照合できるようにする ---
    # （母欄が和名の馬と英馬名の馬のどちらも、その母の仔として見つかるようにする）
    alias_of = {}
    def _add_alias(name_ja, name_en):
        name_ja, name_en = str(name_ja or '').strip(), str(name_en or '').strip()
        key = name_ja or name_en
        for n in (name_ja, name_en):
            if n and n not in alias_of:
                alias_of[n] = key

    for h in all_horses:
        _add_alias(h[H_NAME] if not is_english_name(h[H_NAME]) else '', h[H_NAME_EN] if len(h) > H_NAME_EN else '')
    for r in sire_records + dam_records:
        _add_alias(r.get('馬名', '') if not is_english_name(r.get('馬名', '')) else '', r.get('英馬名', ''))

    def canon(name):
        """和名・英馬名のどちらで書かれていても同じ馬なら同じ値を返す（照合用）"""
        name = str(name or '').strip()
        return alias_of.get(name, name)

    def canon_field(value):
        """母欄の値（「馬名 (母馬 - 生年)」の注記付きを含む）を照合用にそろえる"""
        disp, mother, year = split_dam_annotation(value)
        return f"{canon(disp)} ({canon(mother)} - {year})" if mother else canon(disp)

    name_candidates = {canon(k): v for k, v in name_candidates.items()}
    for r in dam_records:
        name_candidates.setdefault(canon(r.get('馬名', '')), []).append(r)

    def resolve_granddam_of(dam_field_value, child_birth_year):
        disp_name, embedded_mother, _embedded_year = split_dam_annotation(dam_field_value)
        if embedded_mother:
            return embedded_mother
        if not disp_name:
            return None
        candidates = name_candidates.get(canon(disp_name), [])
        if not candidates:
            return None
        if len(candidates) == 1:
            g_disp, _, _ = split_dam_annotation(str(candidates[0].get('母', '')).strip())
            return g_disp or None
        if child_birth_year:
            valid = []
            for r in candidates:
                b_year = extract_year(str(r.get('生年月日', '')).strip())
                if b_year and b_year < child_birth_year:
                    valid.append((r, b_year))
            if valid:
                valid.sort(key=lambda x: child_birth_year - x[1])
                g_disp, _, _ = split_dam_annotation(str(valid[0][0].get('母', '')).strip())
                return g_disp or None
        return None

    # --- Horsesシート・Sireシート・Damシートの馬をひとつの候補リストにまとめる ---
    # （同名馬がHorsesシートに既に登録済みの場合はそちらを優先し、Sire/Damシート側の重複は無視する）
    existing_names = set()
    candidates = []

    for h in all_horses:
        if len(h) <= H_DAM or not h[H_NAME]:
            continue
        nm = h[H_NAME].strip()
        if not nm or canon(nm) == canon(horse_name):
            continue
        existing_names.add(canon(nm))

        def _blank(idx, row=h):
            return len(row) > idx and (row[idx] is None or str(row[idx]).strip() == '')

        candidates.append({
            'name': nm,
            'gender': h[H_GENDER],
            'birth_str': h[H_BIRTH],
            'sire_disp': h[H_SIRE],
            'dam_field': h[H_DAM].strip() if h[H_DAM] else '',
            'status': h[H_STATUS].strip() if len(h) > H_STATUS and h[H_STATUS] else '',
            'has_url': len(h) > H_JRA_URL and h[H_JRA_URL] and str(h[H_JRA_URL]).strip() != '',
            'has_alert': _blank(H_OWNER) or _blank(H_REGION) or _blank(H_BREEDER),
            'linkable': True,
        })

    for r in sire_records:
        nm = str(r.get('馬名', '')).strip()
        if not nm or canon(nm) == canon(horse_name) or canon(nm) in existing_names:
            continue
        existing_names.add(canon(nm))
        candidates.append({
            'name': nm,
            'gender': '牡',
            'birth_str': str(r.get('生年月日', '')).strip(),
            'sire_disp': str(r.get('父', '')).strip(),
            'dam_field': str(r.get('母', '')).strip(),
            'status': '種馬',
            'has_url': False,
            'has_alert': False,
            'linkable': False,
        })

    for r in dam_records:
        nm = str(r.get('馬名', '')).strip()
        if not nm or canon(nm) == canon(horse_name) or canon(nm) in existing_names:
            continue
        existing_names.add(canon(nm))
        candidates.append({
            'name': nm,
            'gender': '牝',
            'birth_str': str(r.get('生年月日', '')).strip(),
            'sire_disp': str(r.get('父', '')).strip(),
            'dam_field': str(r.get('母', '')).strip(),
            'status': '繁殖',
            'has_url': False,
            'has_alert': False,
            'linkable': False,
        })

    by_name = {}
    for c in candidates:
        by_name.setdefault(canon(c['name']), []).append(c)

    def _find_own(name, birth_year):
        """母・祖母自身の表示用データ（性別・父・状態など）を候補リストから探す"""
        opts = by_name.get(canon(name), [])
        if not opts:
            return None
        if birth_year:
            for c in opts:
                if extract_year(c['birth_str']) == birth_year:
                    return c
        return opts[0]

    def _make_row(relation, name, gender, birth_year, sire_disp, status, has_url, has_alert, linkable,
                  is_self=False, parent_name=None, depth=0):
        return {
            'kind': 'row',
            'relation': relation,
            'name': name,
            'gender': gender,
            'birth_year': birth_year if birth_year else '不明',
            'sire': sire_disp,
            'status': status,
            'has_url': has_url,
            'has_alert': has_alert,
            'linkable': linkable,
            'is_self': is_self,
            'is_muted': (not is_self) and ((status == '抹消') or (not linkable)),
            'parent_name': parent_name,
            'depth': depth,
        }

    siblings, nephews, uncles, cousins = [], [], [], []
    great_uncles, itoko_oji = [], []
    dam_full_key, granddam_full_key, ggranddam_full_key = (
        canon_field(dam_full_name), canon_field(granddam_full_name), canon_field(ggranddam_full_name))
    dam_key, granddam_key, ggranddam_key = canon(dam_name), canon(granddam_name), canon(ggranddam_name)

    for c in candidates:
        target_dam = c['dam_field']
        if not target_dam:
            continue
        target_dam_key = canon_field(target_dam)
        target_birth_year = extract_year(c['birth_str']) or 0
        gender = c['gender']

        # 1. 兄弟馬の判定（母が完全一致するか）
        if target_dam_key == dam_full_key:
            if not horse_birth_year or not target_birth_year:
                relation = "兄弟" if gender in ['牡', 'せん'] else "姉妹"
            elif target_birth_year < horse_birth_year:
                relation = "兄" if gender in ['牡', 'せん'] else "姉"
            elif target_birth_year > horse_birth_year:
                relation = "弟" if gender in ['牡', 'せん'] else "妹"
            else:
                relation = "同期(兄弟)"
            siblings.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                       c['status'], c['has_url'], c['has_alert'], c['linkable']))

        # 2. 伯父叔父・伯母叔母の判定（対象馬の母＝本馬の祖母、が一致するか）
        # ※ 本馬の実母自身もここに該当してしまう（祖母の子であるため）ので、実母は除外する
        # ※ 実母より年上なら伯父・伯母、年下なら叔父・叔母と表記を分ける
        elif granddam_full_name and target_dam_key == granddam_full_key:
            if canon(c['name']) == dam_key:
                continue
            if dam_birth_year and target_birth_year and target_birth_year < dam_birth_year:
                relation = "伯父" if gender in ['牡', 'せん'] else "伯母"
            else:
                relation = "叔父" if gender in ['牡', 'せん'] else "叔母"
            uncles.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                     c['status'], c['has_url'], c['has_alert'], c['linkable']))

        # 2b. 大伯父・大叔母の判定（対象馬の母＝本馬の曾祖母、が一致するか）
        # ※ 祖母自身もここに該当してしまうので除外する
        # ※ 祖母より年上なら大伯父・大伯母、年下なら大叔父・大叔母と表記を分ける
        elif ggranddam_full_name and target_dam_key == ggranddam_full_key:
            if canon(c['name']) == granddam_key:
                continue
            if granddam_birth_year and target_birth_year and target_birth_year < granddam_birth_year:
                relation = "大伯父" if gender in ['牡', 'せん'] else "大伯母"
            else:
                relation = "大叔父" if gender in ['牡', 'せん'] else "大叔母"
            great_uncles.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                           c['status'], c['has_url'], c['has_alert'], c['linkable']))

        # 3. 甥・姪／いとこ／伯従父・叔従母の判定
        elif dam_name or granddam_name or ggranddam_name:
            parent_disp_name, _, _ = split_dam_annotation(target_dam)
            target_granddam = canon(resolve_granddam_of(target_dam, target_birth_year))
            if target_granddam and dam_name and target_granddam == dam_key:
                relation = "甥" if gender in ['牡', 'せん'] else "姪"
                nephews.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                          c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                          parent_name=parent_disp_name, depth=1))
            elif target_granddam and granddam_name and target_granddam == granddam_key:
                cousins.append(_make_row("いとこ", c['name'], gender, target_birth_year, c['sire_disp'],
                                          c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                          parent_name=parent_disp_name, depth=1))
            # ※ 祖母自身の子（伯父叔父・母）は、その祖母経由でも曾祖母に行き着いてしまうため、
            #   直接の親が祖母自身のものはここでは除外する（伯父叔父・母は既にテーブル2で表示済み）
            elif (target_granddam and ggranddam_name and target_granddam == ggranddam_key
                  and canon(parent_disp_name) != granddam_key):
                if dam_birth_year and target_birth_year and target_birth_year < dam_birth_year:
                    relation = "伯従父" if gender in ['牡', 'せん'] else "伯従母"
                else:
                    relation = "叔従父" if gender in ['牡', 'せん'] else "叔従母"
                itoko_oji.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                            c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                            parent_name=parent_disp_name, depth=1))

    # 見出しの馬（母／祖母／曾祖母）の生年より前に生まれた馬は、同名の別馬の仔である可能性が
    # 高いため表示しない（生年が不明な場合は判定できないので残す）
    def _not_before(rows, header_year):
        if not header_year:
            return rows
        return [r for r in rows
                if not (isinstance(r['birth_year'], int) and r['birth_year'] < header_year)]

    siblings = _not_before(siblings, dam_birth_year)
    nephews = _not_before(nephews, dam_birth_year)
    uncles = _not_before(uncles, granddam_birth_year)
    cousins = _not_before(cousins, granddam_birth_year)
    great_uncles = _not_before(great_uncles, ggranddam_birth_year)
    itoko_oji = _not_before(itoko_oji, ggranddam_birth_year)

    # 見出しの馬（母・祖母・曾祖母）より前に生まれた馬は、その仔・親族としてあり得ないため表示しない
    def _born_after(rows, anchor_year):
        if not anchor_year:
            return rows
        return [r for r in rows
                if not isinstance(r['birth_year'], int) or r['birth_year'] >= anchor_year]

    siblings = _born_after(siblings, dam_birth_year)
    nephews = _born_after(nephews, dam_birth_year)
    uncles = _born_after(uncles, granddam_birth_year)
    cousins = _born_after(cousins, granddam_birth_year)
    great_uncles = _born_after(great_uncles, ggranddam_birth_year)
    itoko_oji = _born_after(itoko_oji, ggranddam_birth_year)

    # 4. 又甥・又姪（甥姪の仔）／いとこ甥・いとこ姪（いとこの仔）／はとこ（伯従父叔従母の仔）の判定
    #    ※ 甥姪・いとこ・伯従父叔従母の一覧が確定した後、それぞれを親として持つ馬を探す
    nephew_names = {canon(n['name']) for n in nephews}
    cousin_names = {canon(c['name']) for c in cousins}
    itoko_oji_names = {canon(o['name']) for o in itoko_oji}
    grand_nephews, cousin_nephews, hatoko = [], [], []

    for c in candidates:
        target_dam = c['dam_field']
        if not target_dam:
            continue
        parent_disp_name, _, _ = split_dam_annotation(target_dam)
        if not parent_disp_name:
            continue
        gender = c['gender']
        target_birth_year = extract_year(c['birth_str']) or 0

        parent_key = canon(parent_disp_name)
        if parent_key in nephew_names:
            relation = "又甥" if gender in ['牡', 'せん'] else "又姪"
            grand_nephews.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                            c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                            parent_name=parent_disp_name, depth=2))
        elif parent_key in cousin_names:
            relation = "いとこ甥" if gender in ['牡', 'せん'] else "いとこ姪"
            cousin_nephews.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                             c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                             parent_name=parent_disp_name, depth=2))
        elif parent_key in itoko_oji_names:
            hatoko.append(_make_row("はとこ", c['name'], gender, target_birth_year, c['sire_disp'],
                                     c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                     parent_name=parent_disp_name, depth=2))

    grand_nephews = _not_before(grand_nephews, dam_birth_year)
    cousin_nephews = _not_before(cousin_nephews, granddam_birth_year)
    hatoko = _not_before(hatoko, ggranddam_birth_year)

    grand_nephews = _born_after(grand_nephews, dam_birth_year)
    cousin_nephews = _born_after(cousin_nephews, granddam_birth_year)
    hatoko = _born_after(hatoko, ggranddam_birth_year)

    def _sort_key(row):
        # 区切り線行など 'birth_year' を持たない行が混ざっても KeyError にならないようにする
        by = row.get('birth_year')
        return by if isinstance(by, int) else 9999

    def _insert_with_children(base_rows, child_rows_by_parent):
        """
        base_rows（本人の世代の行、あらかじめ生年順に並べたもの）の各行に、その馬を親とする子
        （あらかじめ生年順、または既に孫まで入れ子にした行の一覧）を children として持たせる。
        親が表示されていない子は表示しない。
        """
        result = []
        for row in base_rows:
            row = dict(row)
            row['children'] = child_rows_by_parent.get(canon(row['name']), [])
            result.append(row)
        return result

    def _group_by_parent(rows):
        """親名ごとにグループ化し、各グループ内は生年順に並べる"""
        grouped = {}
        for r in rows:
            grouped.setdefault(canon(r['parent_name']), []).append(r)
        for lst in grouped.values():
            lst.sort(key=_sort_key)
        return grouped

    def _nest_two_generations(children_by_parent, grandchildren_by_parent):
        """
        children_by_parent（例：兄弟名→甥姪一覧）の各子の直下に、
        grandchildren_by_parent（例：甥姪名→又甥又姪一覧）から該当する孫世代をネストして展開する。
        戻り値は、そのまま_insert_with_childrenのchild_rows_by_parentとして使える形。
        """
        expanded = {}
        for parent_name, children in children_by_parent.items():
            scoped_grandchildren = {
                canon(child['name']): grandchildren_by_parent[canon(child['name'])]
                for child in children if canon(child['name']) in grandchildren_by_parent
            }
            expanded[parent_name] = _insert_with_children(children, scoped_grandchildren)
        return expanded

    # --- テーブル1：母（見出し） → 兄弟馬・本馬（仔がいれば直下に甥姪、孫がいればさらに直下に又甥又姪） ---
    own_row = _make_row('本馬', horse_name, horse_gender, horse_birth_year, horse_sire_disp,
                         horse_status, horse_has_url, horse_has_alert, False, is_self=True)
    siblings_and_self = siblings + [own_row]
    siblings_and_self.sort(key=_sort_key)

    nephews_by_sibling = _nest_two_generations(_group_by_parent(nephews), _group_by_parent(grand_nephews))

    def _table(relation, name, birth_year, rows):
        # 「からの3代子孫」はスマホで改行できるようテンプレート側で付ける
        return {'label': f"{relation}：{name}（{birth_year if birth_year else '不明'}）", 'rows': rows}

    table1 = [_table('母', dam_name, dam_birth_year, _insert_with_children(siblings_and_self, nephews_by_sibling))]

    # --- テーブル2：祖母（見出し） → 伯父叔父叔母・母（仔がいれば直下にいとこ、孫がいればさらに直下にいとこ甥いとこ姪） ---
    table2 = []
    if granddam_name:
        mother_own = _find_own(dam_name, dam_birth_year)
        if mother_own:
            mother_row = _make_row('母', dam_name, mother_own['gender'], dam_birth_year, mother_own['sire_disp'],
                                    mother_own['status'], mother_own['has_url'], mother_own['has_alert'],
                                    mother_own['linkable'], is_self=True)
        else:
            mother_row = _make_row('母', dam_name, '牝', dam_birth_year, '不明', '', False, False, True, is_self=True)

        tier2 = uncles + [mother_row]
        tier2.sort(key=_sort_key)

        # 母自身の子（＝本馬の兄弟）はテーブル1で既に表示済みのため、いとことしては差し込まない
        cousins_by_parent = _group_by_parent(cousins)
        cousins_by_parent.pop(dam_key, None)
        cousins_by_uncle = _nest_two_generations(cousins_by_parent, _group_by_parent(cousin_nephews))

        table2.append(_table('祖母', granddam_name, granddam_birth_year,
                             _insert_with_children(tier2, cousins_by_uncle)))

        # --- 曾祖母からの3代子孫：大伯父大叔母・祖母（仔がいれば直下に伯従父叔従母、
        #     孫がいればさらに直下にはとこ）を、近親馬の2つ目のテーブルにする ---
        if ggranddam_name:
            granddam_own = _find_own(granddam_name, granddam_birth_year)
            if granddam_own:
                granddam_row = _make_row('祖母', granddam_name, granddam_own['gender'], granddam_birth_year,
                                          granddam_own['sire_disp'], granddam_own['status'],
                                          granddam_own['has_url'], granddam_own['has_alert'],
                                          granddam_own['linkable'], is_self=True)
            else:
                granddam_row = _make_row('祖母', granddam_name, '牝', granddam_birth_year, '不明', '',
                                          False, False, True, is_self=True)

            tier3 = great_uncles + [granddam_row]
            tier3.sort(key=_sort_key)

            # 祖母自身の子（＝伯父叔父・母）はテーブル2で既に表示済みのため、伯従父叔従母としては差し込まない
            itoko_oji_by_parent = _group_by_parent(itoko_oji)
            itoko_oji_by_parent.pop(granddam_key, None)
            itoko_oji_by_great_uncle = _nest_two_generations(itoko_oji_by_parent, _group_by_parent(hatoko))

            table2.append(_table('曾祖母', ggranddam_name, ggranddam_birth_year,
                                 _insert_with_children(tier3, itoko_oji_by_great_uncle)))

    return table1, table2

def compute_relatives(horse_name, horse_birth_year, dam_name, dam_full_name, granddam_name, granddam_full_name):
    """
    兄弟馬・近親馬（叔父叔母・甥姪・従兄弟姉妹）のリストを計算する。
    horse_detail の初期表示（SSR）と /api/relatives（互換用）の両方から呼び出される共通ロジック。
    """
    if not dam_full_name:
        return []

    all_horses = get_all_horses()

    # 甥・姪判定用：ある馬名から「その馬自身の母欄の値」の候補群を引けるようにしておく
    # （Horsesシート＝実際に登録されている競走馬、Damシート＝血統表専用の繁殖牝馬データ）
    name_candidates = {}
    for h in all_horses:
        if len(h) > H_DAM and h[H_NAME]:
            name_candidates.setdefault(h[H_NAME], []).append({'母': h[H_DAM], '生年月日': h[H_BIRTH]})
    try:
        sh = gc.open(horse_data)
        for r in normalize_name_records(sh.worksheet('Dam').get_all_records()):
            # 和名・英馬名のどちらの表記でも引けるようにする
            for n in {str(r.get('馬名', '')).strip(), str(r.get('英馬名', '')).strip()}:
                if n:
                    name_candidates.setdefault(n, []).append(r)
    except Exception:
        pass

    def resolve_granddam_of(dam_field_value, child_birth_year):
        """
        ある馬の「母」欄の値から、その母馬自身の母（＝対象馬から見た祖母）の表示名を推定する。
        「馬名 (母馬 - 生年)」の注記が既にあればそこから直接取得し、無ければ登録データを検索する。
        """
        disp_name, embedded_mother, _embedded_year = split_dam_annotation(dam_field_value)
        if embedded_mother:
            return embedded_mother
        if not disp_name:
            return None

        candidates = name_candidates.get(disp_name, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            g_disp, _, _ = split_dam_annotation(str(candidates[0].get('母', '')).strip())
            return g_disp or None

        # 同名の母馬候補が複数ある場合は、対象馬より前に生まれた馬を優先して絞り込む
        if child_birth_year:
            valid = []
            for r in candidates:
                b_year = extract_year(str(r.get('生年月日', '')).strip())
                if b_year and b_year < child_birth_year:
                    valid.append((r, b_year))
            if valid:
                valid.sort(key=lambda x: child_birth_year - x[1])
                g_disp, _, _ = split_dam_annotation(str(valid[0][0].get('母', '')).strip())
                return g_disp or None
        return None

    relatives = []

    for h in all_horses:
        if len(h) <= H_DAM:
            continue

        target_name = h[H_NAME].strip() if h[H_NAME] else ''
        if not target_name or target_name == horse_name:
            continue

        target_gender = h[H_GENDER]
        target_birth_str = h[H_BIRTH]
        target_sire = h[H_SIRE]
        target_dam = h[H_DAM].strip()

        if not target_dam:
            continue

        target_birth_year = extract_year(target_birth_str) or 0

        relation = None

        # 1. 兄弟馬の判定 (母が完全一致するか)
        # 同名馬混同を避けるため、フルDBネーム「馬名 (母馬 - 生年)」で照合する
        if target_dam == dam_full_name:
            if not horse_birth_year or not target_birth_year:
                relation = "兄弟" if target_gender in ['牡', 'せん'] else "姉妹"  # 生年不明で前後関係が判定できないケース
            elif target_birth_year < horse_birth_year:
                relation = "兄" if target_gender in ['牡', 'せん'] else "姉"
            elif target_birth_year > horse_birth_year:
                relation = "弟" if target_gender in ['牡', 'せん'] else "妹"
            else:
                relation = "同期(兄弟)"  # 双子などの例外ケース

        # 2. 叔父・叔母の判定 (対象馬の母 ＝ 本馬の祖母、が一致するか)
        elif granddam_full_name and target_dam == granddam_full_name:
            relation = "叔父" if target_gender in ['牡', 'せん'] else "叔母"

        # 3. 甥・姪／従兄弟・従姉妹の判定
        # 対象馬自身の「母の母」を注記または登録データから逆算して照合する
        elif dam_name or granddam_name:
            target_granddam = resolve_granddam_of(target_dam, target_birth_year)
            if target_granddam and dam_name and target_granddam == dam_name:
                # 対象馬の祖母 ＝ 本馬の母 → 対象馬は本馬の兄弟の子
                relation = "甥" if target_gender in ['牡', 'せん'] else "姪"
            elif target_granddam and granddam_name and target_granddam == granddam_name:
                # 対象馬の祖母 ＝ 本馬の祖母（母同士は別）→ いとこ
                relation = "いとこ"

        # 必要に応じて従兄弟などの判定もここに追加可能です

        if relation:
            def _blank(idx):
                return len(h) > idx and (h[idx] is None or str(h[idx]).strip() == '')
            has_alert = _blank(5) or _blank(9) or _blank(11)
            target_status = h[H_STATUS].strip() if len(h) > H_STATUS and h[H_STATUS] else ''
            has_url = len(h) > H_JRA_URL and h[H_JRA_URL] and str(h[H_JRA_URL]).strip() != ''

            relatives.append({
                "relation": relation,
                "name": target_name,
                "gender": target_gender,
                "birth_year": target_birth_year if target_birth_year else '不明',
                "sire": target_sire,
                "has_alert": has_alert,
                "status": target_status,
                "has_url": has_url
            })

    # 生年の古い順に並び替え
    relatives.sort(key=lambda x: (x['birth_year'] if isinstance(x['birth_year'], int) else 9999))

    return relatives

@app.route('/api/relatives')
def api_relatives():
    horse_name = request.args.get('horse_name', '').strip()
    try:
        horse_birth_year = int(request.args.get('horse_birth_year', 0))
    except ValueError:
        horse_birth_year = 0

    dam_name = request.args.get('dam_name', '').strip()
    dam_full_name = request.args.get('dam_full_name', '').strip()
    granddam_name = request.args.get('granddam_name', '').strip()
    granddam_full_name = request.args.get('granddam_full_name', '').strip()

    relatives = compute_relatives(horse_name, horse_birth_year, dam_name, dam_full_name, granddam_name, granddam_full_name)
    return jsonify({"relatives": relatives})

@app.route('/api/available_races')
def api_available_races():
    horse_name = request.args.get('horse_name')
    race_date = parse_full_date(request.args.get('date') or '')
    if not horse_name or not race_date:
        return {"error": "パラメータが不足しています。"}, 400

    horse = next((h for h in get_all_horses() if len(h) > 0 and (h[0] == horse_name or row_matches_name(h, horse_name))), None)
    if not horse:
        return {"error": "馬が見つかりませんでした。"}, 404
    current_class = horse_class_of(horse, race_date)
    birth_year = date_year(horse[H_BIRTH]) if len(horse) > H_BIRTH else None
    age = race_date.year - birth_year if birth_year else None

    day = get_program_for_dates([race_date]).get(race_date, {})
    if not day:
        return {"races": [], "message": "指定された日の番組はありません（番組が取り込まれていない場合は「データ更新」の「開催日程（番組）インポート」から取り込んでください）。"}, 200

    available_races = []
    for venue, races in day.items():
        for r_num, race in sorted(races.items()):
            if race['jump'] or not class_allowed(race['class'], current_class) or not age_allowed(race['category'], age):
                continue
            available_races.append({
                'venue': venue, 'race_num': r_num, 'name': race['name'] or '-',
                'condition': race['condition'], 'course': race['course'], 'time': race['time']
            })
    return {"races": available_races, "current_class": current_class or '不明'}, 200

if __name__ == '__main__':
    app.run(debug=True, port=5001 ,use_reloader=False)