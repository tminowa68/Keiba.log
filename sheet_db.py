"""Horse_Data のスプレッドシートの代わりに、データベース（PostgreSQL・SQLite）にシートを置く。

gspread の Spreadsheet・Worksheet と同じ使い方（worksheet()・get_all_values()・update()・append_rows() など）が
できるようにしてあるため、app.py の読み書きの処理はそのまま使える。
Google Sheets の API と比べて 1 回の読み書きが速く、1 分あたりの回数制限も無い。

テーブル
  sheet_tabs：シート（タブ）の一覧（ファイル名・シート名・並び順・列数）
  sheet_rows：シートの 1 行ずつの内容（セルの値を JSON の配列で保存。空の行は保存しない）

値はスプレッドシートの画面に表示される文字列と同じ形で保存する（数値も文字列にする）。

読み込んだシートの内容はアプリの中に覚えておき、シートの版（sheet_tabs.version。書き込むたびに増やす）が
変わっていなければ、データベースからは読み込み直さない（Neon の無料プランは転送量が月 5GB までのため、
数万行のシートを毎回読み込むとすぐに上限になる）。
覚えた内容はフォルダ（環境変数 SHEET_CACHE_DIR。既定は .sheet_cache）にも保存し、アプリを起動し直したときも、
版が同じシートはそのファイルから読む。

SPREADSHEET_ONLY_SHEETS のシート（手で編集する変換表など）は、データベースには置かず、
今までどおり本物のスプレッドシートで読み書きする（スプレッドシートを編集すれば、そのままアプリに反映される）。
"""
import atexit
import hashlib
import json
import os
import time
import threading

import gspread
from gspread.utils import a1_range_to_grid_range, a1_to_rowcol, fill_gaps, numericise_all, to_records
from sqlalchemy import (Column, Integer, MetaData, String, Table, Text, UniqueConstraint, and_, create_engine,
                        delete, func, inspect, insert, select, text, update)


IN_CHUNK = 5000   # SQL の IN に一度に渡す値の数（PostgreSQL は 1 回の SQL に渡せる値が 65535 個まで）

# データベースに置かず、スプレッドシートのまま読み書きするシート（手で編集するもの）
SPREADSHEET_ONLY_SHEETS = frozenset({'レース名変換表', '地域変換表', '交流競走クラス', '為替レート'})


def _sheet_name_of_range(rng):
    """'シート名'!A1:B2・'シート名'・シート名 の形の範囲からシート名を取り出す"""
    name = rng.rsplit('!', 1)[0] if '!' in rng else rng
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        name = name[1:-1].replace("''", "'")
    return name


# スプレッドシートのままのシートの内容を覚えておく秒数（スプレッドシートを編集すると、この時間内にアプリに反映される）
SPREADSHEET_ONLY_CACHE_TTL = 60
_SHEET_WRITE_METHODS = frozenset({
    'update', 'batch_update', 'update_cell', 'update_acell', 'update_cells', 'append_row', 'append_rows',
    'clear', 'batch_clear', 'delete_rows', 'insert_row', 'insert_rows', 'insert_cols', 'delete_columns',
    'update_title', 'add_cols', 'add_rows', 'resize', 'sort'})


class _SpreadsheetOnlyWorksheet:
    """スプレッドシートのままのシート。本物のシートは使うときに開き（名前だけなら Google に問い合わせない）、
    読み込んだ内容はしばらく覚えておく（書き込んだときは忘れる）"""

    def __init__(self, book, title, ws=None):
        self.title = title
        self._book = book
        self._ws = ws

    def _real_ws(self):
        if self._ws is None:
            self._ws = self._book._real().worksheet(self.title)
        return self._ws

    def get_all_values(self, *args, **kwargs):
        if args or kwargs:
            return self._real_ws().get_all_values(*args, **kwargs)
        cached = self._book._store.cached_real_values(self._book.title, self.title)
        if cached is not None:
            return cached
        values = self._real_ws().get_all_values()
        self._book._store.remember_real_values(self._book.title, self.title, values)
        return [list(r) for r in values]

    def get_values(self, *args, **kwargs):
        return self.get_all_values(*args, **kwargs)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        attr = getattr(self._real_ws(), name)
        if name in _SHEET_WRITE_METHODS and callable(attr):
            def write(*args, **kwargs):
                try:
                    return attr(*args, **kwargs)
                finally:
                    self._book._store.forget_real_values(self._book.title, self.title)
            return write
        return attr


def normalize_url(url):
    """Neon などで表示される接続文字列（postgresql://...）を SQLAlchemy の psycopg 3 用の形にする"""
    url = (url or '').strip()
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    if url.startswith('postgresql://'):
        url = 'postgresql+psycopg://' + url[len('postgresql://'):]
    return url


def cell_text(value):
    """書き込む値を、スプレッドシートに RAW で書き込んで読み直したときと同じ文字列にする（None は書き込まない）"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _trim(cells):
    """行の末尾の空のセルを除く（スプレッドシートの API の読み込み結果と同じ形）"""
    cells = list(cells)
    while cells and cells[-1] == '':
        cells.pop()
    return cells


def _strip_sheet_name(range_name):
    """'シート名'!A1:B2 の形の範囲からシート名を除く"""
    if range_name and '!' in range_name:
        return range_name.rsplit('!', 1)[1]
    return range_name


def _grid_range(range_name):
    """A1 形式の範囲 → (開始行, 終了行, 開始列, 終了列)（0 始まり・終わりは含まない。上限が無い場合は None）"""
    a1 = _strip_sheet_name(range_name) or 'A1'
    g = a1_range_to_grid_range(a1)
    return (g.get('startRowIndex', 0), g.get('endRowIndex'), g.get('startColumnIndex', 0), g.get('endColumnIndex'))


class SheetStore:
    """データベースへの接続と、シート（タブ）の一覧の管理"""
    TAB_CACHE_TTL = 60   # シートの一覧を覚えておく秒数（シートの追加・名前の変更をしたときはすぐに忘れる）

    def __init__(self, url, cache_dir=None):
        url = normalize_url(url)
        kwargs = {}
        if not url.startswith('sqlite'):
            # Neon は使っていない間に止まるため、使う前に接続が切れていないか確かめる
            kwargs = {'pool_pre_ping': True, 'pool_recycle': 300, 'pool_size': 5, 'max_overflow': 5}
        self.engine = create_engine(url, **kwargs)
        self.meta = MetaData()
        self.tabs = Table(
            'sheet_tabs', self.meta,
            Column('sheet_id', Integer, primary_key=True, autoincrement=False),
            Column('book', String(200), nullable=False),
            Column('title', String(200), nullable=False),
            Column('position', Integer, nullable=False, default=0),
            Column('cols', Integer, nullable=False, default=26),
            # シートの版：書き込むたびに増やす（覚えている内容が最新かを、この値だけを読んで確かめる）
            Column('version', Integer, nullable=False, default=0, server_default='0'),
            UniqueConstraint('book', 'title', name='uq_sheet_tabs_book_title'),
        )
        self.rows = Table(
            'sheet_rows', self.meta,
            Column('sheet_id', Integer, primary_key=True, autoincrement=False),
            Column('row_no', Integer, primary_key=True, autoincrement=False),
            Column('cells', Text, nullable=False),
        )
        self.meta.create_all(self.engine)
        self._add_missing_columns()
        self._is_sqlite = url.startswith('sqlite')
        self._rows_cache = {}    # シートID → (版, {行番号: セルの値の一覧})
        # 覚えた内容を保存するフォルダ（接続先ごとに分ける）。None なら保存しない
        if cache_dir is None:
            cache_dir = os.environ.get('SHEET_CACHE_DIR', '.sheet_cache')
        self._cache_dir = (os.path.join(cache_dir, hashlib.sha256(url.encode()).hexdigest()[:16])
                           if cache_dir else None)
        self._saved_versions = {}   # シートID → ファイルに保存した版
        if self._cache_dir:
            atexit.register(self.save_cache_files)
        self._tab_cache = {}
        self._real_values = {}   # (ファイル名, シート名) → (読み込んだ時刻, スプレッドシートのままのシートの内容)
        self._lock = threading.Lock()

    def _add_missing_columns(self):
        """以前に作ったテーブルに、後から足した列（version）が無ければ足す"""
        cols = {c['name'] for c in inspect(self.engine).get_columns('sheet_tabs')}
        if 'version' not in cols:
            with self.engine.begin() as conn:
                conn.execute(text('ALTER TABLE sheet_tabs ADD COLUMN version INTEGER NOT NULL DEFAULT 0'))

    # --- 読み込んだシートの内容（版が変わっていなければ、データベースから読み込み直さない） ---
    def versions(self, conn, sheet_ids, lock=False):
        """{シートID: 版}。lock=True なら書き込みが終わるまで、ほかの書き込みを待たせる"""
        q = select(self.tabs.c.sheet_id, self.tabs.c.version).where(self.tabs.c.sheet_id.in_(list(sheet_ids)))
        if lock and not self._is_sqlite:
            q = q.with_for_update()
        return {r.sheet_id: r.version for r in conn.execute(q)}

    def cached_rows(self, sheet_id, version):
        """覚えている内容（版が同じ場合だけ。中身は書き換えないこと）"""
        with self._lock:
            cached = self._rows_cache.get(sheet_id)
        if cached and cached[0] == version:
            return cached[1]
        return None

    def remember_rows(self, sheet_id, version, rows):
        with self._lock:
            self._rows_cache[sheet_id] = (version, rows)

    def forget_rows(self, sheet_id=None):
        with self._lock:
            if sheet_id is None:
                self._rows_cache.clear()
            else:
                self._rows_cache.pop(sheet_id, None)

    # --- 覚えた内容のファイル ---
    def _cache_path(self, sheet_id):
        return os.path.join(self._cache_dir, f'{int(sheet_id)}.json')

    def _read_cache_file(self, sheet_id, version):
        """ファイルに保存した内容（版が同じ場合だけ）"""
        if not self._cache_dir:
            return None
        try:
            with open(self._cache_path(sheet_id), encoding='utf-8') as f:
                data = json.load(f)
            if data.get('version') != version:
                return None
            self._saved_versions[sheet_id] = version
            return {int(n): cells for n, cells in data['rows'].items()}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache_file(self, sheet_id, version, rows):
        if not self._cache_dir:
            return
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            path = self._cache_path(sheet_id)
            tmp = f'{path}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': version, 'rows': rows}, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp, path)
            self._saved_versions[sheet_id] = version
        except OSError as e:
            print(f"シートの内容をファイルに保存できませんでした: {e}")

    def save_cache_files(self):
        """書き込みで変わった、覚えている内容をファイルに保存し直す（アプリの終了時など）"""
        with self._lock:
            items = list(self._rows_cache.items())
        for sheet_id, (version, rows) in items:
            if self._saved_versions.get(sheet_id) != version:
                self._write_cache_file(sheet_id, version, rows)

    def load_rows(self, conn, sheet_ids):
        """シートの全行を読み込む → {シートID: {行番号: セルの値の一覧}}"""
        out = {i: {} for i in sheet_ids}
        if out:
            for r in conn.execute(select(self.rows.c.sheet_id, self.rows.c.row_no, self.rows.c.cells)
                                  .where(self.rows.c.sheet_id.in_(list(out)))):
                out[r.sheet_id][r.row_no] = json.loads(r.cells)
        return out

    def rows_of(self, sheet_ids):
        """シートの全行 {シートID: {行番号: セルの値の一覧}}。覚えている内容が最新ならそれを使う（中身は書き換えないこと）"""
        sheet_ids = list(dict.fromkeys(sheet_ids))
        if not sheet_ids:
            return {}
        with self.engine.connect() as conn:
            versions = self.versions(conn, sheet_ids)
            out, missing = {}, []
            for i in sheet_ids:
                cached = self.cached_rows(i, versions.get(i))
                if cached is not None:
                    out[i] = cached
                else:
                    missing.append(i)
            # ファイルに保存した内容が最新なら、それを使う
            for i in list(missing):
                if i in versions:
                    saved = self._read_cache_file(i, versions[i])
                    if saved is not None:
                        out[i] = saved
                        self.remember_rows(i, versions[i], saved)
                        missing.remove(i)
            if missing:
                loaded = self.load_rows(conn, missing)
                for i in missing:
                    out[i] = loaded[i]
                    if i in versions:
                        self.remember_rows(i, versions[i], loaded[i])
                        self._write_cache_file(i, versions[i], loaded[i])
        return out

    # --- スプレッドシートのままのシートの内容 ---
    def cached_real_values(self, book, title):
        with self._lock:
            cached = self._real_values.get((book, title))
        if cached and time.time() - cached[0] < SPREADSHEET_ONLY_CACHE_TTL:
            return [list(r) for r in cached[1]]
        return None

    def remember_real_values(self, book, title, values):
        with self._lock:
            self._real_values[(book, title)] = (time.time(), [list(r) for r in values])

    def forget_real_values(self, book, title=None):
        with self._lock:
            for key in [k for k in self._real_values if k[0] == book and (title is None or k[1] == title)]:
                self._real_values.pop(key, None)

    # --- シートの一覧 ---
    def tab_list(self, book):
        with self._lock:
            cached = self._tab_cache.get(book)
            if cached and time.time() - cached[0] < self.TAB_CACHE_TTL:
                return list(cached[1])
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.tabs.c.sheet_id, self.tabs.c.title, self.tabs.c.cols)
                                .where(self.tabs.c.book == book)
                                .order_by(self.tabs.c.position, self.tabs.c.sheet_id)).all()
        tabs = [(r.sheet_id, r.title, r.cols) for r in rows]
        with self._lock:
            self._tab_cache[book] = (time.time(), tabs)
        return list(tabs)

    def forget_tabs(self, book=None):
        with self._lock:
            if book is None:
                self._tab_cache.clear()
            else:
                self._tab_cache.pop(book, None)

    def has_book(self, book):
        return bool(self.tab_list(book))

    def book(self, name, fallback=None):
        return DbSpreadsheet(self, name, fallback)

    def _next_sheet_id(self, conn):
        return (conn.execute(select(func.max(self.tabs.c.sheet_id))).scalar() or 0) + 1

    def create_tab(self, conn, book, title, cols, position=None):
        exists = conn.execute(select(self.tabs.c.sheet_id).where(
            and_(self.tabs.c.book == book, self.tabs.c.title == title))).first()
        if exists:
            raise ValueError(f'「{title}」という名前のシートは既にあります')
        if position is None:
            position = (conn.execute(select(func.max(self.tabs.c.position))
                                     .where(self.tabs.c.book == book)).scalar() or 0) + 1
        sheet_id = self._next_sheet_id(conn)
        conn.execute(insert(self.tabs).values(sheet_id=sheet_id, book=book, title=title,
                                              position=position, cols=int(cols or 26),
                                              version=int(time.time()) % 1000000000))
        self.forget_rows(sheet_id)
        return sheet_id

    def delete_book(self, conn, book):
        ids = [r[0] for r in conn.execute(select(self.tabs.c.sheet_id).where(self.tabs.c.book == book))]
        if ids:
            conn.execute(delete(self.rows).where(self.rows.c.sheet_id.in_(ids)))
            conn.execute(delete(self.tabs).where(self.tabs.c.book == book))
            for i in ids:
                self.forget_rows(i)

    def insert_rows(self, conn, sheet_id, values, start_row=1):
        """行の一覧をまとめて書き込む（空の行は保存しない）"""
        batch = []
        for i, row in enumerate(values):
            cells = _trim('' if v is None else cell_text(v) for v in row)
            if cells:
                batch.append({'sheet_id': sheet_id, 'row_no': start_row + i,
                              'cells': json.dumps(cells, ensure_ascii=False)})
            if len(batch) >= 1000:
                conn.execute(insert(self.rows), batch)
                batch = []
        if batch:
            conn.execute(insert(self.rows), batch)


class DbSpreadsheet:
    """gspread の Spreadsheet の代わり（データベース上のシートのまとまり）"""

    def __init__(self, store, name, fallback=None, spreadsheet_only=SPREADSHEET_ONLY_SHEETS):
        self._store = store
        self.title = name
        self.id = f'db:{name}'
        # 共有の設定や、スプレッドシートのままのシートに使う、本物のスプレッドシートを開く関数
        self._fallback = fallback
        self._fallback_book = None
        self._spreadsheet_only = frozenset(spreadsheet_only) if fallback is not None else frozenset()

    def _real(self):
        if self._fallback_book is None:
            self._fallback_book = self._fallback()
        return self._fallback_book

    def _is_spreadsheet_only(self, title):
        return title in self._spreadsheet_only

    def __getattr__(self, name):
        # list_permissions など、シートの内容以外の機能は本物のスプレッドシートで行う
        if name.startswith('_') or self.__dict__.get('_fallback') is None:
            raise AttributeError(name)
        return getattr(self._real(), name)

    def _forget(self):
        self._store.forget_tabs(self.title)

    def _ws(self, tab):
        sheet_id, title, cols = tab
        return DbWorksheet(self._store, self, sheet_id, title, cols)

    def _find_tab(self, match):
        # 覚えているシートの一覧に無ければ、ほかの処理で追加された場合に備えて一覧を読み直してから探す
        for refresh in (False, True):
            if refresh:
                self._forget()
            for tab in self._store.tab_list(self.title):
                if match(tab):
                    return tab
        return None

    def worksheet(self, title):
        if self._is_spreadsheet_only(title):
            return _SpreadsheetOnlyWorksheet(self, title, self._real().worksheet(title))
        tab = self._find_tab(lambda t: t[1] == title)
        if tab is None:
            raise gspread.WorksheetNotFound(title)
        return self._ws(tab)

    def worksheets(self, *args, **kwargs):
        # データベースのシートと、スプレッドシートのままのシート（名前だけ。中身は使うときに読み込む）
        return ([self._ws(t) for t in self._store.tab_list(self.title) if not self._is_spreadsheet_only(t[1])]
                + [_SpreadsheetOnlyWorksheet(self, t) for t in sorted(self._spreadsheet_only)])

    def get_worksheet_by_id(self, sheet_id):
        tab = self._find_tab(lambda t: t[0] == int(sheet_id))
        if tab is None:
            raise gspread.WorksheetNotFound(str(sheet_id))
        return self._ws(tab)

    @property
    def sheet1(self):
        tabs = self._store.tab_list(self.title)
        if not tabs:
            raise gspread.WorksheetNotFound('sheet1')
        return self._ws(tabs[0])

    def add_worksheet(self, title, rows=1000, cols=26, index=None):
        if self._is_spreadsheet_only(title):
            ws = self._real().add_worksheet(title=title, rows=rows, cols=cols)
            self._store.forget_real_values(self.title, title)
            return _SpreadsheetOnlyWorksheet(self, title, ws)
        with self._store.engine.begin() as conn:
            sheet_id = self._store.create_tab(conn, self.title, title, cols)
        self._forget()
        return self.worksheet(title)

    def duplicate_sheet(self, source_sheet_id, insert_sheet_index=None, new_sheet_id=None, new_sheet_name=None):
        src = self.get_worksheet_by_id(source_sheet_id)
        new_name = new_sheet_name or f'{src.title} のコピー'
        st = self._store
        rows = st.rows_of([src.id])[src.id]   # 覚えている内容があればそれを使う
        with st.engine.begin() as conn:
            sheet_id = st.create_tab(conn, self.title, new_name, src._cols)
            if rows:
                conn.execute(insert(st.rows), [{'sheet_id': sheet_id, 'row_no': n,
                                                'cells': json.dumps(cells, ensure_ascii=False)}
                                               for n, cells in rows.items()])
        self._forget()
        return self.worksheet(new_name)

    def del_worksheet(self, worksheet):
        if not isinstance(worksheet, DbWorksheet):
            return self._real().del_worksheet(getattr(worksheet, '_ws', None) or worksheet)
        st = self._store
        with st.engine.begin() as conn:
            conn.execute(delete(st.rows).where(st.rows.c.sheet_id == worksheet.id))
            conn.execute(delete(st.tabs).where(st.tabs.c.sheet_id == worksheet.id))
        st.forget_rows(worksheet.id)
        self._forget()

    def values_batch_get(self, ranges, params=None):
        """複数のシートの内容をまとめて読み込む（範囲はシート名だけの形 'シート名' に対応）"""
        # スプレッドシートのままのシートは、本物のスプレッドシートからまとめて読み込む
        # （シート名だけの範囲は、覚えている内容があればそれを使う）
        real = {}
        real_ranges = []
        for r in ranges:
            name = _sheet_name_of_range(r)
            if not self._is_spreadsheet_only(name):
                continue
            cached = self._store.cached_real_values(self.title, name) if '!' not in r else None
            if cached is not None:
                vr = {'range': r, 'majorDimension': 'ROWS'}
                if cached and cached != [[]]:
                    vr['values'] = cached
                real[r] = vr
            else:
                real_ranges.append(r)
        if real_ranges:
            res = self._real().values_batch_get(real_ranges)
            for r, vr in zip(real_ranges, res.get('valueRanges', [])):
                real[r] = vr
                if '!' not in r:
                    self._store.remember_real_values(self.title, _sheet_name_of_range(r),
                                                     fill_gaps(vr.get('values', []) or [[]]))
        wanted = []
        for rng in ranges:
            if rng in real:
                continue
            name = _sheet_name_of_range(rng)
            tab = self._find_tab(lambda t, name=name: t[1] == name)
            if tab is None:
                raise gspread.WorksheetNotFound(name)
            wanted.append((rng, tab))
        by_sheet = self._store.rows_of([t[0] for _, t in wanted])
        db_ranges = {}
        for rng, tab in wanted:
            vr = {'range': rng, 'majorDimension': 'ROWS'}
            rows = by_sheet[tab[0]]
            if rows:
                vr['values'] = [list(rows.get(i, [])) for i in range(1, max(rows) + 1)]
            db_ranges[rng] = vr
        value_ranges = [real[r] if r in real else db_ranges[r] for r in ranges]
        return {'spreadsheetId': self.id, 'valueRanges': value_ranges}


class DbWorksheet:
    """gspread の Worksheet の代わり（データベース上の 1 枚のシート）"""

    def __init__(self, store, book, sheet_id, title, cols):
        self._store = store
        self.spreadsheet = book
        self.id = sheet_id
        self.title = title
        self._cols = cols

    def __repr__(self):
        return f"<DbWorksheet {self.title!r} id:{self.id}>"

    # --- 読み込み ---
    def _load(self, conn, row_nos=None):
        st = self._store
        q = select(st.rows.c.row_no, st.rows.c.cells).where(st.rows.c.sheet_id == self.id)
        if row_nos is None:
            return {r.row_no: json.loads(r.cells) for r in conn.execute(q)}
        row_nos = sorted(row_nos)
        if not row_nos:
            return {}
        out = {}
        # 行番号を多く指定する場合は、まとめて範囲で読み込む（SQLに渡せる値の数に上限があるため）
        if len(row_nos) > IN_CHUNK:
            q = q.where(and_(st.rows.c.row_no >= row_nos[0], st.rows.c.row_no <= row_nos[-1]))
            wanted = set(row_nos)
            return {r.row_no: json.loads(r.cells) for r in conn.execute(q) if r.row_no in wanted}
        return {r.row_no: json.loads(r.cells) for r in conn.execute(q.where(st.rows.c.row_no.in_(row_nos)))}

    def _all_rows(self):
        """全行（覚えている内容が最新ならそれを使う。中身は書き換えないこと）"""
        return self._store.rows_of([self.id])[self.id]

    def get_all_values(self, *args, **kwargs):
        rows = self._all_rows()
        if not rows:
            return [[]]
        return fill_gaps([list(rows.get(i, [])) for i in range(1, max(rows) + 1)])

    def get_values(self, *args, **kwargs):
        if args or kwargs.get('range_name'):
            raise NotImplementedError('データベースのシートでは、範囲を指定した読み込みには対応していません')
        return self.get_all_values()

    def get_all_records(self, head=1, default_blank='', empty2zero=False, **kwargs):
        values = self.get_all_values()
        if len(values) < head:
            return []
        keys = values[head - 1]
        rows = [numericise_all(list(r) + [''] * (len(keys) - len(r)), empty2zero, default_blank)
                for r in values[head:]]
        return to_records(keys, rows)

    def row_values(self, row, *args, **kwargs):
        st = self._store
        with st.engine.connect() as conn:
            cached = st.cached_rows(self.id, st.versions(conn, [self.id]).get(self.id))
            if cached is not None:
                return list(cached.get(int(row), []))
            return list(self._load(conn, [int(row)]).get(int(row), []))

    def col_values(self, col, *args, **kwargs):
        idx = int(col) - 1
        rows = self._all_rows()
        out = [(rows.get(i, [])[idx] if idx < len(rows.get(i, [])) else '')
               for i in range(1, (max(rows) if rows else 0) + 1)]
        return _trim(out)

    @property
    def col_count(self):
        return self._cols

    @property
    def row_count(self):
        with self._store.engine.connect() as conn:
            last = conn.execute(select(func.max(self._store.rows.c.row_no))
                                .where(self._store.rows.c.sheet_id == self.id)).scalar() or 0
        return max(last, 1000)

    # --- 書き込み ---
    def _write(self, func):
        """書き込みの処理 func(conn, base) を行い、シートの版を増やす。base は覚えている全行（最新でなければ None）。
        func は、base を元に書き込み後の全行を作れた場合はそれを返す（覚えている内容をそれに置き換える）"""
        st = self._store
        with st.engine.begin() as conn:
            version = st.versions(conn, [self.id], lock=True).get(self.id, 0)
            base = st.cached_rows(self.id, version)
            after = func(conn, base)
            conn.execute(update(st.tabs).where(st.tabs.c.sheet_id == self.id).values(version=version + 1))
        if after is not None:
            st.remember_rows(self.id, version + 1, after)
        else:
            st.forget_rows(self.id)

    def _apply(self, conn, cell_writes, base=None):
        """cell_writes：{行番号: {列番号(0始まり): 文字列}} を書き込む（行を読み直して変更し、保存し直す）。
        base（覚えている全行）があれば、行はそこから読み、書き込み後の全行を返す（無ければ None を返す）"""
        if not cell_writes:
            return dict(base) if base is not None else None
        st = self._store
        current = base if base is not None else self._load(conn, cell_writes.keys())
        after = dict(base) if base is not None else None
        new_rows = []
        for row_no, writes in cell_writes.items():
            cells = list(current.get(row_no, []))
            width = max(writes) + 1
            if len(cells) < width:
                cells += [''] * (width - len(cells))
            for c, v in writes.items():
                cells[c] = v
            cells = _trim(cells)
            if cells:
                new_rows.append({'sheet_id': self.id, 'row_no': row_no,
                                 'cells': json.dumps(cells, ensure_ascii=False)})
            if after is not None:
                if cells:
                    after[row_no] = cells
                else:
                    after.pop(row_no, None)
        touched = sorted(cell_writes)
        for k in range(0, len(touched), IN_CHUNK):
            conn.execute(delete(st.rows).where(and_(st.rows.c.sheet_id == self.id,
                                                    st.rows.c.row_no.in_(touched[k:k + IN_CHUNK]))))
        if new_rows:
            conn.execute(insert(st.rows), new_rows)
        widest = max((max(w) + 1 for w in cell_writes.values()), default=0)
        if widest > self._cols:
            conn.execute(update(st.tabs).where(st.tabs.c.sheet_id == self.id).values(cols=widest))
            self._cols = widest
            self._store.forget_tabs(self.spreadsheet.title)
        return after

    @staticmethod
    def _collect(cell_writes, range_name, values):
        r0, _, c0, _ = _grid_range(range_name)
        for i, row in enumerate(values or []):
            for j, v in enumerate(row):
                text = cell_text(v)
                if text is not None:   # None のセルは書き込まない（スプレッドシートの API と同じ）
                    cell_writes.setdefault(r0 + 1 + i, {})[c0 + j] = text

    def update(self, values=None, range_name=None, *args, **kwargs):
        # 古い書き方（範囲, 値）にも対応する（gspread と同じ）
        if isinstance(values, str) and isinstance(range_name, (list, tuple)):
            values, range_name = range_name, values
        writes = {}
        self._collect(writes, range_name, values)
        self._write(lambda conn, base: self._apply(conn, writes, base))
        return {'updatedRange': range_name}

    def batch_update(self, data, *args, **kwargs):
        writes = {}
        for item in data:
            self._collect(writes, item['range'], item['values'])
        self._write(lambda conn, base: self._apply(conn, writes, base))
        return {}

    def update_cell(self, row, col, value):
        writes = {}
        text = cell_text(value)
        if text is not None:
            writes[int(row)] = {int(col) - 1: text}
        self._write(lambda conn, base: self._apply(conn, writes, base))
        return {}

    def update_acell(self, label, value):
        row, col = a1_to_rowcol(label)
        return self.update_cell(row, col, value)

    def append_row(self, values, *args, **kwargs):
        return self.append_rows([values])

    def append_rows(self, values, *args, **kwargs):
        """最後の行（空でない行）の次から書き込む"""
        st = self._store

        def append(conn, base):
            if base is not None:
                last = max(base) if base else 0
            else:
                last = conn.execute(select(func.max(st.rows.c.row_no))
                                    .where(st.rows.c.sheet_id == self.id)).scalar() or 0
            writes = {}
            for i, row in enumerate(values):
                for j, v in enumerate(row):
                    text = cell_text(v)
                    if text is not None:
                        writes.setdefault(last + 1 + i, {})[j] = text
            return self._apply(conn, writes, base)
        self._write(append)
        return {}

    def clear(self):
        st = self._store

        def clear_all(conn, base):
            conn.execute(delete(st.rows).where(st.rows.c.sheet_id == self.id))
            return {}
        self._write(clear_all)
        return {}

    def batch_clear(self, ranges):
        def clear_ranges(conn, base):
            rows = base if base is not None else self._load(conn)
            writes = {}
            for rng in ranges:
                r0, r1, c0, c1 = _grid_range(rng)
                for row_no, cells in rows.items():
                    if row_no - 1 < r0 or (r1 is not None and row_no - 1 >= r1):
                        continue
                    end = len(cells) if c1 is None else min(c1, len(cells))
                    for c in range(c0, end):
                        writes.setdefault(row_no, {})[c] = ''
            # 全行を読み込んだので、書き込み後の全行も覚えておける
            return self._apply(conn, writes, rows)
        self._write(clear_ranges)
        return {}

    def delete_rows(self, start_index, end_index=None):
        """行を削除し、下の行を上に詰める"""
        start, end = int(start_index), int(end_index or start_index)
        count = end - start + 1
        st = self._store
        mine = st.rows.c.sheet_id == self.id

        def delete_and_shift(conn, base):
            conn.execute(delete(st.rows).where(and_(mine, st.rows.c.row_no >= start, st.rows.c.row_no <= end)))
            # 主キーが重ならないよう、いったん負の番号にしてから詰める
            conn.execute(update(st.rows).where(and_(mine, st.rows.c.row_no > end))
                         .values(row_no=-st.rows.c.row_no))
            conn.execute(update(st.rows).where(and_(mine, st.rows.c.row_no < 0))
                         .values(row_no=-st.rows.c.row_no - count))
            if base is None:
                return None
            return {(n if n < start else n - count): cells for n, cells in base.items() if not start <= n <= end}
        self._write(delete_and_shift)
        return {}

    def add_cols(self, cols):
        st = self._store
        with st.engine.begin() as conn:
            conn.execute(update(st.tabs).where(st.tabs.c.sheet_id == self.id).values(cols=self._cols + int(cols)))
        self._cols += int(cols)
        st.forget_tabs(self.spreadsheet.title)

    def resize(self, rows=None, cols=None):
        if cols is not None and int(cols) > self._cols:
            self.add_cols(int(cols) - self._cols)

    def update_title(self, title):
        st = self._store
        with st.engine.begin() as conn:
            conn.execute(update(st.tabs).where(st.tabs.c.sheet_id == self.id).values(title=title))
        self.title = title
        st.forget_tabs(self.spreadsheet.title)
        return {}
