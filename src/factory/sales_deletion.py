"""Preview and delete sales rows without touching images or style archives."""
from __future__ import annotations

import hashlib
import json
import re
from threading import RLock
from uuid import uuid4

from src import catalog
from src.db import is_mysql, json_extract
from src.factory.sales_policy import counted_from_rows
from src.factory.tables import _column_names, now

TABLES = {"orders": "style_order_lines", "summary": "style_sales"}
KEYS = {"orders": ("order_no", "variant_code", "color", "size_label"),
        "summary": ("variant_code", "color", "size_label", "year", "period")}
# Like the import job worker, the API runs in one process. Lock before opening
# the transaction so an import cannot validate a preview then race a deletion.
mutation_lock = RLock()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def row_id(kind, row):
    return _hash([kind, [row[key] for key in KEYS[kind]]])


def read_rows(conn, *, lock=False):
    suffix = " FOR UPDATE" if lock and is_mysql(conn) else ""
    return {row_id(kind, row): {"kind": kind, "row": dict(row)}
            for kind, table in TABLES.items() for row in conn.execute(f"SELECT * FROM {table}" + suffix)}


def capture_batch(before, after, kind, items):
    allowed = {row_id(kind, item) for item in items}
    return {key: _hash(value) for key, value in after.items() if key in allowed and before.get(key) != value}


def public_report(report):
    return {key: value for key, value in report.items() if not key.startswith("_")}


def _later_references(conn, report, rows):
    protected = set()
    for found in conn.execute("SELECT payload FROM factory_import_history WHERE created_at > ?", (report["created_at"],)):
        later = json.loads(found[0])
        if later.get("kind") != "sales":
            continue
        if "_sales_references" in later:
            protected.update(later["_sales_references"])
            continue
        path = catalog.PROCESSED_DIR / "table-previews" / f"{later['id']}.json"
        if path.is_file():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            kind = "orders" if later.get("format") == "sales_lines" else "summary"
            for row in manifest["rows"]:
                protected.add(row_id(kind, row))
        else:
            # Missing old manifests cannot prove that overlapping styles are independent.
            protected.update(key for key, value in rows.items()
                             if value["row"]["style_code"] in later.get("style_codes", []))
    return protected


def _batch_scope(conn, batch_id, rows):
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise ValueError("导入批次编号无效。")
    result = conn.execute("SELECT payload FROM factory_import_history WHERE id=?", (batch_id,)).fetchone()
    if not result:
        raise KeyError(batch_id)
    report = json.loads(result[0])
    if report.get("kind") != "sales":
        raise ValueError("此入口仅管理销量和订单，不删除款式档案。")
    referenced = _later_references(conn, report, rows)
    if "_sales_rows" in report:
        expected = report["_sales_rows"]
        ids = {key for key, fingerprint in expected.items()
               if key in rows and key not in referenced and _hash(rows[key]) == fingerprint}
        return ids, {"name": report["source_file"], "protected": len(expected) - len(ids), "legacy": False}
    # Old confirmations retained the parsed input, but not overwritten versions.
    # Require keys, values, source name AND the import time window to agree.
    path = catalog.PROCESSED_DIR / "table-previews" / f"{batch_id}.json"
    if not path.is_file():
        raise ValueError("这批旧记录缺少关联明细，无法安全按批次删除。请使用「全部销量」按订单或款号核对后选择。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    kind = "orders" if report.get("format") == "sales_lines" else "summary"
    expected = manifest["rows"]
    preview = manifest.get("response", {})
    if kind == "summary" and preview.get("historical_start"):
        start, end = preview["historical_start"], preview["historical_end"]
        expected = [{**row, "year": start[:4], "period": f"{start}:{end}"} for row in expected]
    ids = set()
    for item in expected:
        key = row_id(kind, item)
        if key not in rows or key in referenced:
            continue
        current = rows[key]["row"]
        if (current.get("source_file") == report["source_file"]
                and manifest["created_at"] <= current.get("updated_at", "") <= report["created_at"]
                and all(current.get(k) == value for k, value in item.items() if k != "extra_json")):
            ids.add(key)
    changed = report.get("imported", 0) + report.get("updated", 0)
    # No-op reimports do not own the older rows they happen to contain.
    if not changed:
        ids.clear()
    return ids, {"name": report["source_file"], "protected": max(0, changed - len(ids)), "legacy": True}


def list_rows(*, batch_id="", q="", page=1, page_size=50):
    with catalog.read_database() as conn:
        rows = read_rows(conn)
        ids, batch = _batch_scope(conn, batch_id, rows) if batch_id else (set(rows), None)
    query = q.strip().casefold()
    items = []
    for key in ids:
        entry = rows[key]
        row = entry["row"]
        if query and query not in " ".join(str(row.get(field, "")) for field in
                                          ("order_no", "style_code", "variant_code", "customer", "source_file")).casefold():
            continue
        items.append({"id": key, "kind": entry["kind"], **{field: row.get(field, "") for field in
                      ("order_no", "variant_code", "style_code", "customer", "color", "size_label", "quantity",
                       "amount", "source_file", "order_date", "year", "period", "updated_at")}})
    items.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
    offset = (page - 1) * page_size
    return {"items": items[offset:offset + page_size], "total": len(items), "page": page,
            "page_size": page_size, "batch": batch}


def _selection(conn, rows, scope, batch_id, ids):
    if scope == "all":
        if batch_id or ids:
            raise ValueError("清空全部销量不能同时指定批次或所选记录，请重新预览。")
        if not rows:
            raise ValueError("当前没有销量或订单明细，无需清空。")
        selected = set(rows)
        batch = {"name": "全部销量与订单明细", "protected": 0, "legacy": False}
    elif scope == "batch":
        if not batch_id:
            raise ValueError("请选择一个销量导入批次。")
        selected, batch = _batch_scope(conn, batch_id, rows)
    elif scope == "selected":
        selected, batch = set(ids), None
        if not selected.issubset(rows):
            raise catalog.CatalogConflict("所选数据已变化，请刷新后重新选择。")
        if batch_id:
            allowed, batch = _batch_scope(conn, batch_id, rows)
            if not selected.issubset(allowed):
                raise catalog.CatalogConflict("所选记录已不属于该批次，请刷新后重新选择。")
    else:
        raise ValueError("删除范围无效。")
    if not selected:
        raise ValueError("没有可删除的数据；本批可能未修改数据，或已被后续导入更新。")
    return selected, batch


def _totals(rows, codes):
    orders = [value["row"] for value in rows.values() if value["kind"] == "orders"]
    summaries = [value["row"] for value in rows.values() if value["kind"] == "summary"]
    counted = counted_from_rows(summaries, orders)
    effective = [row for row in [*orders, *counted] if row["style_code"] in codes]
    return {"quantity": sum(int(row["quantity"]) for row in effective),
            "amount": round(sum(float(row["amount"]) for row in effective), 2)}, {row_id("summary", row) for row in counted}


def _plan(conn, rows, scope, batch_id, ids):
    selected, batch = _selection(conn, rows, scope, batch_id, ids)
    codes = {rows[key]["row"]["style_code"] for key in selected}
    before, counted_before = _totals(rows, codes)
    after, counted_after = _totals({key: value for key, value in rows.items() if key not in selected}, codes)
    return {"token": _hash([rows, scope, batch_id, sorted(selected)]), "row_count": len(selected),
            "order_rows": sum(rows[key]["kind"] == "orders" for key in selected),
            "summary_rows": sum(rows[key]["kind"] == "summary" for key in selected),
            "style_codes": sorted(codes), "before": before, "after": after,
            "reactivated_summary_rows": len(counted_after - counted_before), "batch": batch}, selected


def preview(*, scope="selected", batch_id="", ids=()):
    with catalog.read_database() as conn:
        plan, _ = _plan(conn, read_rows(conn), scope, batch_id, ids)
    return plan


def delete(*, scope="selected", batch_id="", ids=(), token):
    with mutation_lock, catalog._database() as conn:
        rows = read_rows(conn, lock=True)
        plan, selected = _plan(conn, rows, scope, batch_id, ids)
        if token != plan["token"]:
            raise catalog.CatalogConflict("预览后销量数据有更新，请重新预览再确认删除。")
        audit = {"id": uuid4().hex, "created_at": now(), "scope": scope, "batch_id": batch_id,
                 "preview": plan, "rows": {key: rows[key] for key in sorted(selected)}}
        # The complete deleted rows and the deletion share one transaction.
        conn.execute("INSERT INTO sales_deletions (id, payload, created_at) VALUES (?, ?, ?)",
                     (audit["id"], _json(audit), audit["created_at"]))
        if scope == "all":
            # Delete only the exact backed-up natural keys, in bounded chunks.
            # This avoids thousands of round trips and never includes unseen rows.
            for kind, table in TABLES.items():
                records = [rows[key]["row"] for key in sorted(selected) if rows[key]["kind"] == kind]
                fields = KEYS[kind]
                names = ", ".join(f"`{field}`" for field in fields)
                placeholders = "(" + ", ".join("?" for _ in fields) + ")"
                for offset in range(0, len(records), 100):
                    chunk = records[offset:offset + 100]
                    params = tuple(row[field] for row in chunk for field in fields)
                    conn.execute(f"DELETE FROM {table} WHERE ({names}) IN ("
                                 + ", ".join(placeholders for _ in chunk) + ")", params)
        else:
            for key in sorted(selected):
                entry = rows[key]
                kind, row = entry["kind"], entry["row"]
                where = " AND ".join(f"`{field}`=?" for field in KEYS[kind])
                conn.execute(f"DELETE FROM {TABLES[kind]} WHERE {where}", tuple(row[field] for field in KEYS[kind]))
    from src.factory.library import invalidate_factory_snapshot

    invalidate_factory_snapshot()
    return {"deleted": len(selected), "backup_id": audit["id"], "before": plan["before"], "after": plan["after"]}


def deletions(*, page=1):
    with catalog.read_database() as conn:
        records = conn.execute(
            "SELECT id, created_at, " + json_extract("payload", "$.preview.row_count") + " AS row_count, "
            + json_extract("payload", "$.preview.batch.name") + " AS source_file, "
            + json_extract("payload", "$.restoration.created_at") + " AS restored_at, "
            + json_extract("payload", "$.restoration.restored") + " AS restored_rows, "
            + json_extract("payload", "$.restoration.skipped_existing") + " AS skipped_rows "
            "FROM sales_deletions ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,))
        return [{"id": row["id"], "created_at": row["created_at"], "row_count": int(row["row_count"]),
                 "source_file": row["source_file"] or "按所选记录删除",
                 "restored_at": row["restored_at"], "restored_rows": int(row["restored_rows"] or 0),
                 "skipped_rows": int(row["skipped_rows"] or 0)} for row in records]


def backup(identifier):
    with catalog.read_database() as conn:
        row = conn.execute("SELECT payload FROM sales_deletions WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return json.loads(row[0])


def _saved(conn, identifier, *, lock=False):
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise KeyError(identifier)
    suffix = " FOR UPDATE" if lock and is_mysql(conn) else ""
    row = conn.execute("SELECT payload FROM sales_deletions WHERE id=?" + suffix, (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return json.loads(row[0])


def _restore_existing(conn, saved, rows):
    existing = {key: rows[key] for key in saved.keys() & rows.keys()}
    if not is_mysql(conn):
        return existing
    # MySQL natural keys use a case/accent-insensitive collation. Match with
    # the database's equality rules as well as our exact Python row IDs.
    for kind, table in TABLES.items():
        candidates = [(key, entry) for key, entry in saved.items() if key not in existing and entry["kind"] == kind]
        where = " AND ".join(f"`{field}`=?" for field in KEYS[kind])
        for offset in range(0, len(candidates), 100):
            chunk = candidates[offset:offset + 100]
            query = " UNION ALL ".join(f"SELECT ? AS backup_key, t.* FROM {table} t WHERE {where}" for _ in chunk)
            params = tuple(value for key, entry in chunk for value in (key, *(entry["row"][field] for field in KEYS[kind])))
            for found in conn.execute(query, params):
                row = dict(found)
                existing[row.pop("backup_key")] = {"kind": kind, "row": row}
    return existing


def _restore_plan(conn, audit, rows):
    if audit.get("restoration"):
        raise catalog.CatalogConflict("这份备份已处理恢复，请刷新删除记录；再次误删时请使用最新的删除备份。")
    saved = audit["rows"]
    existing = _restore_existing(conn, saved, rows)
    missing = {key: entry for key, entry in saved.items() if key not in existing}
    identical = sum(saved[key] == existing[key] for key in existing)
    conflicts = sorted(key for key in existing if saved[key] != existing[key])
    codes = {entry["row"]["style_code"] for entry in saved.values()}
    before, counted_before = _totals(rows, codes)
    after, counted_after = _totals({**rows, **missing}, codes)
    return {"token": _hash(["restore", audit, rows]), "backup_id": audit["id"],
            "row_count": len(saved), "restore_count": len(missing), "skipped_existing": len(existing),
            "identical_rows": identical, "conflicting_rows": len(conflicts),
            "conflicts": [{field: saved[key]["row"].get(field, "") for field in
                           ("order_no", "variant_code", "color", "size_label")} for key in conflicts[:10]],
            "before": before, "after": after, "style_codes": sorted(codes),
            "suppressed_summary_rows": len(counted_before - counted_after)}, missing


def restore_preview(identifier):
    with catalog.read_database() as conn:
        plan, _ = _restore_plan(conn, _saved(conn, identifier), read_rows(conn))
    return plan


def restore(identifier, token):
    with mutation_lock, catalog._database() as conn:
        audit = _saved(conn, identifier, lock=True)
        if audit.get("restoration"):
            return {**audit["restoration"], "already_restored": True}
        rows = read_rows(conn, lock=True)
        plan, missing = _restore_plan(conn, audit, rows)
        if token != plan["token"]:
            raise catalog.CatalogConflict("预览后销量数据有更新，请重新预览再恢复。")
        if not missing:
            raise ValueError("备份中的订单或汇总已存在，无需恢复；现有数据不会被覆盖。")
        columns = {kind: _column_names(conn, table) for kind, table in TABLES.items()}
        for key, entry in sorted(missing.items()):
            kind, row = entry["kind"], entry["row"]
            if row_id(kind, row) != key or not set(row).issubset(columns[kind]):
                raise ValueError("备份与当前数据结构不一致，请联系管理员核对。")
            fields = list(row)
            names = ", ".join(f"`{field}`" for field in fields)
            placeholders = ", ".join("?" for _ in fields)
            # Plain INSERT: never replace or update an existing business key.
            conn.execute(f"INSERT INTO {TABLES[kind]} ({names}) VALUES ({placeholders})",
                         tuple(row[field] for field in fields))
        result = {"backup_id": identifier, "created_at": now(), "restored": len(missing),
                  "skipped_existing": plan["skipped_existing"], "conflicting_rows": plan["conflicting_rows"],
                  "before": plan["before"], "after": plan["after"]}
        audit["restoration"] = result
        # Rows and the single-use restoration receipt commit or roll back together.
        conn.execute("UPDATE sales_deletions SET payload=? WHERE id=?", (_json(audit), identifier))
    from src.factory.library import invalidate_factory_snapshot

    invalidate_factory_snapshot()
    return {**result, "already_restored": False}
