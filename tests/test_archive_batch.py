"""Archive batches preserve existing fields and roll back as one import."""
import json

import pytest

from src import catalog, db
from src.factory import archive, import_workspace
from test_import_workspace import sheet
from test_review_workflow import store  # noqa: F401


HEADERS = ['季节', '大货款号', '设计款号', '颜色', '尺码', '针型', '一级品类',
           '设计师', '工艺师', '原料名称', '二级品类']


def item(number):
    code = f'{600000 + number}客户甲'
    return dict(variant_code=code, style_code=str(600000 + number), customer='客户甲',
                season='2023秋', needle='14针', category='上衣', material="羊毛50%/棉'50%",
                design_code=f'W{number}', designer='设计甲', craftsman='工艺乙', subcategory='开衫',
                colors=[{'name': '粉色', 'family': '粉'}], sizes=[{'label': 'M'}])


class BatchConnection:
    def __init__(self, conn, fail_at=None):
        self.conn = conn
        self.reads = []
        self.batches = []
        self.fail_at = fail_at

    def execute(self, sql, args):
        assert 'WHERE variant_code IN (' in sql
        assert 0 < len(args) <= 500
        self.reads.append(len(args))
        return self.conn.execute(sql, args)

    def executemany(self, sql, values):
        self.batches.append(len(values))
        result = self.conn.executemany(sql, values)
        if len(self.batches) == self.fail_at:
            raise RuntimeError('Failure after second batch writes')
        return result


def test_archive_batches_preserve_unrelated_data_and_duplicates_across_batches(store):
    rows = [item(i) for i in range(1001)]
    with catalog._database() as conn:
        archive.write_archive(conn, [item(0), item(5000)], 'original.xlsx')
        conn.execute("UPDATE style_variants SET series='原有系列', designer='原设计' WHERE variant_code=?",
                     (rows[0]['variant_code'],))
        untouched = dict(conn.execute('SELECT * FROM style_variants WHERE variant_code=?',
                                      (item(5000)['variant_code'],)).fetchone())
        # The same key in a later batch must update, not count as a second new style.
        rows.append({**item(1), 'designer': '最后一行设计师', 'subcategory': ''})
        counter = BatchConnection(conn)
        assert archive.write_archive(counter, rows, 'batch.xlsx') == (1000, 2)
        assert counter.reads == [500, 500, 1]
        assert counter.batches == [500, 500, 2]
        stored = {r['variant_code']: dict(r) for r in conn.execute('SELECT * FROM style_variants')}
        assert len(stored) == 1002
        assert stored[item(5000)['variant_code']] == untouched
        assert stored[item(0)['variant_code']]['series'] == '原有系列'
        for row in rows[:1001]:
            expected = rows[-1] if row['variant_code'] == item(1)['variant_code'] else row
            actual = stored[row['variant_code']]
            for key, value in import_workspace._row_data('archive', expected).items():
                assert actual[key] == value
            assert actual['source_file'] == 'batch.xlsx'
            if row['variant_code'] != item(0)['variant_code']:
                assert actual['series'] == ''
        assert len({stored[r['variant_code']]['updated_at'] for r in rows}) == 1


def test_archive_confirm_failure_rolls_back_updates_inserts_and_history_then_retries(store, monkeypatch):
    with catalog._database() as conn:
        archive.write_archive(conn, [item(0)], 'existing.xlsx')
        conn.execute("UPDATE style_variants SET series='保留系列'")
        original_row = dict(conn.execute('SELECT * FROM style_variants').fetchone())
        conn.execute('INSERT INTO style_order_lines '
                     '(order_no,variant_code,color,size_label,quantity,amount,style_code,updated_at) '
                     'VALUES (?,?,?,?,?,?,?,?)', ('KEEP', '600000客户甲', '红', 'M', 7, 350, '600000', 'before'))
        original_order = dict(conn.execute('SELECT * FROM style_order_lines').fetchone())
    path = sheet(store, '商品资料表.xlsx', HEADERS,
                 [['2023冬', f'{600000 + i}客户甲', f'D{i}', '粉色,黑色', 'M,L', '12针', '上衣',
                   '新设计', '新工艺', '棉', '开衫'] for i in range(1001)])
    preview = import_workspace.preview_table(path, path.name, 'archive')
    assert preview['counts'] == {'new': 1000, 'updated': 1, 'unchanged': 0}
    writer = import_workspace.write_archive

    def broken(conn, rows, source):
        return writer(BatchConnection(conn, fail_at=2), rows, source)

    monkeypatch.setattr(import_workspace, 'write_archive', broken)
    with pytest.raises(RuntimeError, match='second batch'):
        import_workspace.confirm_table(preview['token'])
    with catalog._database() as conn:
        assert [dict(r) for r in conn.execute('SELECT * FROM style_variants')] == [original_row]
        assert conn.execute('SELECT COUNT(*) FROM factory_import_history').fetchone()[0] == 0
        assert dict(conn.execute('SELECT * FROM style_order_lines').fetchone()) == original_order
    monkeypatch.setattr(import_workspace, 'write_archive', writer)
    report = import_workspace.confirm_table(preview['token'])
    assert (report['imported'], report['updated'], report['unchanged']) == (1000, 1, 0)
    assert import_workspace.confirm_table(preview['token']) == report
    repeated = import_workspace.preview_table(path, path.name, 'archive')
    assert repeated['counts'] == {'new': 0, 'updated': 0, 'unchanged': 1001}
    assert import_workspace.confirm_table(repeated['token'])['unchanged'] == 1001
    with catalog._database() as conn:
        assert conn.execute('SELECT COUNT(*) FROM style_variants').fetchone()[0] == 1001
        current = dict(conn.execute('SELECT * FROM style_variants WHERE variant_code=?',
                                    (item(0)['variant_code'],)).fetchone())
        assert current['series'] == '保留系列' and current['designer'] == '新设计'
        assert [c['name'] for c in json.loads(current['colors_json'])] == ['粉色', '黑色']
        assert dict(conn.execute('SELECT * FROM style_order_lines').fetchone()) == original_order


def test_empty_archive_does_not_access_database():
    assert archive.write_archive(object(), [], 'empty.xlsx') == (0, 0)


def test_archive_sql_is_actually_combined_by_mysql_driver(monkeypatch):
    pymysql = pytest.importorskip('pymysql')
    monkeypatch.setattr(archive, 'on_conflict_update',
                        lambda keys, columns: db.on_conflict_update(keys, columns, dialect='mysql'))
    commands = []
    connection = pymysql.connections.Connection(defer_connect=True, charset='utf8mb4')
    connection.server_status = 0

    class RecordingCursor(pymysql.cursors.Cursor):
        def execute(self, sql, args=None):
            # A per-row driver fallback would supply args here.
            assert args is None
            commands.append(bytes(sql) if not isinstance(sql, str) else sql.encode('utf8'))
            return 1

    cursor = RecordingCursor(connection)

    class Connection:
        def execute(self, sql, args):
            assert 'WHERE variant_code IN (' in sql and len(args) <= 500
            return []

        def executemany(self, sql, values):
            cursor.executemany(db._qmark_to_percent(sql), values)

    assert archive.write_archive(Connection(), [item(i) for i in range(1001)], '商品资料.xlsx') == (1001, 0)
    assert len(commands) == 3
    assert [query.count(b'),(') + 1 for query in commands] == [500, 500, 1]
    assert all(b'ON DUPLICATE KEY UPDATE' in query for query in commands)
    assert all(b'series=VALUES(series)' not in query for query in commands)
