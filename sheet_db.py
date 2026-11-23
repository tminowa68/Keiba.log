"""Horse_Data のスプレッドシートの代わりに、データベース（PostgreSQL・SQLite）にシートを置く。

gspread の Spreadsheet・Worksheet と同じ使い方（worksheet()・get_all_values()・update()・append_rows() など）が
できるようにしてあるため、app.py の読み書きの処理はそのまま使える。
Google Sheets の API と比べて 1 回の読み書きが速く、1 分あたりの回数制限も無い。

テーブル
  sheet_tabs：シート（タブ）の一覧（ファイル名・シート名・並び順・列数）
  sheet_rows：シートの 1 行ずつの内容（セルの値を JSON の配列で保存。空の行は保存しない）

値はスプレッドシートの画面に表示される文字列と同じ形で保存する（数値も文字列にする）。
"""
import json
import time
import threading

import gspread
from gspread.utils import a1_range_to_grid_range, a1_to_rowcol, fill_gaps, numericise_all, to_records
from sqlalchemy import (Column, Integer, MetaData, String, Table, Text, UniqueConstraint, and_, create_engine,
                        delete, func, insert, select, update)


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

    def __init__(self, url):
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
            UniqueConstraint('book', 'title', name='uq_sheet_tabs_book_title'),
        )
        self.rows = Table(
            'sheet_rows', self.meta,
            Column('sheet_id', Integer, primary_key=True, autoincrement=False),
            Column('row_no', Integer, primary_key=True, autoincrement=False),
            Column('cells', Text, nullable=False),
        )
        self.meta.create_all(self.engine)
        self._tab_cache = {}
        self._lock = threading.Lock()

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
                                              position=position, cols=int(cols or 26)))
        return sheet_id

    def delete_book(self, conn, book):
        ids = [r[0] for r in conn.execute(select(self.tabs.c.sheet_id).where(self.tabs.c.book == book))]
        if ids:
            conn.execute(delete(self.rows).where(self.rows.c.sheet_id.in_(ids)))
            conn.execute(delete(self.tabs).where(self.tabs.c.book == book))

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

    def __init__(self, store, name, fallback=None):
        self._store = store
        self.title = name
        self.id = f'db:{name}'
        self._fallback = fallback   # 共有の設定など、データベースでは扱わない処理に使う本物のスプレッドシートを開く関数
        self._fallback_book = None

    def __getattr__(self, name):
        # list_permissions など、シートの内容以外の機能は本物のスプレッドシートで行う
        if name.startswith('_') or self.__dict__.get('_fallback') is None:
            raise AttributeError(name)
        if self._fallback_book is None:
            self._fallback_book = self._fallback()
        return getattr(self._fallback_book, name)

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
        tab = self._find_tab(lambda t: t[1] == title)
        if tab is None:
            raise gspread.WorksheetNotFound(title)
        return self._ws(tab)

    def worksheets(self, *args, **kwargs):
        return [self._ws(t) for t in self._store.tab_list(self.title)]

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
        with self._store.engine.begin() as conn:
            sheet_id = self._store.create_tab(conn, self.title, title, cols)
        self._forget()
        return self.worksheet(title)

    def duplicate_sheet(self, source_sheet_id, insert_sheet_index=None, new_sheet_id=None, new_sheet_name=None):
        src = self.get_worksheet_by_id(source_sheet_id)
        new_name = new_sheet_name or f'{src.title} のコピー'
        st = self._store
        with st.engine.begin() as conn:
            sheet_id = st.create_tab(conn, self.title, new_name, src._cols)
            rows = conn.execute(select(st.rows.c.row_no, st.rows.c.cells)
                                .where(st.rows.c.sheet_id == src.id)).all()
            if rows:
                conn.execute(insert(st.rows), [{'sheet_id': sheet_id, 'row_no': r.row_no, 'cells': r.cells}
                                               for r in rows])
        self._forget()
        return self.worksheet(new_name)

    def del_worksheet(self, worksheet):
        st = self._store
        with st.engine.begin() as conn:
            conn.execute(delete(st.rows).where(st.rows.c.sheet_id == worksheet.id))
            conn.execute(delete(st.tabs).where(st.tabs.c.sheet_id == worksheet.id))
        self._forget()

    def values_batch_get(self, ranges, params=None):
        """複数のシートの内容をまとめて読み込む（範囲はシート名だけの形 'シート名' に対応）"""
        wanted = []
        for rng in ranges:
            name = rng.rsplit('!', 1)[0] if '!' in rng else rng
            if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
                name = name[1:-1].replace("''", "'")
            tab = self._find_tab(lambda t, name=name: t[1] == name)
            if tab is None:
                raise gspread.WorksheetNotFound(name)
            wanted.append((rng, tab))
        st = self._store
        ids = [t[0] for _, t in wanted]
        by_sheet = {i: {} for i in ids}
        with st.engine.connect() as conn:
            for r in conn.execute(select(st.rows.c.sheet_id, st.rows.c.row_no, st.rows.c.cells)
                                  .where(st.rows.c.sheet_id.in_(ids))):
                by_sheet[r.sheet_id][r.row_no] = json.loads(r.cells)
        value_ranges = []
        for rng, tab in wanted:
            vr = {'range': rng, 'majorDimension': 'ROWS'}
            rows = by_sheet[tab[0]]
            if rows:
                vr['values'] = [rows.get(i, []) for i in range(1, max(rows) + 1)]
            value_ranges.append(vr)
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
        if row_nos is not None:
            if not row_nos:
                return {}
            q = q.where(st.rows.c.row_no.in_(sorted(row_nos)))
        return {r.row_no: json.loads(r.cells) for r in conn.execute(q)}

    def _all_rows(self):
        with self._store.engine.connect() as conn:
            return self._load(conn)

    def get_all_values(self, *args, **kwargs):
        rows = self._all_rows()
        if not rows:
            return [[]]
        return fill_gaps([rows.get(i, []) for i in range(1, max(rows) + 1)])

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
        with self._store.engine.connect() as conn:
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
    def _apply(self, conn, cell_writes):
        """cell_writes：{行番号: {列番号(0始まり): 文字列}} を書き込む（行を読み直して変更し、保存し直す）"""
        if not cell_writes:
            return
        st = self._store
        current = self._load(conn, cell_writes.keys())
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
        conn.execute(delete(st.rows).where(and_(st.rows.c.sheet_id == self.id,
                                                st.rows.c.row_no.in_(sorted(cell_writes)))))
        if new_rows:
            conn.execute(insert(st.rows), new_rows)
        widest = max((max(w) + 1 for w in cell_writes.values()), default=0)
        if widest > self._cols:
            conn.execute(update(st.tabs).where(st.tabs.c.sheet_id == self.id).values(cols=widest))
            self._cols = widest
            self._store.forget_tabs(self.spreadsheet.title)

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
        with self._store.engine.begin() as conn:
            self._apply(conn, writes)
        return {'updatedRange': range_name}

    def batch_update(self, data, *args, **kwargs):
        writes = {}
        for item in data:
            self._collect(writes, item['range'], item['values'])
        with self._store.engine.begin() as conn:
            self._apply(conn, writes)
        return {}

    def update_cell(self, row, col, value):
        writes = {}
        text = cell_text(value)
        if text is not None:
            writes[int(row)] = {int(col) - 1: text}
        with self._store.engine.begin() as conn:
            self._apply(conn, writes)
        return {}

    def update_acell(self, label, value):
        row, col = a1_to_rowcol(label)
        return self.update_cell(row, col, value)

    def append_row(self, values, *args, **kwargs):
        return self.append_rows([values])

    def append_rows(self, values, *args, **kwargs):
        """最後の行（空でない行）の次から書き込む"""
        st = self._store
        with st.engine.begin() as conn:
            last = conn.execute(select(func.max(st.rows.c.row_no))
                                .where(st.rows.c.sheet_id == self.id)).scalar() or 0
            writes = {}
            for i, row in enumerate(values):
                for j, v in enumerate(row):
                    text = cell_text(v)
                    if text is not None:
                        writes.setdefault(last + 1 + i, {})[j] = text
            self._apply(conn, writes)
        return {}

    def clear(self):
        st = self._store
        with st.engine.begin() as conn:
            conn.execute(delete(st.rows).where(st.rows.c.sheet_id == self.id))
        return {}

    def batch_clear(self, ranges):
        with self._store.engine.begin() as conn:
            rows = self._load(conn)
            writes = {}
            for rng in ranges:
                r0, r1, c0, c1 = _grid_range(rng)
                for row_no, cells in rows.items():
                    if row_no - 1 < r0 or (r1 is not None and row_no - 1 >= r1):
                        continue
                    end = len(cells) if c1 is None else min(c1, len(cells))
                    for c in range(c0, end):
                        writes.setdefault(row_no, {})[c] = ''
            self._apply(conn, writes)
        return {}

    def delete_rows(self, start_index, end_index=None):
        """行を削除し、下の行を上に詰める"""
        start, end = int(start_index), int(end_index or start_index)
        count = end - start + 1
        st = self._store
        mine = st.rows.c.sheet_id == self.id
        with st.engine.begin() as conn:
            conn.execute(delete(st.rows).where(and_(mine, st.rows.c.row_no >= start, st.rows.c.row_no <= end)))
            # 主キーが重ならないよう、いったん負の番号にしてから詰める
            conn.execute(update(st.rows).where(and_(mine, st.rows.c.row_no > end))
                         .values(row_no=-st.rows.c.row_no))
            conn.execute(update(st.rows).where(and_(mine, st.rows.c.row_no < 0))
                         .values(row_no=-st.rows.c.row_no - count))
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
