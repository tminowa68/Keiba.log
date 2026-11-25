"""sheet_db（データベース上のシート）が、gspread（Google Sheets）と同じように動くかのテスト。

  python -m unittest tests.test_sheet_db
PostgreSQL でも試す場合は、環境変数 SHEET_DB_TEST_URL に接続文字列を設定する（中のデータは消える）。
"""
import os
import sys
import tempfile
import unittest

import gspread

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sheet_db  # noqa: E402


class SheetDbTests:
    url = None

    def setUp(self):
        self.store = sheet_db.SheetStore(self.url, cache_dir='')
        with self.store.engine.begin() as conn:
            conn.execute(self.store.rows.delete())
            conn.execute(self.store.tabs.delete())
        self.store.forget_tabs()
        self.book = self.store.book('Horse_Data')
        self.ws = self.book.add_worksheet(title='Horses', rows=100, cols=3)
        self.ws.append_row(['馬名', '英馬名', '性別'])

    def tearDown(self):
        self.store.engine.dispose()

    def test_empty_sheet(self):
        ws = self.book.add_worksheet(title='空', rows=10, cols=2)
        self.assertEqual(ws.get_all_values(), [[]])
        self.assertEqual(ws.row_values(1), [])
        self.assertEqual(ws.col_values(1), [])
        self.assertEqual(ws.get_all_records(), [])

    def test_worksheet_not_found(self):
        with self.assertRaises(gspread.WorksheetNotFound):
            self.book.worksheet('無いシート')
        self.assertEqual([w.title for w in self.book.worksheets()], ['Horses'])
        self.assertEqual(self.book.sheet1.title, 'Horses')

    def test_append_and_read_padded(self):
        self.ws.append_rows([['アイ', '', '牡'], ['', 'Bee'], [], ['ウマ']])
        self.assertEqual(self.ws.get_all_values(), [
            ['馬名', '英馬名', '性別'], ['アイ', '', '牡'], ['', 'Bee', ''], ['', '', ''], ['ウマ', '', '']])
        # 空の行の次ではなく、最後の行の次に追加される
        self.ws.append_row(['エ'])
        self.assertEqual(self.ws.get_all_values()[-1], ['エ', '', ''])

    def test_number_values_become_text(self):
        self.ws.append_row([1, 2.0, 2.5])
        self.ws.append_row([True, None, '007'])
        self.assertEqual(self.ws.get_all_values()[1:], [['1', '2', '2.5'], ['TRUE', '', '007']])

    def test_update_forms(self):
        self.ws.append_rows([['a', 'b', 'c'], ['d', 'e', 'f']])
        self.ws.update(range_name='B2', values=[['X']])
        self.ws.update('C3', [['Y']])            # 古い書き方（範囲, 値）
        self.ws.update([['Z1', 'Z2']], 'A3:B3')  # 新しい書き方（値, 範囲）
        self.assertEqual(self.ws.get_all_values()[1:], [['a', 'X', 'c'], ['Z1', 'Z2', 'Y']])
        # 範囲より値が少ない場合、残りのセルはそのまま
        self.ws.update(range_name='A2:C2', values=[['only']])
        self.assertEqual(self.ws.row_values(2), ['only', 'X', 'c'])
        # 空の文字列はセルを空にし、行の末尾は詰める
        self.ws.update(range_name='B2:C2', values=[['', '']])
        self.assertEqual(self.ws.row_values(2), ['only'])

    def test_update_beyond_end_and_whole_sheet(self):
        self.ws.update(range_name='E6', values=[['far']])
        values = self.ws.get_all_values()
        self.assertEqual(len(values), 6)
        self.assertEqual(values[5], ['', '', '', '', 'far'])
        self.assertGreaterEqual(self.ws.col_count, 5)
        self.ws.clear()
        self.assertEqual(self.ws.get_all_values(), [[]])
        self.ws.update(range_name='A1', values=[['h1', 'h2'], ['v1', 'v2']])
        self.assertEqual(self.ws.get_all_values(), [['h1', 'h2'], ['v1', 'v2']])

    def test_batch_update_and_cells(self):
        self.ws.append_rows([['a', 'b'], ['c', 'd']])
        self.ws.batch_update([{'range': 'A2', 'values': [['A']]}, {'range': 'B3:C3', 'values': [['B', 'C']]}])
        self.ws.update_cell(2, 3, 'U')
        self.ws.update_acell('A3', 'V')
        self.assertEqual(self.ws.get_all_values()[1:], [['A', 'b', 'U'], ['V', 'B', 'C']])

    def test_batch_clear_and_delete_rows(self):
        self.ws.append_rows([['1', 'x'], ['2', 'y'], ['3', 'z'], ['4', 'w']])
        self.ws.batch_clear(['A4:C5'])
        self.assertEqual(self.ws.get_all_values()[1:], [['1', 'x', ''], ['2', 'y', '']])
        self.ws.append_rows([['3'], ['4'], ['5']])
        self.ws.delete_rows(3)
        self.assertEqual([r[0] for r in self.ws.get_all_values()], ['馬名', '1', '3', '4', '5'])
        self.ws.delete_rows(3, 4)
        self.assertEqual([r[0] for r in self.ws.get_all_values()], ['馬名', '1', '5'])

    def test_rewrite_many_rows(self):
        # 行の多いシートの書き直し（Horses・Changes は数万行ある）
        rows = [[f'馬{i}', '', '牡'] for i in range(70000)]
        self.ws.update(range_name='A2', values=rows)
        self.ws.update(range_name='A2', values=rows[::-1])
        values = self.ws.get_all_values()
        self.assertEqual(len(values), 70001)
        self.assertEqual(values[1][0], '馬69999')
        self.assertEqual(values[-1][0], '馬0')

    def test_row_col_values_and_records(self):
        self.ws.append_rows([['アイ', '', '牡'], ['', 'Bee', '3']])
        self.assertEqual(self.ws.row_values(2), ['アイ', '', '牡'])
        self.assertEqual(self.ws.col_values(1), ['馬名', 'アイ'])
        self.assertEqual(self.ws.col_values(2), ['英馬名', '', 'Bee'])
        self.assertEqual(self.ws.get_all_records(), [
            {'馬名': 'アイ', '英馬名': '', '性別': '牡'}, {'馬名': '', '英馬名': 'Bee', '性別': 3}])

    def test_sheet_operations(self):
        self.ws.append_row(['アイ'])
        copy = self.book.duplicate_sheet(self.ws.id, new_sheet_name='Horses_移行前')
        self.assertEqual(copy.get_all_values(), self.ws.get_all_values())
        self.ws.update_title('Horses_old')
        self.assertEqual(self.book.worksheet('Horses_old').get_all_values()[1], ['アイ', '', ''])
        with self.assertRaises(gspread.WorksheetNotFound):
            self.book.worksheet('Horses')
        with self.assertRaises(ValueError):
            self.book.add_worksheet(title='Horses_移行前')
        self.book.del_worksheet(copy)
        self.assertEqual([w.title for w in self.book.worksheets()], ['Horses_old'])
        ws = self.book.worksheet('Horses_old')
        before = ws.col_count
        ws.add_cols(2)
        self.assertEqual(self.book.worksheet('Horses_old').col_count, before + 2)

    def test_values_batch_get(self):
        other = self.book.add_worksheet(title="O'Neil", rows=10, cols=2)
        other.append_rows([['x'], [], ['y', 'z']])
        res = self.book.values_batch_get(["'Horses'", "'O''Neil'"])
        vrs = res['valueRanges']
        self.assertEqual(vrs[0]['values'], [['馬名', '英馬名', '性別']])
        self.assertEqual(gspread.utils.fill_gaps(vrs[1]['values']), [['x', ''], ['', ''], ['y', 'z']])

    def test_sheet_added_by_another_connection(self):
        self.book.worksheets()   # シートの一覧を覚えさせる
        other_store = sheet_db.SheetStore(self.url, cache_dir='')
        other_store.book('Horse_Data').add_worksheet(title='美浦').append_row(['厩舎名'])
        other_store.engine.dispose()
        self.assertEqual(self.book.worksheet('美浦').get_all_values(), [['厩舎名']])

    def _count_loads(self, store):
        calls = []
        original = store.load_rows

        def counting(conn, ids):
            calls.append(list(ids))
            return original(conn, ids)
        store.load_rows = counting
        return calls

    def test_reads_use_cache_until_changed(self):
        self.ws.append_rows([['アイ', '', '牡'], ['ウマ', '', '牝']])
        calls = self._count_loads(self.store)
        first = self.ws.get_all_values()
        # 書き込みの後も、覚えている内容を書き換えるので読み込み直さない
        self.ws.update(range_name='C2', values=[['せん']])
        self.ws.append_row(['エ'])
        self.ws.batch_update([{'range': 'B3', 'values': [['Uma']]}])
        self.ws.delete_rows(2)
        values = self.ws.get_all_values()
        self.book.values_batch_get(["'Horses'"])
        self.assertEqual(self.ws.row_values(2), ['ウマ', 'Uma', '牝'])
        self.assertEqual(calls, [[self.ws.id]])   # 最初の 1 回だけ
        self.assertEqual(values, [['馬名', '英馬名', '性別'], ['ウマ', 'Uma', '牝'], ['エ', '', '']])
        # 返した値を書き換えても、覚えている内容は変わらない
        values[1][0] = '書き換え'
        first[0][0] = '書き換え'
        self.assertEqual(self.ws.get_all_values()[1][0], 'ウマ')
        # データベースの内容と一致している
        self.store.forget_rows()
        self.assertEqual(self.ws.get_all_values(), [['馬名', '英馬名', '性別'], ['ウマ', 'Uma', '牝'], ['エ', '', '']])

    def test_cache_notices_writes_from_another_process(self):
        self.ws.append_row(['アイ'])
        self.assertEqual(len(self.ws.get_all_values()), 2)
        other = sheet_db.SheetStore(self.url, cache_dir='')
        other.book('Horse_Data').worksheet('Horses').append_row(['ウマ'])
        other.engine.dispose()
        self.assertEqual([r[0] for r in self.ws.get_all_values()], ['馬名', 'アイ', 'ウマ'])
        self.ws.append_row(['エ'])
        self.assertEqual([r[0] for r in self.ws.get_all_values()], ['馬名', 'アイ', 'ウマ', 'エ'])

    def test_clear_and_batch_clear_keep_cache_correct(self):
        self.ws.append_rows([['1'], ['2'], ['3']])
        self.ws.get_all_values()
        self.ws.batch_clear(['A3:C4'])
        self.assertEqual([r[0] for r in self.ws.get_all_values()], ['馬名', '1'])
        self.ws.clear()
        self.assertEqual(self.ws.get_all_values(), [[]])
        self.ws.update(range_name='A1', values=[['x']])
        self.store.forget_rows()
        self.assertEqual(self.ws.get_all_values(), [['x']])

    def test_adds_version_column_to_old_table(self):
        with self.store.engine.begin() as conn:
            if self.url.startswith('sqlite'):
                return   # SQLite は列の削除に手間がかかるため、PostgreSQL だけで確かめる
            conn.execute(sheet_db.text('ALTER TABLE sheet_tabs DROP COLUMN version'))
        store = sheet_db.SheetStore(self.url, cache_dir='')
        self.assertEqual(store.book('Horse_Data').worksheet('Horses').get_all_values()[0][0], '馬名')
        store.engine.dispose()

    def test_cache_file_used_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            store = sheet_db.SheetStore(self.url, cache_dir=d)
            ws = store.book('Horse_Data').worksheet('Horses')
            ws.append_row(['アイ'])
            ws.get_all_values()          # 読み込んでファイルに保存する
            ws.append_row(['ウマ'])      # 書き込み（ファイルは終了時に保存し直す）
            store.save_cache_files()
            store.engine.dispose()
            # 起動し直したとき：データベースから全行を読まず、ファイルから読む
            store2 = sheet_db.SheetStore(self.url, cache_dir=d)
            calls = self._count_loads(store2)
            self.assertEqual([r[0] for r in store2.book('Horse_Data').worksheet('Horses').get_all_values()],
                             ['馬名', 'アイ', 'ウマ'])
            self.assertEqual(calls, [])
            # ほかで書き込まれていたら（版が違えば）データベースから読み直す
            self.ws.append_row(['エ'])
            store2.forget_rows()
            self.assertEqual(store2.book('Horse_Data').worksheet('Horses').get_all_values()[-1][0], 'エ')
            self.assertEqual(len(calls), 1)
            store2.engine.dispose()

    def test_books_are_separate(self):
        other = self.store.book('別のファイル')
        self.assertEqual(other.worksheets(), [])
        other.add_worksheet(title='Horses')
        self.assertEqual(self.book.worksheet('Horses').get_all_values()[0][0], '馬名')
        self.assertEqual(other.worksheet('Horses').get_all_values(), [[]])


class FakeRealBook:
    """本物のスプレッドシートの代わり（スプレッドシートのままのシートのテスト用）"""
    def __init__(self):
        self.sheets = {'レース名変換表': [['変換前', '変換後'], ['ステークス', 'Ｓ']]}
        self.calls = []

    def worksheet(self, title):
        self.calls.append(('worksheet', title))
        if title not in self.sheets:
            raise gspread.WorksheetNotFound(title)
        book = self

        class WS:
            def __init__(self):
                self.title = title

            def get_all_values(self):
                return book.sheets[title]
        return WS()

    def add_worksheet(self, title, rows=1000, cols=26):
        self.calls.append(('add_worksheet', title))
        self.sheets[title] = []
        return self.worksheet(title)

    def values_batch_get(self, ranges):
        self.calls.append(('values_batch_get', tuple(ranges)))
        return {'valueRanges': [{'range': r, 'values': self.sheets[sheet_db._sheet_name_of_range(r)]} for r in ranges]}

    def list_permissions(self):
        return ['perm']


class SpreadsheetOnlyMixin:
    def _book_with_real(self):
        real = FakeRealBook()
        return self.store.book('Horse_Data', fallback=lambda: real), real

    def test_spreadsheet_only_sheets_use_real_book(self):
        book, real = self._book_with_real()
        # データベースに古いコピーがあっても、スプレッドシートの方を読む
        self.book.add_worksheet(title='レース名変換表').append_row(['古い'])
        self.assertEqual(book.worksheet('レース名変換表').get_all_values()[1], ['ステークス', 'Ｓ'])
        self.assertEqual(book.worksheet('Horses').get_all_values()[0][0], '馬名')
        book.add_worksheet(title='交流競走クラス')
        self.assertIn(('add_worksheet', '交流競走クラス'), real.calls)
        self.assertNotIn('交流競走クラス', [w.title for w in self.book.worksheets() if isinstance(w, sheet_db.DbWorksheet)])
        self.assertEqual(book.list_permissions(), ['perm'])

    def test_worksheets_lists_without_calling_google(self):
        book, real = self._book_with_real()
        titles = [w.title for w in book.worksheets()]
        self.assertIn('Horses', titles)
        self.assertIn('レース名変換表', titles)
        self.assertEqual(titles.count('レース名変換表'), 1)
        self.assertEqual(real.calls, [])

    def test_values_batch_get_mixes_db_and_real(self):
        book, real = self._book_with_real()
        res = book.values_batch_get(["'Horses'", "'レース名変換表'"])['valueRanges']
        self.assertEqual(res[0]['values'][0][0], '馬名')
        self.assertEqual(res[1]['values'][1], ['ステークス', 'Ｓ'])
        self.assertEqual(real.calls, [('values_batch_get', ("'レース名変換表'",))])

    def test_spreadsheet_only_values_are_cached(self):
        book, real = self._book_with_real()
        book.values_batch_get(["'レース名変換表'"])
        real.calls.clear()
        # 覚えている間は Google に問い合わせない（worksheet() で開くだけはする）
        res = book.values_batch_get(["'Horses'", "'レース名変換表'"])['valueRanges']
        self.assertEqual(res[1]['values'][1], ['ステークス', 'Ｓ'])
        self.assertEqual(book.worksheet('レース名変換表').get_all_values()[1], ['ステークス', 'Ｓ'])
        self.assertEqual(real.calls, [('worksheet', 'レース名変換表')])
        # 時間が過ぎたら読み込み直す
        real.sheets['レース名変換表'].append(['カップ', 'Ｃ'])
        self.store._real_values = {k: (0, v[1]) for k, v in self.store._real_values.items()}
        self.assertEqual(book.worksheet('レース名変換表').get_all_values()[-1], ['カップ', 'Ｃ'])

    def test_without_fallback_everything_is_in_db(self):
        # 移行ツールなど、本物のスプレッドシートを渡さない場合は、データベースだけを使う
        self.book.add_worksheet(title='レース名変換表').append_row(['x'])
        self.assertEqual(self.book.worksheet('レース名変換表').get_all_values(), [['x']])



class SqliteTests(SheetDbTests, SpreadsheetOnlyMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.url = 'sqlite:///' + os.path.join(cls._tmp.name, 'test.db')

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


@unittest.skipUnless(os.environ.get('SHEET_DB_TEST_URL'), 'SHEET_DB_TEST_URL が設定されていません')
class PostgresTests(SheetDbTests, SpreadsheetOnlyMixin, unittest.TestCase):
    url = os.environ.get('SHEET_DB_TEST_URL')


if __name__ == '__main__':
    unittest.main()
