import sqlite3
import re, os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session
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
    ws1.append_row(["馬名", "性別", "生年月日", "父", "母", "馬主名", "拠点", "厩舎", "状態", "産地", "地域", "生産牧場"])
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
                    valid_records = []
                    for r in records:
                        dob = str(r.get('生年月日', '')).strip()
                        b_year = extract_year(dob)
                        if b_year and b_year < child_birth_year:
                            valid_records.append((r, b_year))
                    
                    if valid_records:
                        valid_records.sort(key=lambda x: child_birth_year - x[1])
                        record = valid_records[0][0] 
                        if len(valid_records) > 1:
                            is_ambiguous = True
                            candidates = [r[0] for r in valid_records]
                    else:
                        record = records[0] 
                        if len(records) > 1:
                            is_ambiguous = True
                            candidates = records
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
                           current_year=datetime.now().year)

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
            request.form.get('breeder')
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
                ws.append_row(["馬名", "生年月日", "父", "母", "馬主", "産地", "地域", "生産牧場"])

            y, m, d = request.form.get('year'), request.form.get('month'), request.form.get('day')
            birth_date_str = f"{y}/{m}/{d}" if y and m and d else (str(y) if y else "")

            data = ws.get_all_values()
            found_idx = next((i + 1 for i, row in enumerate(data) if len(row) > 0 and row[0] == p_name), None)
            
            if found_idx:
                ws.update(
                    f'B{found_idx}:H{found_idx}', 
                    [[birth_date_str, 
                    request.form.get('sire'), 
                    request.form.get('dam'),
                    request.form.get('owner'),
                    request.form.get('birthplace_region'),
                    request.form.get('birthplace_detail'),
                    request.form.get('breeder')]]
                )
            else:
                ws.append_row(
                    [p_name, birth_date_str, request.form.get('sire'), request.form.get('dam'),
                    request.form.get('owner'), request.form.get('birthplace_region'),
                    request.form.get('birthplace_detail'), request.form.get('breeder')]
                )
            sort_and_resize_table(ws, sort_col_index=0)
        except Exception as e:
            flash(f"エラーが発生しました: {e}")
        return redirect(f"/horse/{origin}") if origin else redirect('/')

    # GET時のデータ復元
    p_type, p_name = request.args.get('p_type', 'Sire'), request.args.get('p_name', '')
    existing_data = {
        "year": "", "month": "", "day": "", "sire": "", "dam": "", 
        "owner": "", "birthplace_region": "", "birthplace_detail": "", "breeder": ""
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
    except: pass
    return render_template('add_parent.html', p_type=p_type, p_name=p_name, origin=request.args.get('origin', ''), data=existing_data)

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
                    request.form.get('breeder')
                ]]
                # 12列分更新
                ws.update(f'A{i+1}:L{i+1}', update_values)
                break
        return redirect(f"/horse/{new_name}")
    except Exception as e:
        return f"エラーが発生しました: {e}", 400
    
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
    horse = next((h for h in all_horses if len(h)>0 and h[0] == name), None)
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

    return render_template('horse_detail.html', 
                           horse=horse, 
                           horse_races=horse_races,
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