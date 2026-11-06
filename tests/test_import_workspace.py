"""Import preview/confirmation, live completeness and effective sales counting."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image

from api import main as api
from src import catalog, import_jobs
from src.factory.archive import import_archive
from src.factory.import_workspace import confirm_table, history, preview_table, replan_table, workspace
from src.factory.library import load_index
from src.factory.sales import import_sales, parse_sales_rows
from test_review_workflow import make_record, store

ARCHIVE = ['大货款号', '季节', '颜色', '尺码', '原料名称']
SUMMARY = ['款号', '颜色', '尺码', '订单数量', '订单金额']
LINES = ['订单号', *SUMMARY, '订单日期', '客户']


def sheet(store, name, headers, rows):
    path = store / name
    book = Workbook(); page = book.active; page.append(headers)
    for row in rows:
        page.append(row)
    book.save(path)
    return path


def test_preview_no_business_writes_confirm_is_idempotent_and_reports_unchanged(store):
    path = sheet(store, 'archive.xlsx', ARCHIVE, [['516083若宇', '2025春', '红', 'M', '羊毛']])
    preview = preview_table(path, path.name, 'sales')
    assert preview['kind'] == 'archive' and preview['requested_kind'] == 'sales'
    assert preview['counts'] == {'new': 1, 'updated': 0, 'unchanged': 0}
    assert workspace()['counts']['styles'] == 0 and history() == []
    result = confirm_table(preview['token'])
    assert result['imported'] == 1 and result['archive_without_image'] == ['516083']
    assert confirm_table(preview['token']) == result
    again = preview_table(path, path.name, 'archive')
    assert again['counts']['unchanged'] == 1
    assert confirm_table(again['token'])['unchanged'] == 1
    assert len(history()) == 2


def test_stale_preview_cannot_overwrite_newer_archive(store):
    first = sheet(store, 'first.xlsx', ARCHIVE, [['516083', '2025春', '红', 'M', '棉']])
    preview = preview_table(first, first.name, 'archive')
    second = sheet(store, 'second.xlsx', ARCHIVE, [['516083', '2025冬', '白', 'L', '羊毛']])
    import_archive(second)
    with pytest.raises(catalog.CatalogConflict):
        confirm_table(preview['token'])
    assert load_index().variants['516083'][0].season == '2025冬'
    assert history() == []


def test_invalid_rows_require_ack_and_are_not_sheet_totals(store):
    path = sheet(store, 'bad.xlsx', SUMMARY, [['516083', '红', 'M', 10, 100], ['错误款号', '白', 'L', 2, 20], ['516084', '', 'M', 3, 30]])
    parsed, meta = parse_sales_rows(path)
    assert len(parsed) == 1 and meta['sheet_quantity'] is None
    preview = preview_table(path, path.name, 'sales')
    assert preview['issue_count'] == 2 and preview['issues'][0]['row'] == 3
    with pytest.raises(ValueError, match='核对'):
        confirm_table(preview['token'])
    result = confirm_table(preview['token'], acknowledge=True)
    assert result['stored_quantity'] == 10 and result['imported'] == 1


def test_sales_only_gap_persists_and_real_image_clears_it(store):
    path = sheet(store, 'sales.xlsx', SUMMARY, [['516083若宇', '红', 'M', 10, 100]])
    import_sales(path)
    current = workspace()
    assert current['counts']['missing_images'] == 1
    assert current['items'][0]['style_code'] == '516083' and not current['items'][0]['has_archive']
    record = make_record(store); record.source_category = '本厂产品'; record.style_code = '516083'
    catalog.save_catalog([record])
    assert workspace()['counts']['missing_images'] == 0
    assert workspace()['counts']['missing_archives'] == 1
    Path(record.image_path).unlink()
    assert workspace()['counts']['missing_images'] == 1


@pytest.mark.parametrize('orders_first', [True, False])
def test_orders_win_over_undated_summary_in_both_import_orders(store, orders_first):
    summary = sheet(store, 'summary.xlsx', SUMMARY, [['516083若宇', '红', 'M', 10, 100]])
    lines = sheet(store, 'lines.xlsx', LINES, [['A001', '516083若宇', '红', 'M', 10, 100, '2025-01-01', '若宇']])
    for path in ([lines, summary] if orders_first else [summary, lines]):
        import_sales(path)
    assert load_index().sales_totals('516083') == (10, 100)
    assert workspace()['sales_policy']['reference_rows'] == 1
    with catalog._database() as conn:
        assert conn.execute('SELECT quantity FROM style_sales').fetchone()[0] == 10
    assert import_sales(lines)['stored_quantity'] == 10


def test_historical_supplement_nonoverlap_reimport_and_future_orders(store):
    lines = sheet(store, 'lines.xlsx', LINES, [['B001', '516083', '红', 'M', 10, 100, '2025-02-01', '若宇']])
    summary = sheet(store, 'history.xlsx', SUMMARY, [['516083', '红', 'M', 5, 50]])
    import_sales(lines)
    preview = preview_table(summary, summary.name, 'sales')
    with pytest.raises(ValueError, match='重叠'):
        confirm_table(preview['token'], historical_start='2025-01-01', historical_end='2025-03-01')
    confirm_table(preview['token'], historical_start='2024-01-01', historical_end='2024-12-31')
    assert load_index().sales_totals('516083') == (15, 150)
    again = preview_table(summary, summary.name, 'sales')
    result = confirm_table(again['token'], historical_start='2024-01-01', historical_end='2024-12-31')
    assert result['unchanged'] == 1 and result['stored_quantity'] == 15
    overlap = preview_table(summary, summary.name, 'sales')
    with pytest.raises(ValueError, match='重叠'):
        confirm_table(overlap['token'], historical_start='2024-11-01', historical_end='2025-01-01')
    future = sheet(store, 'future.xlsx', LINES, [['B002', '516083', '红', 'M', 5, 50, '2024-10-01', '若宇']])
    import_sales(future)
    assert load_index().sales_totals('516083') == (15, 150)
    assert workspace()['sales_policy']['reference_rows'] == 1


def test_api_inspects_tables_and_deferred_images_without_ai(store, monkeypatch):
    monkeypatch.setattr(import_jobs, 'submit_job', import_jobs.run_job)
    monkeypatch.setattr(import_jobs, 'get_tagger', lambda _: pytest.fail('Deferred import must not call a model'))
    client = TestClient(api.app)
    path = sheet(store, 'sales.xlsx', SUMMARY, [['516083', '红', 'M', 10, 100]])
    response = client.post('/api/import/inspect', data={'category': '网图趋势'}, files={'files': ('sales.xlsx', path.read_bytes())})
    assert response.status_code == 200 and response.json()['items'][0]['kind'] == 'sales'
    data = io.BytesIO(); Image.new('RGB', (240, 320), 'red').save(data, format='PNG')
    response = client.post('/api/import/jobs?backend=deferred', data={'source_categories': '本厂产品'}, files={'files': ('516083.png', data.getvalue())})
    assert response.status_code == 202, response.text
    job = client.get('/api/import/jobs/' + response.json()['id']).json()
    assert job['succeeded'] == 1 and job['filtered'] == 0
    record = catalog.load_catalog()[0]
    assert record.tags_source == 'deferred' and record.ai_tags is None
    assert catalog.library_records([record]) == [record]
    assert import_jobs.create_retag('mock', False, batch_id=job['id'])['items']
    assert not import_jobs.create_retag('mock', False, batch_id='other-batch')['items']
    response = client.post('/api/import/inspect', data={'category': '本厂产品'}, files={'files': ('516083.png', data.getvalue())})
    assert response.json()['items'][0]['action'] == 'unchanged'
    response = client.get('/api/import/workspace?status=all')
    assert response.status_code == 200 and response.json()['counts']['styles'] == 1


def test_historical_repreview_counts_and_confirms_the_selected_range(store):
    path = sheet(store, 'historical.xlsx', SUMMARY, [['516083', '红', 'M', 5, 50]])
    import_sales(path)
    initial = preview_table(path, path.name, 'sales')
    assert initial['counts']['unchanged'] == 1
    scoped = replan_table(initial['token'], '2024-01-01', '2024-12-31')
    assert scoped['counts']['new'] == 1
    with pytest.raises(ValueError, match='范围与预览'):
        confirm_table(scoped['token'])
    result = confirm_table(scoped['token'], historical_start='2024-01-01', historical_end='2024-12-31')
    assert result['imported'] == 1 and result['stored_quantity'] == 5
    assert workspace()['sales_policy']['reference_rows'] == 1


def test_corrupt_excel_preview_returns_actionable_error(store):
    response = TestClient(api.app).post('/api/import/table-preview', data={'kind': 'archive'}, files={'file': ('bad.xlsx', b'not a workbook')})
    assert response.status_code == 422
    assert '重新导出' in response.json()['detail']
    assert workspace()['counts']['styles'] == 0
