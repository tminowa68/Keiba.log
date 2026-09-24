import sqlite3
import re, os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

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
# 初期設定 (スプレッドシート)
# ==============================================================================
try:
    sh = gc.open(horse_data)
except gspread.SpreadsheetNotFound:
    sh = gc.create(horse_data)
    ws1 = sh.sheet1
    ws1.update_title("Horses")
    ws1.append_row(["馬名", "性別", "生年月日", "父", "母", "馬主名", "拠点", "厩舎", "状態", "産地", "地域", "生産牧場", "URL"])
    ws_miho = sh.add_worksheet(title="美浦", rows=100, cols=20)
    ws_miho.append_row(["厩舎名", "よみがな", "生年月日", "免許取得年", "開業", "引退", "馬房数", "臨時貸付"])
    ws_ritto = sh.add_worksheet(title="栗東", rows=100, cols=20)
    ws_ritto.append_row(["厩舎名", "よみがな", "生年月日", "免許取得年", "開業", "引退", "馬房数", "臨時貸付"])

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

def format_last_updated(dt_str):
    """Meta保存形式（YYYY/MM/DD HH:MM）を「年月日(曜日)」の表示形式に変換する"""
    if not dt_str:
        return None
    dt = None
    for fmt in ('%Y/%m/%d %H:%M', '%Y/%m/%d'):
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
        raise ValueError("JRA公式サイト（jra.go.jp / jra.jp）のURLを入力してください。")

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
            cancel_date = f"{int(m.group(1))}/{int(m.group(2))}/{int(m.group(3))}"
        return '抹消', cancel_date

    return ('放牧' if rest_span else '入厩'), None

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
    try:
        sh = gc.open(horse_data)
        data = sh.worksheet("Horses").get_all_values()
        return data[1:] if len(data) > 1 else []
    except Exception:
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
                        # 列インデックスの更新 (E:開業[4], F:引退[5], G:馬房数[6], H:臨時貸付[7])
                        opening = row[4] if len(row) > 4 else ""
                        retirement = row[5] if len(row) > 5 else ""
                        capacity_str = row[6] if len(row) > 6 else "0"
                        temp_loan_str = row[7] if len(row) > 7 else "0"
                        
                        # --- 〇年目の計算（開業の年月日の西暦から数える） ---
                        years_active = ""
                        if opening:
                            try:
                                # YYYY/MM/DD や YYYY-MM-DD から西暦部分を抽出
                                open_y = int(opening.split('/')[0]) if '/' in opening else int(opening[:4])
                                years_active = f"{current_year - open_y + 1}年目"
                            except ValueError:
                                pass
                                
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
                            'kana': row[1],
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
        return all_stables
    except Exception:
        return []

# --- 賞金・クラス判定 ---
def calc_added_prize(rank, condition, race_name, horse_birthday_str, race_date_str):
    condition, race_name = str(condition or ""), str(race_name or "")
    try: rank = int(rank)
    except (ValueError, TypeError): return 0

    race_year = int(race_date_str[:4])
    try:
        birth_year = int(horse_birthday_str.split('/')[0]) if '/' in horse_birthday_str else int(horse_birthday_str[:4])
    except ValueError:
        birth_year = race_year - 3 # フォールバック

    age = race_year - birth_year

    if "G" in race_name or "重賞" in condition or "J・G" in race_name:
        if rank == 1:
            if age <= 2: return 600
            if age == 3: return 1200 if race_date_str <= f"{race_year}-06-30" else 1200
            return 1200
        if rank == 2:
            if age <= 2: return 200
            if age == 3 and race_date_str <= f"{race_year}-06-30": return 400
            return 0
    
    if rank == 1:
        if "新馬" in condition or "未勝利" in condition: return 400
        if "1勝" in condition or "500万" in condition: return 500
        if "2勝" in condition or "1000万" in condition: return 600
        if "3勝" in condition or "1600万" in condition: return 900
        if "オープン" in condition:
            is_listed = "L" in race_name or "リステッド" in race_name
            return 800 if is_listed else 700
            
    return 0

def judge_class_by_prize(total_prize, has_raced, age, race_month):
    if total_prize == 0: 
        return "未勝利" if has_raced else "新馬"
    
    if age == 2 or (age == 3 and race_month <= 5):
        if total_prize <= 500: return "1勝クラス"
        return "オープン"
    else:
        if total_prize <= 500: return "1勝クラス"
        if total_prize <= 1000: return "2勝クラス"
        if total_prize <= 1600: return "3勝クラス"
        return "オープン"

def judge_required_class(condition_str):
    s = str(condition_str or "")
    if "1勝" in s or "500万" in s: return "1勝クラス"
    if "2勝" in s or "1000万" in s: return "2勝クラス"
    if "3勝" in s or "1600万" in s: return "3勝クラス"
    if "オープン" in s or "重賞" in s: return "オープン"
    if "未勝利" in s: return "未勝利"
    if "新馬" in s: return "新馬"
    return None

def get_class_from_results(horse_name, target_date_str, all_results_dict, horse_birthday_str):
    total_prize = 0
    target_dt = datetime.strptime(target_date_str, '%Y-%m-%d')
    results = all_results_dict.get(horse_name, [])
    
    past_races = [r for r in results if datetime.strptime(r['date'], '%Y-%m-%d') < target_dt]
    
    for r in past_races:
        total_prize += calc_added_prize(r['rank'], r['condition'], r['race_name'], horse_birthday_str, r['date'])
            
    race_year = target_dt.year
    race_month = target_dt.month
    try:
        birth_year = int(horse_birthday_str.split('/')[0]) if '/' in horse_birthday_str else int(horse_birthday_str[:4])
    except ValueError:
        birth_year = race_year - 3

    age = race_year - birth_year
    return judge_class_by_prize(total_prize, len(past_races) > 0, age, race_month)

def load_all_horse_results():
    all_results = {}
    files = gc.list_spreadsheet_files()
    entry_files = [f for f in files if re.search(r'^\d{4}_entry_.+$', f['name'])]
    
    for f_info in entry_files:
        try:
            sh = gc.open_by_key(f_info['id'])
            for ws in sh.worksheets():
                data = ws.get_all_values()
                race_date = "2025-01-01" # 簡易仮置き
                if len(data) < 3: continue
                
                for r_num in range(1, 13):
                    name_col_idx = (r_num - 1) * 4 + 2
                    rank_col_idx = (r_num - 1) * 4
                    for row in data[2:]:
                        if name_col_idx < len(row):
                            h_name = row[name_col_idx]
                            if not h_name: continue
                            rank_val = row[rank_col_idx] if rank_col_idx < len(row) else ""
                            if h_name not in all_results: all_results[h_name] = []
                            all_results[h_name].append({
                                'date': race_date,
                                'rank': rank_val,
                                'condition': "レース条件",
                                'race_name': "レース名"
                            })
        except Exception:
            continue
    return all_results

# --- レース情報の抽出 ---
def format_excel_time(time_val):
    if not time_val: return ""
    return str(time_val)[:5] # スプレッドシートからは文字列で取得されるため簡易整形

def extract_race_name(race_col_data):
    if len(race_col_data) <= 2: return ""
    name_strs = race_col_data[:-2]
    hit_text = next((str(c) for c in name_strs if any(x in str(c) for x in ["G", "L", "J・G"])), None)
    if not hit_text and name_strs: hit_text = str(name_strs[-1])
    formatted_text = re.sub(r'第\s*\d+\s*回', '', hit_text).strip()
    return re.sub(r'第.*?回\s*', '', formatted_text).strip()

def extract_race_condition(race_col_data):
    if len(race_col_data) < 2: return ""
    cond_str = str(race_col_data[-2])
    match_age = re.search(r'(\d歳(?:以上)?)', cond_str)
    age_part = match_age.group(1) if match_age else ""
    class_part = "障害" if "障害" in cond_str else ""
    for c in ["未勝利", "新馬", "1勝クラス", "2勝クラス", "3勝クラス", "オープン"]:
        if c in cond_str: class_part += c
    return f"{age_part} {class_part}".strip()

def extract_race_course(race_col_data):
    if not race_col_data: return ""
    course_str = str(race_col_data[-1])
    cond_str = str(race_col_data[-2]) if len(race_col_data) > 1 else ""
    cource_part = "障" if "障害" in cond_str or "障" in course_str else ("芝" if "芝" in course_str else ("ダ" if "ダ" in course_str else ""))
    straight_part = "直線 " if "直" in course_str else ""
    dist_match = re.search(r'[\d,]+', course_str)
    distance_part = dist_match.group(0) if dist_match else ""
    return f"{cource_part} {straight_part} {distance_part}m".strip()

# --- ファイル操作・整理の共通処理 ---
def sort_and_resize_table(ws, sort_col_index=0):
    """スプレッドシートのデータをメモリ上でソートし一括更新する"""
    data = ws.get_all_values()
    if len(data) <= 1: return
    headers = data[0]
    rows = [r for r in data[1:] if r and len(r) > sort_col_index and r[sort_col_index]]
    
    rows.sort(key=lambda x: x[sort_col_index] if x[sort_col_index] else "")
    ws.clear()
    ws.update(range_name='A1', values=[headers] + rows)

def get_schedule_data(target_year):
    race_data = f'{target_year}_Race_Data'
    available_dates, venue_data_map, date_map, venue_map = [], {}, {}, {}
    
    try:
        sh = gc.open(race_data)
        ws_sched = sh.worksheet("Schedule")
        data = ws_sched.get_all_values()
        
        for row in data[1:]:
            if not row or not row[0]: continue
            
            d_val = row[0]
            d_str = d_val.replace('/', '-')
            available_dates.append(d_str)
            
            v_info = {}
            for i in [1, 5, 9]:
                if i + 3 < len(row) and row[i+1]:
                    v_id = row[i]
                    v_name = row[i+1]
                    v_day = row[i+2]
                    search_text = row[i+3]
                    
                    v_info[str(v_name)] = {"id": v_id, "day": v_day}
                    
                    date_map[search_text] = d_str
                    venue_map[search_text] = f"{v_id}回{v_name}"
            
            venue_data_map[d_str] = v_info
            
    except Exception as e:
        print(f"Error reading schedule: {e}")
        pass
        
    return available_dates, venue_data_map, date_map, venue_map

def get_race_info_from_sheet(ws_race_data, search_text, target_r_num=None):
    races_info = {}
    if not ws_race_data or len(ws_race_data) < 1: return races_info
    
    headers = ws_race_data[0]
    target_col_idx = None
    
    # 1行目から SearchText に一致する列を探す（空白スペースなどのズレを吸収するためstripで比較）
    clean_search = str(search_text).strip()
    for i, h in enumerate(headers):
        if str(h).strip() == clean_search:
            target_col_idx = i
            break
            
    # 見つからなければ空の辞書を返す
    if target_col_idx is None: return races_info

    # target_col_idx   : レース番号 (例: 1レース)
    # target_col_idx+1 : レース名・条件 (例: 3歳未勝利)
    # target_col_idx+2 : 発走時刻 (例: 10:05)
    label_col_idx = target_col_idx 
    data_col_idx = target_col_idx + 1
    time_col_idx = target_col_idx + 2

    all_rows = ws_race_data[2:] 
    race_start_indices = {}

    # 各レースの開始行を特定する
    for i, row_cells in enumerate(all_rows):
        if label_col_idx < len(row_cells):
            val = str(row_cells[label_col_idx]).strip()
            # 「1R」「12」などから数字だけを確実に抽出
            match = re.search(r'^(\d+)', val)
            if match:
                r_num = int(match.group(1))
                if 1 <= r_num <= 12 and r_num not in race_start_indices:
                    race_start_indices[r_num] = i

    sorted_races = sorted(race_start_indices.items())
    
    for idx, (r_num, start_idx) in enumerate(sorted_races):
        if target_r_num and r_num != target_r_num: continue
        
        # 次のレースの開始行の1つ前まで。12R（最後のレース）の場合はファイルの最後まで。
        end_idx = sorted_races[idx + 1][1] - 1 if idx + 1 < len(sorted_races) else len(all_rows) - 1
        
        race_col_data = []
        for i in range(start_idx, end_idx + 1):
            if data_col_idx < len(all_rows[i]):
                val = str(all_rows[i][data_col_idx]).strip()
                # ★重要: 空のセルを除外することで、コースや条件の抽出（後ろから〇番目）を正常に機能させる
                if val:
                    race_col_data.append(val)

        time_val = ""
        if time_col_idx < len(all_rows[start_idx]):
            time_val = str(all_rows[start_idx][time_col_idx]).strip()

        num_val = all_rows[start_idx][label_col_idx] if label_col_idx < len(all_rows[start_idx]) else f"{r_num}R"

        note_val = ""
        combined_text = "".join(str(v) for v in race_col_data)
        if "牡・牝" in combined_text:
            note_val = "牡・牝"
        elif "（牝）" in combined_text or "(牝)" in combined_text:
            note_val = "牝"

        races_info[r_num] = {
            'time': format_excel_time(time_val),
            'num': num_val,
            'name': extract_race_name(race_col_data),
            'condition': extract_race_condition(race_col_data),
            'course': extract_race_course(race_col_data),
            'note': note_val
        }
    return races_info

def extract_year(date_str):
    """ '2000/1/1', '2000-01-01', '2000' などの文字列から西暦を抽出 """
    if not date_str: 
        return None
    date_str = str(date_str).strip()
    
    # スラッシュやハイフン区切りの形式
    match = re.search(r'^(\d{4})[/-]', date_str)
    if match:
        return int(match.group(1))
    
    # 西暦のみの形式
    match = re.search(r'^(\d{4})', date_str)
    if match:
        return int(match.group(1))
        
    return None

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

def get_5gen_pedigree(sire_name, dam_name, base_birth_year, gc):
    # スプレッドシートから全データを取得
    try:
        wb = gc.open('Horse_Data')
        sire_data = wb.worksheet('Sire').get_all_records()
        dam_data = wb.worksheet('Dam').get_all_records()
    except Exception as e:
        print(f"Spreadsheet fetch error: {e}")
        sire_data = []
        dam_data = []

    sire_dict = {}
    for r in sire_data:
        name = str(r.get('馬名', '')).strip()
        if name not in sire_dict:
            sire_dict[name] = []
        sire_dict[name].append(r)
        
    dam_dict = {}
    for r in dam_data:
        name = str(r.get('馬名', '')).strip()
        if name not in dam_dict:
            dam_dict[name] = []
        dam_dict[name].append(r)

    pedigree = {}

    def traverse(node_index, h_name, child_birth_year, is_sire):
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

        records = sire_dict.get(display_name, []) if is_sire else dam_dict.get(display_name, [])
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
            
            if dob and s and d and b_reg:
                needs_update = False

            b_year = extract_year(dob)
            sire_of_h = s
            dam_of_h = d
            birthplace_region = b_reg
            birthplace_detail = b_det
            
        age_when_born = None
        if b_year is not None and child_birth_year is not None:
            age_when_born = child_birth_year - b_year

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

        traverse(node_index * 2, sire_of_h, b_year, True)
        traverse(node_index * 2 + 1, dam_of_h, b_year, False)

    traverse(2, sire_name, base_birth_year, True)
    traverse(3, dam_name, base_birth_year, False)

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
def index():
    stable_filter = request.args.get('stable', '')
    raw_results = get_all_horses()
    
    results = []
    for h in raw_results:
        horse = list(h)
        if len(horse) > 2:
            try:
                horse[2] = datetime.strptime(horse[2], '%Y/%m/%d')
            except (ValueError, TypeError):
                horse[2] = datetime.now()
        results.append(horse)

    if stable_filter:
        results = [h for h in results if len(h) > 7 and h[7] == stable_filter]
        
    return render_template('index.html', 
                           results=results, 
                           stables=get_stables_list(), 
                           current_year=datetime.now().year,
                           last_updated=format_last_updated(get_last_updated()))

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
        return jsonify(data), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"ページの取得に失敗しました: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"解析中にエラーが発生しました: {e}"}), 500

@app.route('/add_horse')
@login_required
def add_horse_page():
    return render_template('add_horse.html', 
                           stables=get_stables_list(), 
                           default_year=datetime.now().year - 3)

@app.route('/add_horse', methods=['POST'])
@login_required
def add_horse():
    try:
        sh = gc.open(horse_data)
        ws = sh.worksheet("Horses")
        name = request.form.get('name')
        y, m, d = request.form.get('year'), request.form.get('month'), request.form.get('day')
        birth_date_str = f"{y}/{m}/{d}"

        existing_data = ws.col_values(1)
        if name in existing_data[1:]:
            flash(f"エラー：『{name}』は既に登録されています。")
            return redirect('/add_horse')
            
        ws.append_row([
            name,
            request.form.get('gender'),
            birth_date_str, request.form.get('sire'),
            request.form.get('dam'), 
            request.form.get('owner'),
            request.form.get('area'),
            request.form.get('stable_name'),
            request.form.get('status'),
            request.form.get('birthplace_region'),
            request.form.get('birthplace_detail'),
            request.form.get('breeder'),
            request.form.get('jra_url')
        ])
        sort_and_resize_table(ws, sort_col_index=0)
        return redirect(f"/horse/{name}")
    except Exception as e:
        flash(f"エラーが発生しました: {e}")
        return redirect('/add_horse')

@app.route('/add_stable', methods=['POST'])
@login_required
def add_stable():
    name = request.form.get('stable_name')
    kana = request.form.get('kana')
    area = request.form.get('area')
    year = request.form.get('year')
    month = request.form.get('month')
    day = request.form.get('day')
    birth_date_str = f"{year}/{month}/{day}" if year and month and day else ""
    license_year = request.form.get('license_year')
    
    # --- 新規追加項目 ---
    opening = request.form.get('opening')
    retirement = request.form.get('retirement')
    temp_loan = request.form.get('temp_loan') or "0"
    
    capacity = request.form.get('capacity')
    is_technical = request.form.get('is_technical')
    if is_technical:
        capacity = "技術調教師"

    if name and area and area in ["美浦", "栗東"]:
        try:
            sh = gc.open(horse_data)
            sheet = sh.worksheet(area)
            existing_names = sheet.col_values(1)
            
            if name in existing_names[1:]:
                flash(f"エラー: {name}厩舎は既に{area}に登録されています。")
                return redirect('/add_horse')

            # スプレッドシートへ書き込む列の順番を変更
            sheet.append_row([
                name,
                kana_to_hira(re.sub(r'\s+', '', kana)),
                birth_date_str,
                license_year,
                opening,      # E列: 開業
                retirement,   # F列: 引退
                capacity,     # G列: 馬房数
                temp_loan     # H列: 臨時貸付
            ])

            sort_and_resize_table(sheet, sort_col_index=1)
        except Exception as e:
            flash(f"厩舎の追加に失敗しました: {e}")
    return redirect('/add_horse')

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
            status = row[8] if len(row) > 8 else ""
            url = row[12] if len(row) > 12 else ""
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
        status = row[8] if len(row) > 8 else ""
        url = row[12] if len(row) > 12 else ""

        if status == "抹消" or not url:
            return jsonify({"status": "skipped", "message": "対象外の馬です"})

        horse_name = row[0] if len(row) > 0 else ""
        current_gender = row[1] if len(row) > 1 else ""
        current_owner = row[5] if len(row) > 5 else ""
        current_area = row[6] if len(row) > 6 else ""
        current_stable = row[7] if len(row) > 7 else ""

        # JRA公式サイトから最新情報を取得
        info = fetch_jra_horse_status(url)

        ws_changes = sh.worksheet("Changes")
        all_stables = get_stables_list()
        today_str = datetime.now().strftime('%Y/%m/%d')

        # ① 抹消判定
        if info.get('status') == '抹消':
            ws_horses.update(f'I{row_index}', [['抹消']])
            ws_cancel = get_or_create_worksheet(sh, "抹消", ["年月日", "馬名"])
            ws_cancel.append_row([info.get('cancel_date', ''), horse_name])
            return jsonify({"status": "success", "result": "抹消", "horse_name": horse_name})

        # ② 放牧／入厩
        new_status = info.get('status', '入厩')
        if new_status != status:
            ws_horses.update(f'I{row_index}', [[new_status]])

            if status in ('放牧', '入厩') and new_status in ('放牧', '入厩'):
                ws_pasture = get_or_create_worksheet(sh, "放牧入厩", ["放牧年月日", "馬名", "入厩年月日"])
                pasture_data = ws_pasture.get_all_values()

                if new_status == '放牧':
                    ws_pasture.append_row([today_str, horse_name, ''])
                else:  # new_status == '入厩'
                    open_row_idx = None
                    for idx in range(len(pasture_data) - 1, 0, -1):
                        prow = pasture_data[idx]
                        p_name = prow[1] if len(prow) > 1 else ''
                        p_checkin = prow[2] if len(prow) > 2 else ''
                        if p_name == horse_name and not p_checkin:
                            open_row_idx = idx
                            break
                    if open_row_idx is not None:
                        sheet_row_num = open_row_idx + 1
                        ws_pasture.update(f'C{sheet_row_num}', [[today_str]])
                    else:
                        ws_pasture.append_row(['', horse_name, today_str])

        # ③ 去勢判定
        if current_gender == '牡' and info.get('gender') == 'せん':
            ws_horses.update(f'B{row_index}', [['せん']])
            ws_changes.append_row([today_str, horse_name, '去勢'])

        # ④-1 馬主名変更
        new_owner = info.get('owner')
        if new_owner and new_owner != current_owner:
            ws_horses.update(f'F{row_index}', [[new_owner]])
            ws_changes.append_row([today_str, horse_name, '変更', current_owner, new_owner])

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
                    ws_horses.update(f'G{row_index}:H{row_index}', [[new_area, new_stable]])
                    ws_changes.append_row([today_str, horse_name, '転厩', current_full, new_full])

        return jsonify({"status": "success", "horse_name": horse_name})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/finish_update_horses', methods=['POST'])
@login_required
def finish_update_horses():
    """3. 全ての更新完了後に最終更新日時を設定するAPI"""
    try:
        set_last_updated(datetime.now().strftime('%Y/%m/%d %H:%M'))
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
                ws = sh.add_worksheet(title=p_type, rows=100, cols=10)
                # 列構造の変更
                ws.append_row(["馬名", "生年月日", "父", "母", "馬主", "産地", "地域", "生産牧場", "URL"])

            y, m, d = request.form.get('year'), request.form.get('month'), request.form.get('day')
            birth_date_str = f"{y}/{m}/{d}" if y and m and d else (str(y) if y else "")

            # 「同名の馬を登録する」がチェックされている場合は、既存の同名データを
            # 上書きせず、常に新規レコードとして追加する
            force_new = request.form.get('force_new') == '1'

            data = ws.get_all_values()
            found_idx = None if force_new else next((i + 1 for i, row in enumerate(data) if len(row) > 0 and row[0] == p_name), None)
            
            if found_idx:
                ws.update(
                    f'B{found_idx}:I{found_idx}', 
                    [[birth_date_str, 
                    request.form.get('sire'), 
                    request.form.get('dam'),
                    request.form.get('owner'),
                    request.form.get('birthplace_region'),
                    request.form.get('birthplace_detail'),
                    request.form.get('breeder'),
                    request.form.get('jra_url')
                    ]]
                )
            else:
                ws.append_row([
                    p_name, 
                    birth_date_str, 
                    request.form.get('sire'), 
                    request.form.get('dam'), 
                    request.form.get('owner'), 
                    request.form.get('birthplace_region'), 
                    request.form.get('birthplace_detail'), 
                    request.form.get('breeder'),
                    request.form.get('jra_url')
                ])
            sort_and_resize_table(ws, sort_col_index=0)
        except Exception as e:
            flash(f"エラーが発生しました: {e}")
        return redirect(f"/horse/{origin}") if origin else redirect('/')

    # GET時のデータ復元
    p_type, p_name = request.args.get('p_type', 'Sire'), request.args.get('p_name', '')
    existing_data = {
        "year": "", "month": "", "day": "", "sire": "", "dam": "", 
        "owner": "", "birthplace_region": "", "birthplace_detail": "", "breeder": "", "URL": ""
    }
    try:
        sh = gc.open(horse_data)
        ws = sh.worksheet(p_type)
        row = next((r for r in ws.get_all_values() if len(r)>0 and r[0] == p_name), None)
        if row:
            if len(row) > 1 and row[1]:
                parts = re.split(r'[-/]', row[1].strip())
                if len(parts) >= 1: existing_data["year"] = parts[0].strip()
                if len(parts) >= 2: existing_data["month"] = parts[1].strip()
                if len(parts) >= 3: existing_data["day"] = parts[2].strip()
                    
            if len(row) > 2: existing_data["sire"] = row[2]
            if len(row) > 3: existing_data["dam"] = row[3]
            if len(row) > 4: existing_data["owner"] = row[4]
            if len(row) > 5: existing_data["birthplace_region"] = row[5]
            if len(row) > 6: existing_data["birthplace_detail"] = row[6]
            if len(row) > 7: existing_data["breeder"] = row[7]
            if len(row) > 8: existing_data["URL"] = row[8]
    except: pass
    return render_template(
        'add_parent.html', 
        p_type=p_type, 
        p_name=p_name, 
        origin=request.args.get('origin', ''),
        data=existing_data
        )

@app.route('/update_horse', methods=['POST'])
@login_required
def update_horse():
    try:
        new_name = request.form.get('name')
        y, m, d = request.form.get('year'), request.form.get('month'), request.form.get('day')
        birth_date_str = f"{y}/{m}/{d}"
        
        sh = gc.open(horse_data)
        ws = sh.worksheet("Horses")
        data = ws.get_all_values()
        
        for i, row in enumerate(data):
            if len(row) > 0 and row[0] == request.form.get('old_name'):
                update_values = [[
                    new_name, request.form.get('gender'), birth_date_str,
                    request.form.get('sire'), request.form.get('dam'),
                    request.form.get('owner'), request.form.get('location'),
                    request.form.get('stable_name'), request.form.get('status'),
                    request.form.get('birthplace_region'), request.form.get('birthplace_detail'),
                    request.form.get('breeder'),
                    request.form.get('jra_url')
                ]]
                # 12列分更新
                ws.update(f'A{i+1}:M{i+1}', update_values)
                break
        return redirect(f"/horse/{new_name}")
    except Exception as e:
        return f"エラーが発生しました: {e}", 400

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
    date_str = f"{t_year or ''}/{t_month or ''}/{t_day or ''}".strip('/') if (t_year or t_month or t_day) else ""
        
    old_val = request.form.get('old_val', '')
    new_val = request.form.get('new_val', '')
        
    try:
        sh = gc.open(horse_data)
        ws_horses = sh.worksheet("Horses")
        horses_data = ws_horses.get_all_values()
        
        target_idx = next((i + 1 for i, row in enumerate(horses_data) if len(row) > 0 and row[0] == horse_name), -1)
                
        if target_idx != -1:
            if change_type == "転厩":
                parts = new_val.split('・')
                new_area = parts[0] if len(parts) > 1 else ""
                new_stable = parts[1] if len(parts) > 1 else new_val
                ws_horses.update(f'G{target_idx}:H{target_idx}', [[new_area, new_stable]])
            elif change_type == "馬主変更":
                ws_horses.update(f'F{target_idx}', [[new_val]])
            
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
                    if not d:
                        d_sort = "9999/99/99"
                    else:
                        d_sort = d.replace('-', '/')
                        parts = d_sort.split('/')
                        if len(parts) == 3:
                            try:
                                d_sort = f"{int(parts[0]):04d}/{int(parts[1]):02d}/{int(parts[2]):02d}"
                            except ValueError:
                                pass
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
                if str(r.get('馬名', '')).strip() == child_name:
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
        date_str = f"{t_year or ''}/{t_month or ''}/{t_day or ''}".strip('/')
        
    try:
        sh = gc.open(horse_data)
        ws_horses = sh.worksheet("Horses")
        horses_data = ws_horses.get_all_values()
        
        old_area = ""
        old_stable = ""
        target_idx = -1
        
        # 該当馬を検索し、行番号と旧厩舎情報を取得
        for i, row in enumerate(horses_data):
            if len(row) > 0 and row[0] == horse_name:
                target_idx = i + 1
                old_area = row[5] if len(row) > 5 else ""
                old_stable = row[6] if len(row) > 6 else ""
                break
                
        if target_idx != -1:
            # Horsesシートの更新 (F列=拠点, G列=厩舎)
            ws_horses.update(f'F{target_idx}:G{target_idx}', [[new_area, new_stable_name]])
            
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
def horse_detail(name, active_tab):
    if active_tab not in ['profile', 'races']:
        return redirect(url_for('horse_detail', name=name, active_tab='profile'))
    all_horses = get_all_horses()
    horse = next((h for h in all_horses if len(h)>0 and h[0].strip() == name.strip()), None)

    if horse is None:
        # セッションから現在のリトライ回数を取得（なければ0）
        retry_count = session.get('horse_retry_count', 0)

        # 5回未満の場合はカウントアップして同じページへリダイレクト
        if retry_count < 5:
            session['horse_retry_count'] = retry_count + 1
            return redirect(
                url_for('horse_detail', name=name, active_tab=active_tab)
            )

        # 5回リトライしてもダメだった場合はセッションをクリアしてエラー遷移
        session.pop('horse_retry_count', None)
        flash('対象の馬が見つかりませんでした。')
        return redirect(url_for('index'))

    # 成功した場合は、次回のためにリトライカウントをリセットしておく
    session.pop('horse_retry_count', None)
    
    horse_races = []
    
    schedule_cache = {}
    race_master_cache_by_year = {}
    
    try:
        sh = gc.open(horse_data)
        ws_entry = sh.worksheet("Entry")
        records = ws_entry.get_all_values()
        
        entry_rows = []
        if len(records) > 1:
            # 対象の馬のデータだけを抽出
            for row in records[1:]:
                if len(row) >= 4 and row[3] == name:
                    entry_rows.append({
                        'race_date': row[0],
                        'venue': row[1],
                        'race_num': int(row[2]) if row[2].isdigit() else 0,
                        'status': row[4] if len(row) > 4 else "-",
                        'horse_num': row[5] if len(row) > 5 else "-",
                        'rank': row[6] if len(row) > 6 else "-"
                    })
        
        # SQLiteの "ORDER BY race_date DESC, race_num DESC" と同じようにソート
        entry_rows.sort(key=lambda x: (x['race_date'], x['race_num']), reverse=True)
        
        for row in entry_rows:
            r_date = row['race_date']
            r_venue = row['venue']
            r_num = int(row['race_num'])
            target_year = r_date[:4]
            
            if target_year not in schedule_cache:
                _, v_data_map, _, _ = get_schedule_data(target_year)
                schedule_cache[target_year] = v_data_map
            
            v_data_map = schedule_cache[target_year]
            
            if target_year not in race_master_cache_by_year:
                race_master_data = {}
                try:
                    rm_sh = gc.open(f"{target_year}_Race_Data")
                    for sheet in rm_sh.worksheets():
                        race_master_data[sheet.title] = sheet.get_all_values()
                except Exception: 
                    pass
                race_master_cache_by_year[target_year] = race_master_data

            r_name, r_condition, r_course = "-", "-", "-"
            
            v_info = v_data_map.get(r_date, {}).get(r_venue)
            
            if v_info:
                m_sheet_name = f"{v_info['id']}回{r_venue}"
                search_text = f"{v_info['id']}回{r_venue}{v_info['day']}日"
                
                if m_sheet_name in race_master_cache_by_year[target_year]:
                    race_info_cache = get_race_info_from_sheet(
                        race_master_cache_by_year[target_year][m_sheet_name], 
                        search_text
                    )
                    if r_num in race_info_cache:
                        r_name = race_info_cache[r_num].get('name', '-')
                        r_condition = race_info_cache[r_num].get('condition', '-')
                        r_course = race_info_cache[r_num].get('course', '-')

            display_date = r_date
            try:
                dt = datetime.strptime(r_date, '%Y/%m/%d')
                display_date = f"{dt.strftime('%Y年%m月%d日')}({['月','火','水','木','金','土','日'][dt.weekday()]})"
            except Exception:
                pass

            horse_races.append({
                'sort_date': r_date, 
                'date_label': display_date, 
                'venue': r_venue, 
                'num': r_num,
                'name': r_name,
                'condition': r_condition,
                'course': r_course,
                'status': row['status'] if row['status'] else "-", 
                'rank': row['rank'] if row['rank'] else "-"
            })
                
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
        if horse and len(horse) > 3 and horse[3]:
            try:
                s_data = sh.worksheet("Sire").get_all_values()
                s_row = next((r for r in s_data if len(r)>0 and r[0] == horse[3]), None)
                if s_row: sire_info.update({"line1": s_row[2] if len(s_row)>2 else "不明", 
                                            "line2": s_row[3] if len(s_row)>3 else "不明"})
            except gspread.WorksheetNotFound: pass
        if horse and len(horse) > 4 and horse[4]:
            try:
                d_data = sh.worksheet("Dam").get_all_values()
                d_row = next((r for r in d_data if len(r)>0 and r[0] == horse[4]), None)
                if d_row: dam_info.update({"line3": d_row[2] if len(d_row)>2 else "不明", 
                                           "line4": d_row[3] if len(d_row)>3 else "不明"})
            except gspread.WorksheetNotFound: pass
    except: pass

    if horse and len(horse) > 2 and isinstance(horse[2], str):
        horse = list(horse)
        try:
            horse[2] = datetime.strptime(horse[2], '%Y/%m/%d')
        except ValueError:
            try:
                horse[2] = datetime.strptime(horse[2], '%Y-%m-%d')
            except ValueError:
                pass
    
    base_birth_year = None
    if isinstance(horse[2], datetime):
        base_birth_year = horse[2].year
    elif isinstance(horse[2], str):
        base_birth_year = extract_year(horse[2])
    elif isinstance(horse[2], dict) and 'year' in horse[2]:
        base_birth_year = int(horse[2]['year'])
    
    pedigree_data = get_5gen_pedigree(horse[3], horse[4], base_birth_year, gc)

    # 兄弟馬・近親馬（インブリードの下に表示）：ページ初期表示時にサーバー側で計算しておくことで
    # クライアント側の追加fetchによる表示の遅延・ちらつきを無くす
    dam_node = pedigree_data.get(3)
    granddam_node = pedigree_data.get(7)

    def _horse_blank(idx):
        return len(horse) > idx and (horse[idx] is None or str(horse[idx]).strip() == '')

    horse_status = horse[8].strip() if len(horse) > 8 and horse[8] else ''
    horse_has_url = len(horse) > 12 and horse[12] and str(horse[12]).strip() != ''
    horse_has_alert = _horse_blank(5) or _horse_blank(9) or _horse_blank(11)

    family_table1, family_table2 = [], []
    if dam_node and dam_node.get('full_db_name'):
        family_table1, family_table2 = compute_family_tables(
            horse[0], horse[1], base_birth_year or 0, horse[3],
            horse_status, horse_has_url, horse_has_alert,
            dam_node.get('name', ''),
            dam_node.get('full_db_name', ''),
            dam_node.get('birth_year'),
            granddam_node.get('name', '') if granddam_node else '',
            granddam_node.get('full_db_name', '') if granddam_node else '',
            granddam_node.get('birth_year') if granddam_node else None
        )

    return render_template('horse_detail.html', 
                           horse=horse, 
                           horse_races=horse_races,
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
def edit_horse(name):
    horse = next((h for h in get_all_horses() if len(h)>0 and h[0] == name), None)
    return render_template('edit_horse.html', horse=horse, stables=get_stables_list(), current_year=datetime.now().year) if horse else ("Horse not found", 404)

@app.route('/races')
def race_list():
    req_date = request.args.get('date')
    req_venue = request.args.get('venue')
    
    today_obj = datetime.now()
    target_year = req_date[:4] if req_date else str(today_obj.year)
    race_data = f'{target_year}_Race_Data' 
    
    available_dates, venue_data_map, _, _ = get_schedule_data(target_year)
    
    def parse_date(d_str):
        return datetime.strptime(d_str.replace('-', '/'), '%Y/%m/%d')

    sorted_dates = sorted(available_dates, key=parse_date)

    # --- 1 & 2: 基準日の設定と直近開催日の取得 ---
    today_dt = datetime(today_obj.year, today_obj.month, today_obj.day)
    
    try:
        base_dt = parse_date(req_date) if req_date else today_dt
    except ValueError:
        base_dt = today_dt

    future_dates = [d for d in sorted_dates if parse_date(d) >= base_dt]
    date = future_dates[0] if future_dates else (sorted_dates[-1] if sorted_dates else today_obj.strftime('%Y/%-m/%-d'))

    # --- 日付ブロック（節）の作成 ---
    date_blocks, current_block = [], []
    for i, d in enumerate(sorted_dates):
        d_obj = parse_date(d)
        if i == 0:
            current_block.append(d)
        else:
            prev_obj = parse_date(sorted_dates[i-1])
            if (d_obj - prev_obj).days <= 2:
                current_block.append(d)
            else:
                date_blocks.append(current_block)
                current_block = [d]
    if current_block:
        date_blocks.append(current_block)

    current_day_venues = venue_data_map.get(date, {})
    
    # --- 3: 会場選択の保持 ---
    venue = req_venue
    if not venue or venue not in current_day_venues:
        venue = list(current_day_venues.keys())[0] if current_day_venues else ""

    display_dates = []
    current_date_block = next((b for b in date_blocks if date in b), [])
    for d in current_date_block:
        dt_obj = parse_date(d)
        display_dates.append({
            'value': d, 
            'label': f"{dt_obj.month}/{dt_obj.day}({['月','火','水','木','金','土','日'][dt_obj.weekday()]})"
        })

    try:
        date_for_input = parse_date(date).strftime('%Y-%m-%d')
    except:
        date_for_input = date

    # --- 以下、スプレッドシートからのレース情報取得処理 ---
    day_races = {i: None for i in range(1, 13)}
    search_text = "開催情報が見つかりません"

    venue_info = current_day_venues.get(venue)
    if venue_info:
        try:
            wb = gc.open(race_data)
            target_sheet_name = f"{venue_info['id']}回{venue}"
            ws = wb.worksheet(target_sheet_name)
            
            search_text = venue_info.get('search_text', f"{venue_info['id']}回{venue}{venue_info['day']}日")
            
            fetched_races = get_race_info_from_sheet(ws.get_all_values(), search_text)
            
            for i in range(1, 13):
                if i in fetched_races:
                    day_races[i] = fetched_races[i]
                else:
                    day_races[i] = {'num': f"{i}レース", 
                                    'name': '', 
                                    'condition': '情報なし', 
                                    'course': '', 
                                    'time': '',
                                    'note': ''
                                    }
        except Exception as e:
            print(f"Error fetching race details: {e}")
            for i in range(1, 13):
                day_races[i] = {'num': f"{i}レース", 
                                'name': '', 
                                'condition': '取得失敗', 
                                'course': '', 
                                'time': '',
                                'note': ''
                                }

    return render_template('race_list.html',
                           date=date,
                           date_for_input=date_for_input,
                           venue=venue, 
                           day_races=day_races,
                           available_venues=list(current_day_venues.keys()),
                           available_dates=available_dates, 
                           display_dates=display_dates,
                           search_text=search_text)

@app.route('/edit_race')
def race_detail():
    req_date, r_num_target = request.args.get('date'), request.args.get('num', default=1, type=int)
    target_year = req_date[:4] if req_date else str(datetime.now().year)
    
    available_dates, venue_data_map, _, _ = get_schedule_data(target_year)
    
    today_str = datetime.now().strftime('%Y-%m-%d')
    future_dates = [d for d in available_dates if d >= today_str]
    date = req_date if req_date and req_date in available_dates else (future_dates[0] if future_dates else (available_dates[-1] if available_dates else today_str))

    current_day_venues = venue_data_map.get(date, {})
    default_venue = list(current_day_venues.keys())[0] if current_day_venues else ''
    venue = request.args.get('venue', default_venue)

    target_race_data, search_text = None, "開催情報が見つかりません"
    venue_info = current_day_venues.get(venue)
    
    race_data = f"{target_year}_Race_Data"
    if venue_info:
        try:
            wb = gc.open(race_data)
            target_sheet_name = f"{venue_info['id']}回{venue}"
            ws = wb.worksheet(target_sheet_name)
            search_text = f"{venue_info['id']}回{venue}{venue_info['day']}日"
            races_info = get_race_info_from_sheet(ws.get_all_values(), search_text, target_r_num=r_num_target)
            target_race_data = races_info.get(r_num_target)
        except Exception: pass

    available_horses_with_class = []
    if target_race_data:
        all_results_dict = load_all_horse_results()
        race_req_class = judge_required_class(target_race_data['condition'])
        
        for h in get_all_horses():
            if len(h) < 7: continue
            horse_birthday_str = h[2]

            try:
                b_year = int(horse_birthday_str.split('/')[0]) if '/' in horse_birthday_str else int(horse_birthday_str[:4])
                calculated_age = int(target_year) - b_year
            except:
                calculated_age = "不明"

            current_class = get_class_from_results(h[0], date, all_results_dict, horse_birthday_str)

            if race_req_class == "新馬":
                if current_class == "新馬":
                    available_horses_with_class.append({
                        'name': h[0], 'gender': h[1], 'age': calculated_age, 'class': current_class, 'stable': f"{h[5]}・{h[6]}"
                    })
            elif race_req_class == "未勝利":
                if current_class in ["新馬", "未勝利"]:
                    available_horses_with_class.append({
                        'name': h[0], 'gender': h[1], 'age': calculated_age, 'class': current_class, 'stable': f"{h[5]}・{h[6]}"
                    })
            elif race_req_class == "オープン" or current_class == race_req_class:
                available_horses_with_class.append({
                    'name': h[0], 'gender': h[1], 'age': calculated_age, 'class': current_class, 'stable': f"{h[5]}・{h[6]}"
                })
                
    entered_horses = []
    entry_file = f'{target_year}_entry_{venue}'
    try:
        wb_entry = gc.open(entry_file)
        ws_entry = wb_entry.worksheet(search_text)
        data = ws_entry.get_all_values()
        
        rank_col, num_col, name_col, status_col = (r_num_target - 1)*4, (r_num_target - 1)*4 + 1, (r_num_target - 1)*4 + 2, (r_num_target - 1)*4 + 3
        if len(data) > 2:
            for row in data[2:]:
                if name_col < len(row) and row[name_col]:
                    entered_horses.append({
                        'rank': row[rank_col] if rank_col < len(row) else "",
                        'num': row[num_col] if num_col < len(row) else "",
                        'name': row[name_col], 
                        'status': row[status_col] if status_col < len(row) else ""
                    })
    except Exception: pass

    return render_template('race_detail.html',
                           date=date, venue=venue, race=target_race_data,
                           available_dates=available_dates, available_horses=available_horses_with_class,
                           all_horses=get_all_horses(), entered_horses=entered_horses, search_text=search_text)

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
                           granddam_name, granddam_full_name, granddam_birth_year):
    """
    「兄弟馬」テーブルと「近親馬」テーブル用のデータを組み立てる。
    Horsesシートに加えて、Sireシート・Damシートに載っている馬（種牡馬・繁殖牝馬として登録されている
    だけの馬）も血縁馬として拾い上げる。SireシートとDamシートから拾った馬にはリンクを付けない。

    戻り値は (table1_rows, table2_rows) のタプル。各要素は以下のいずれか：
      - {'kind': 'header', 'label': ...}                 見出し行（母／祖母）
      - {'kind': 'divider'}                               区切り線行
      - {'kind': 'row', 'relation':.., 'name':.., ...}    馬の行
    """
    if not dam_full_name:
        return [], []

    all_horses = get_all_horses()

    # 甥・姪／いとこ判定用：ある馬名から「その馬自身の母欄の値」の候補群を引けるようにしておく
    name_candidates = {}
    for h in all_horses:
        if len(h) > 4 and h[0]:
            name_candidates.setdefault(h[0], []).append({'母': h[4], '生年月日': h[2] if len(h) > 2 else ''})

    sire_records, dam_records = [], []
    try:
        sh = gc.open(horse_data)
        try:
            dam_records = sh.worksheet('Dam').get_all_records()
        except Exception:
            dam_records = []
        try:
            sire_records = sh.worksheet('Sire').get_all_records()
        except Exception:
            sire_records = []
    except Exception:
        pass

    for r in dam_records:
        n = str(r.get('馬名', '')).strip()
        if n:
            name_candidates.setdefault(n, []).append(r)

    def resolve_granddam_of(dam_field_value, child_birth_year):
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
        if len(h) < 5 or not h[0]:
            continue
        nm = h[0].strip()
        if not nm or nm == horse_name:
            continue
        existing_names.add(nm)

        def _blank(idx, row=h):
            return len(row) > idx and (row[idx] is None or str(row[idx]).strip() == '')

        candidates.append({
            'name': nm,
            'gender': h[1] if len(h) > 1 else '',
            'birth_str': h[2] if len(h) > 2 else '',
            'sire_disp': h[3] if len(h) > 3 else '',
            'dam_field': h[4].strip() if len(h) > 4 and h[4] else '',
            'status': h[8].strip() if len(h) > 8 and h[8] else '',
            'has_url': len(h) > 12 and h[12] and str(h[12]).strip() != '',
            'has_alert': _blank(5) or _blank(9) or _blank(11),
            'linkable': True,
        })

    for r in sire_records:
        nm = str(r.get('馬名', '')).strip()
        if not nm or nm == horse_name or nm in existing_names:
            continue
        existing_names.add(nm)
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
        if not nm or nm == horse_name or nm in existing_names:
            continue
        existing_names.add(nm)
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
        by_name.setdefault(c['name'], []).append(c)

    def _find_own(name, birth_year):
        """母・祖母自身の表示用データ（性別・父・状態など）を候補リストから探す"""
        opts = by_name.get(name, [])
        if not opts:
            return None
        if birth_year:
            for c in opts:
                if extract_year(c['birth_str']) == birth_year:
                    return c
        return opts[0]

    def _make_row(relation, name, gender, birth_year, sire_disp, status, has_url, has_alert, linkable,
                  is_self=False, parent_name=None):
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
        }

    siblings, nephews, uncles, cousins = [], [], [], []

    for c in candidates:
        target_dam = c['dam_field']
        if not target_dam:
            continue
        target_birth_year = extract_year(c['birth_str']) or 0
        gender = c['gender']

        # 1. 兄弟馬の判定（母が完全一致するか）
        if target_dam == dam_full_name:
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

        # 2. 叔父・叔母の判定（対象馬の母＝本馬の祖母、が一致するか）
        # ※ 本馬の実母自身もここに該当してしまう（祖母の子であるため）ので、実母は除外する
        elif granddam_full_name and target_dam == granddam_full_name:
            if c['name'] == dam_name:
                continue
            relation = "叔父" if gender in ['牡', 'せん'] else "叔母"
            uncles.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                     c['status'], c['has_url'], c['has_alert'], c['linkable']))

        # 3. 甥・姪／いとこの判定
        elif dam_name or granddam_name:
            parent_disp_name, _, _ = split_dam_annotation(target_dam)
            target_granddam = resolve_granddam_of(target_dam, target_birth_year)
            if target_granddam and dam_name and target_granddam == dam_name:
                relation = "甥" if gender in ['牡', 'せん'] else "姪"
                nephews.append(_make_row(relation, c['name'], gender, target_birth_year, c['sire_disp'],
                                          c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                          parent_name=parent_disp_name))
            elif target_granddam and granddam_name and target_granddam == granddam_name:
                cousins.append(_make_row("いとこ", c['name'], gender, target_birth_year, c['sire_disp'],
                                          c['status'], c['has_url'], c['has_alert'], c['linkable'],
                                          parent_name=parent_disp_name))

    def _sort_key(row):
        return row['birth_year'] if isinstance(row['birth_year'], int) else 9999

    def _insert_with_children(base_rows, child_rows_by_parent):
        """
        base_rows（本人の世代の行、生年順）を並べ、各行の直後に、
        その馬を母とする子（甥姪／いとこ）があれば二重線を挟んで差し込む。
        親＋子のまとまりには太枠で囲むための位置情報（group_pos／group_divider）を付与する。
        どの親にも一致しなかった子は、最後にまとめて追加する（この場合は枠なし）。
        """
        remaining = dict(child_rows_by_parent)
        result = []
        for row in base_rows:
            children = remaining.pop(row['name'], None)
            if children:
                children.sort(key=_sort_key)
                row = dict(row)
                row['group_pos'] = 'top'
                result.append(row)
                result.append({'kind': 'divider', 'group_divider': True})
                last_idx = len(children) - 1
                for i, ch in enumerate(children):
                    ch = dict(ch)
                    ch['group_pos'] = 'bottom' if i == last_idx else 'mid'
                    result.append(ch)
            else:
                result.append(row)
        leftover = [r for rows in remaining.values() for r in rows]
        if leftover:
            leftover.sort(key=_sort_key)
            result.append({'kind': 'divider'})
            result.extend(leftover)
        return result

    def _group_by_parent(rows):
        grouped = {}
        for r in rows:
            grouped.setdefault(r['parent_name'], []).append(r)
        return grouped

    # --- テーブル1：母（見出し） → 兄弟馬・本馬（仔がいれば直下に甥姪） ---
    own_row = _make_row('本馬', horse_name, horse_gender, horse_birth_year, horse_sire_disp,
                         horse_status, horse_has_url, horse_has_alert, False, is_self=True)
    siblings_and_self = siblings + [own_row]
    siblings_and_self.sort(key=_sort_key)

    table1 = [{'kind': 'header', 'label': f"母：{dam_name}（{dam_birth_year if dam_birth_year else '不明'}）"}]
    table1.extend(_insert_with_children(siblings_and_self, _group_by_parent(nephews)))

    # --- テーブル2：祖母（見出し） → 叔父叔母・母（叔父叔母に仔がいれば直下にいとこ） ---
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
        cousins_by_parent.pop(dam_name, None)

        table2.append({'kind': 'header',
                        'label': f"祖母：{granddam_name}（{granddam_birth_year if granddam_birth_year else '不明'}）"})
        table2.extend(_insert_with_children(tier2, cousins_by_parent))

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
        if len(h) > 4 and h[0]:
            name_candidates.setdefault(h[0], []).append({'母': h[4], '生年月日': h[2] if len(h) > 2 else ''})
    try:
        sh = gc.open(horse_data)
        for r in sh.worksheet('Dam').get_all_records():
            n = str(r.get('馬名', '')).strip()
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
        if len(h) < 5:
            continue

        target_name = h[0].strip() if h[0] else ''
        if not target_name or target_name == horse_name:
            continue

        target_gender = h[1]
        target_birth_str = h[2]
        target_sire = h[3]
        target_dam = h[4].strip()

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
            target_status = h[8].strip() if len(h) > 8 and h[8] else ''
            has_url = len(h) > 12 and h[12] and str(h[12]).strip() != ''

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
    date_str = request.args.get('date') # YYYY-MM-DD
    
    if not horse_name or not date_str:
        return {"error": "パラメータが不足しています。"}, 400
        
    all_horses = get_all_horses()
    horse = next((h for h in all_horses if len(h) > 0 and h[0] == horse_name), None)
    if not horse:
        return {"error": "馬が見つかりませんでした。"}, 404
        
    horse_birthday_str = horse[2]
    all_results_dict = load_all_horse_results()
    
    # 対象日時点での馬のクラスを取得
    current_class = get_class_from_results(horse_name, date_str, all_results_dict, horse_birthday_str)
    
    target_year = date_str[:4]
    _, venue_data_map, _, _ = get_schedule_data(target_year)
    
    current_day_venues = venue_data_map.get(date_str, {})
    if not current_day_venues:
        return {"races": [], "message": "指定された日に開催されるレースはありません。"}, 200
        
    race_data = f"{target_year}_Race_Data"
    available_races = []
    
    try:
        wb = gc.open(race_data)
        for venue, venue_info in current_day_venues.items():
            target_sheet_name = f"{venue_info['id']}回{venue}"
            try:
                ws = wb.worksheet(target_sheet_name)
                search_text = f"{venue_info['id']}回{venue}{venue_info['day']}日"
                races_info = get_race_info_from_sheet(ws.get_all_values(), search_text)
                
                for r_num, race in races_info.items():
                    race_req_class = judge_required_class(race['condition'])
                    
                    # 出走可能かどうかのクラス判定
                    allowed = False
                    if race_req_class == "新馬" and current_class == "新馬":
                        allowed = True
                    elif race_req_class == "未勝利" and current_class in ["新馬", "未勝利"]:
                        allowed = True
                    elif race_req_class == "オープン":
                        allowed = True
                    elif race_req_class == current_class:
                        allowed = True
                        
                    if allowed:
                        available_races.append({
                            'venue': venue,
                            'race_num': r_num,
                            'name': race.get('name', '-'),
                            'condition': race.get('condition', '-'),
                            'course': race.get('course', '-'),
                            'time': race.get('time', '')
                        })
            except Exception:
                continue
    except Exception as e:
        return {"error": f"レースデータの取得に失敗しました: {e}"}, 500
        
    return {"races": available_races, "current_class": current_class}, 200

if __name__ == '__main__':
    app.run(debug=True, port=5001 ,use_reloader=False)