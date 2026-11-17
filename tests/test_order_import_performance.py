"""Large table imports must preserve totals, atomicity and live image checks."""
import threading
import time

import pytest

from src import catalog, db, storage
from src.factory import archive, import_workspace, sales
from src.schema import ImageRecord, SourceMeta
from test_import_workspace import ARCHIVE, LINES, SUMMARY, sheet
from test_review_workflow import store  # noqa: F401


@pytest.mark.parametrize('kind,headers,rows', [
    ('sales', LINES, [['A001', '516083', '红', 'M', 10, 100, '2025-01-01', '若宇']]),
    ('archive', ARCHIVE, [['516083', '2025春', '红', 'M', '棉']]),
])
def test_preview_reads_excel_once(store, monkeypatch, kind, headers, rows):
    path = sheet(store, 'data.xlsx', headers, rows)
    original = import_workspace.load_sheet_rows
    reads = []
    def load(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(import_workspace, 'load_sheet_rows', load)
    def duplicate_read(*args, **kwargs):
        pytest.fail('Preview reread the workbook')
    monkeypatch.setattr(sales, 'load_sheet_rows', duplicate_read)
    monkeypatch.setattr(archive, 'load_sheet_rows', duplicate_read)
    preview = import_workspace.preview_table(path, path.name, kind)
    assert preview['row_count'] == 1 and preview['issue_count'] == 0
    assert reads == [path]


class TestBucket:
    __test__ = False
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.active = self.peak = 0
        self.missing = {'missing'}

    def object_exists(self, key):
        with self.lock:
            self.calls.append(key)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(.005)
            if key == 'error':
                raise OSError('Storage unavailable')
            return key not in self.missing
        finally:
            with self.lock:
                self.active -= 1


def test_bulk_presence_is_bounded_deduplicated_and_fresh(store):
    bucket = TestBucket()
    storage.set_bucket_override(bucket)
    local = store/'existing.jpg'
    local.write_bytes(b'test')
    refs = [f'oss://{i}' for i in range(40)]
    found = storage.existing_files([*refs, *refs, 'oss://missing', 'oss://error',
                                    local, store/'absent.jpg', '', None])
    assert found == set(refs) | {str(local)}
    assert len(bucket.calls) == 42
    assert 1 < bucket.peak <= 8
    bucket.missing.add('0')
    local.unlink()
    assert storage.existing_files(['oss://0', local]) == set()


def test_preview_checks_each_remote_image_once_and_detects_later_removal(store):
    bucket = TestBucket()
    storage.set_bucket_override(bucket)
    with catalog._database() as conn:
        for i in range(24):
            record = ImageRecord(image_path=f'oss://{i}', source=SourceMeta(
                source_file='test.jpg', source_type='image', location='test'),
                source_category='本厂产品', style_code=f'5160{i:02}', review_status='approved')
            conn.execute('INSERT INTO records (id,image_path,review_status,revision,payload) VALUES (?,?,?,?,?)',
                         (record.id, record.image_path, record.review_status, record.revision, record.model_dump_json()))
    path = sheet(store, 'orders.xlsx', LINES, [['A001', '516001', '红', 'M', 10, 100, '2025-01-01', '若宇']])
    preview = import_workspace.preview_table(path, path.name, 'sales')
    assert len(bucket.calls) == 24
    assert preview['missing_images'] == []
    bucket.missing.add('1')
    with pytest.raises(catalog.CatalogConflict):
        import_workspace.confirm_table(preview['token'])
    with catalog._database() as conn:
        assert conn.execute('SELECT COUNT(*) FROM style_order_lines').fetchone()[0] == 0


class CountWrites:
    def __init__(self, conn, fail_at=None):
        self.conn = conn
        self.reads = 0
        self.batches = []
        self.fail_at = fail_at

    def execute(self, sql, args=()):
        self.reads += 1
        return self.conn.execute(sql, args)

    def executemany(self, sql, values):
        self.batches.append(len(values))
        if self.fail_at == len(self.batches):
            raise RuntimeError('Simulated second batch failure')
        return self.conn.executemany(sql, values)


def test_batched_confirmation_rolls_back_and_retry_does_not_duplicate(store, monkeypatch):
    # More than two batches; rows with distinct identifiers exercise chunked reads too.
    path = sheet(store, 'large.xlsx', LINES,
                 [[f'A{i:05}', '516083', '红', 'M', i + 1, (i + 1) * 2.5, '2025-01-01', '若宇']
                  for i in range(1001)])
    preview = import_workspace.preview_table(path, path.name, 'sales')
    original = import_workspace._write_lines
    def broken(conn, rows, source):
        return original(CountWrites(conn, fail_at=2), rows, source)
    monkeypatch.setattr(import_workspace, '_write_lines', broken)
    with pytest.raises(RuntimeError, match='second batch'):
        import_workspace.confirm_table(preview['token'])
    with catalog._database() as conn:
        assert conn.execute('SELECT COUNT(*) FROM style_order_lines').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM factory_import_history').fetchone()[0] == 0
    observed = []
    def counted(conn, rows, source):
        counter = CountWrites(conn)
        result = original(counter, rows, source)
        observed.append(counter)
        return result
    monkeypatch.setattr(import_workspace, '_write_lines', counted)
    report = import_workspace.confirm_table(preview['token'])
    assert report['imported'] == 1001 and report['updated'] == 0
    assert observed[0].batches == [500, 500, 1] and observed[0].reads == 3
    assert report['impact'] == preview['impact']
    assert import_workspace.confirm_table(preview['token']) == report
    again = import_workspace.preview_table(path, path.name, 'sales')
    assert again['counts'] == {'new': 0, 'updated': 0, 'unchanged': 1001}
    assert again['impact']['counted']['delta'] == {'quantity': 0, 'amount': 0.0}
    second = import_workspace.confirm_table(again['token'])
    assert second['unchanged'] == 1001
    assert observed[1].batches == []
    assert second['stored_quantity'] == sum(range(1, 1002))


def test_batch_upsert_updates_rows_preserves_historical_metadata_and_handles_duplicates(store):
    path = sheet(store, 'summary.xlsx', SUMMARY, [['516083', '红', 'M', 10, 25]])
    rows, _ = sales.parse_sales_rows(path)
    with catalog._database() as conn:
        assert sales._write_summary(conn, rows, 'first.xlsx') == (1, 0)
        conn.execute("UPDATE style_sales SET extra_json=?", ('{"historical":{"start":"2024-01-01","end":"2024-12-31"}}',))
        changed = {**rows[0], 'quantity': 20, 'amount': 50}
        assert sales._write_summary(conn, [changed, changed], 'second.xlsx') == (0, 2)
        actual = dict(conn.execute('SELECT * FROM style_sales').fetchone())
    assert actual['quantity'] == 20 and actual['amount'] == 50
    assert actual['source_file'] == 'second.xlsx'
    assert '2024-12-31' in actual['extra_json']


@pytest.mark.parametrize('kind', ['lines', 'summary'])
def test_mysql_batch_sql_uses_driver_bulk_insert(kind, monkeypatch):
    # PyMySQL falls back to one execute per row if the INSERT syntax is not recognized.
    RE_INSERT_VALUES = pytest.importorskip('pymysql.cursors').RE_INSERT_VALUES
    monkeypatch.setattr(sales, 'on_conflict_update',
                        lambda keys, assignments: db.on_conflict_update(keys, assignments, dialect='mysql'))
    class Connection:
        def execute(self, sql, args):
            assert len(args) <= 500
            return []
        def executemany(self, sql, values):
            translated = db._qmark_to_percent(sql)
            assert RE_INSERT_VALUES.match(translated), translated
            assert 'ON DUPLICATE KEY UPDATE' in translated
            assert len(values) == 1
    row = dict(order_no='A001', variant_code='516083', style_code='516083', color='红', size_label='M',
               quantity=10, amount=25, customer='若宇', customer_raw='若宇', customer_style_code='',
               order_kind='订单', order_date='2025-01-01', salesperson='', garment_type='', season='2025春',
               year='', period='')
    writer = sales._write_lines if kind == 'lines' else sales._write_summary
    assert writer(Connection(), [row], 'test.xlsx') == (1, 0)
