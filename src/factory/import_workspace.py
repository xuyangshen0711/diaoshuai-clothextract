"""Persistent completeness and staged, optimistic, idempotent table imports."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from src import catalog, storage
from src.factory.archive import parse_archive_rows, write_archive
from src.factory.codes import parse_style_code
from src.factory.excel_io import (cell_at, column_index, find_header, load_sheet_rows, parse_number,
                                 plain_text, sheet_headers, table_kind_from_headers)
from src.factory.sales import _stored_totals, _write_lines, _write_summary, is_total_row, parse_sales_rows
from src.factory.sales_policy import historical_range, policy_summary
from src.factory.tables import gap_report, now, own_factory_images


def completeness(conn) -> list[dict]:
    archives = {r[0] for r in conn.execute("SELECT DISTINCT style_code FROM style_variants")}
    sales = {r[0] for table in ("style_sales", "style_order_lines")
             for r in conn.execute(f"SELECT DISTINCT style_code FROM {table}")}
    images = own_factory_images(conn)
    result = []
    for code in sorted(archives | sales | set(images)):
        record = images.get(code)
        has_image = bool(record and storage.is_file(record.image_path))
        missing = []
        if not has_image:
            missing.append("image")
        if code not in archives:
            missing.append("archive")
        result.append({"style_code": code, "has_image": has_image, "has_archive": code in archives,
                       "has_sales": code in sales, "record_id": record.id if has_image else None,
                       "pending_review": bool(has_image and record.review_status == "pending"), "missing": missing})
    return result


def workspace(status="missing", query="", page=1, page_size=20) -> dict:
    with catalog._database() as conn:
        items = completeness(conn)
        policy = policy_summary(conn)
    counts = {"styles": len(items), "missing_images": sum(not r["has_image"] for r in items),
              "missing_archives": sum(not r["has_archive"] for r in items),
              "no_sales": sum(not r["has_sales"] for r in items),
              "pending_review": sum(r["pending_review"] for r in items)}
    filtered = [r for r in items if query.upper().strip() in r["style_code"].upper() and (
        status == "all" or (status == "missing" and r["missing"]) or
        (status == "image" and not r["has_image"]) or
        (status == "archive" and not r["has_archive"]) or (status == "no_sales" and not r["has_sales"]))]
    offset = (page - 1) * page_size
    return {"counts": counts, "items": filtered[offset:offset + page_size], "total": len(filtered),
            "page": page, "page_size": page_size, "sales_policy": policy}


def _fingerprint(conn) -> str:
    state = {}
    for table in ("style_variants", "style_sales", "style_order_lines"):
        state[table] = sorted([dict(r) for r in conn.execute(f"SELECT * FROM {table}")],
                              key=lambda r: json.dumps(r, sort_keys=True, default=str))
    state["images"] = [(r["style_code"], r["has_image"], r["record_id"]) for r in completeness(conn)]
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _row_data(kind, item):
    if kind == "archive":
        return {**{k: v for k, v in item.items() if k not in {"colors", "sizes"}},
                "colors_json": json.dumps(item["colors"], ensure_ascii=False),
                "sizes_json": json.dumps(item["sizes"], ensure_ascii=False)}
    return {k: v for k, v in item.items() if k != "extra_json"}


def _plan(conn, kind, rows, fmt):
    table = "style_variants" if kind == "archive" else "style_order_lines" if fmt == "sales_lines" else "style_sales"
    keys = ["variant_code"] if kind == "archive" else (
        ["order_no", "variant_code", "color", "size_label"] if fmt == "sales_lines" else
        ["variant_code", "color", "size_label", "year", "period"])
    existing = {tuple(r[k] for k in keys): dict(r) for r in conn.execute(f"SELECT * FROM {table}")}
    planned = []
    for item in rows:
        previous = existing.get(tuple(item[k] for k in keys))
        data = _row_data(kind, item)
        equal = previous is not None and all(previous.get(k) == v for k, v in data.items())
        planned.append({"row": item, "action": "unchanged" if equal else "updated" if previous else "new"})
    return planned


def _issues(path, kind, fmt) -> tuple[list, int]:
    rows = load_sheet_rows(path)
    header, headers = find_header(rows, "大货款号" if kind == "archive" else "款号")
    idx = {name: column_index(headers, name) for name in headers if name}
    issues = []
    ignored = 0
    for number, row in enumerate(rows[header + 1:], header + 2):
        text = " ".join(str(c or "") for c in row)
        code = parse_style_code(cell_at(row, idx.get("大货款号" if kind == "archive" else "款号")))
        if not text.strip() or (code is None and (is_total_row(row, idx) if kind == "sales" else any(w in text for w in ("合计", "总计")))):
            ignored += 1
            continue
        message = ""
        if code is None:
            message = "款号为空或格式无效"
        elif kind == "sales":
            missing = [name for name in ("颜色", "尺码") if not plain_text(cell_at(row, idx.get(name)))]
            missing += [name for name in ("订单数量", "订单金额") if parse_number(cell_at(row, idx.get(name))) is None]
            if fmt == "sales_lines" and not plain_text(cell_at(row, idx.get("订单号", idx.get("订单")))):
                missing.append("订单号")
            if missing:
                message = "缺少或无法识别：" + "、".join(missing)
        if message:
            issues.append({"row": number, "style_code": code.raw if code else plain_text(cell_at(row, idx.get("款号", idx.get("大货款号")))), "message": message})
    return issues, ignored


def _folder() -> Path:
    path = catalog.PROCESSED_DIR / "table-previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def preview_table(path: Path, filename: str, requested_kind: str) -> dict:
    kind = table_kind_from_headers(sheet_headers(load_sheet_rows(path)))
    if kind not in {"archive", "sales"}:
        raise ValueError("未识别到款式档案或销量表，请检查表头。")
    if kind == "archive":
        rows, skipped, warnings, recognized = parse_archive_rows(path)
        meta = {**recognized, "skipped_rows": skipped, "warnings": warnings}
    else:
        rows, meta = parse_sales_rows(path)
    issues, ignored = _issues(path, kind, meta["format"])
    token = uuid4().hex
    with catalog._database() as conn:
        plan = _plan(conn, kind, rows, meta["format"])
        state = _fingerprint(conn)
        codes = sorted({r["style_code"] for r in rows})
        gaps = [r for r in completeness(conn) if r["style_code"] in codes]
        ordered = {r[0] for r in conn.execute("SELECT DISTINCT variant_code FROM style_order_lines")}
        summarized = {r[0] for r in conn.execute("SELECT DISTINCT variant_code FROM style_sales")}
    collisions = sorted({r["variant_code"] for r in rows} & (summarized if meta["format"] == "sales_lines" else ordered)) if kind == "sales" else []
    response = {"token": token, "kind": kind, "requested_kind": requested_kind, "source_file": filename,
                "format": meta["format"], "format_label": meta["format_label"], "meta": meta,
                "row_count": len(rows), "style_codes": codes,
                "counts": {action: sum(p["action"] == action for p in plan) for action in ("new", "updated", "unchanged")},
                "issues": issues[:100], "issue_count": len(issues), "ignored_rows": ignored,
                "samples": [{**p["row"], "action": p["action"]} for p in plan[:8]],
                "overlap_variants": collisions,
                "missing_images": sorted(set(codes) - {r["style_code"] for r in gaps if r["has_image"]}),
                "can_confirm": bool(rows)}
    manifest = {"response": response, "rows": rows, "base_rows": rows, "fingerprint": state, "created_at": now()}
    (_folder() / f"{token}.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return response


def confirm_table(token: str, acknowledge=False, historical_start="", historical_end="") -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("预览编号无效，请重新选择文件。")
    with catalog._database() as conn:
        previous = conn.execute("SELECT payload FROM factory_import_history WHERE id=?", (token,)).fetchone()
        if previous:
            return json.loads(previous[0])
        path = _folder() / f"{token}.json"
        if not path.is_file():
            raise ValueError("预览已失效，请重新选择文件。")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(manifest["created_at"])
        if age.total_seconds() > 86400:
            raise ValueError("预览已超过 24 小时，请重新选择文件。")
        if manifest["fingerprint"] != _fingerprint(conn):
            raise catalog.CatalogConflict("资料在预览后有更新，请重新预览再导入。")
        preview = manifest["response"]
        if "historical_start" in preview and (preview["historical_start"], preview["historical_end"]) != (historical_start, historical_end):
            raise ValueError("日期范围与预览不一致，请重新预览范围。")
        if not preview["can_confirm"]:
            raise ValueError("没有可导入的有效行，请修正表格后重试。")
        if (preview["issue_count"] or preview["meta"].get("reconciled") is False) and not acknowledge:
            raise ValueError("请先核对异常行或合计差异，再确认只导入有效行。")
        rows = manifest["rows"]
        fmt, kind = preview["format"], preview["kind"]
        if historical_start or historical_end:
            if kind != "sales" or fmt == "sales_lines":
                raise ValueError("仅销量汇总可作为历史补充。")
            rows = _historical_rows(conn, rows, historical_start, historical_end)
        plan = _plan(conn, kind, rows, fmt)
        changed = [p["row"] for p in plan if p["action"] != "unchanged"]
        writer = write_archive if kind == "archive" else _write_lines if fmt == "sales_lines" else _write_summary
        imported, updated = writer(conn, changed, preview["source_file"])
        if historical_start:
            extra = json.dumps({"historical": {"start": historical_start, "end": historical_end}})
            for r in rows:
                conn.execute("UPDATE style_sales SET extra_json=? WHERE variant_code=? AND color=? AND size_label=? AND `year`=? AND period=?",
                             (extra, r["variant_code"], r["color"], r["size_label"], r["year"], r["period"]))
        quantity, amount = _stored_totals(conn, preview["style_codes"])
        report = {**preview["meta"], "id": token, "kind": kind, "source_file": preview["source_file"],
                  "created_at": now(), "imported": imported, "updated": updated,
                  "unchanged": len(rows) - len(changed), "row_count": len(rows),
                  "style_codes": preview["style_codes"], "issue_count": preview["issue_count"],
                  "stored_quantity": quantity, "stored_amount": round(amount, 2), **gap_report(conn),
                  "sales_policy": policy_summary(conn)}
        conn.execute("INSERT INTO factory_import_history (id, payload, created_at) VALUES (?, ?, ?)",
                     (token, json.dumps(report, ensure_ascii=False), report["created_at"]))
    return report


def history() -> list[dict]:
    with catalog._database() as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM factory_import_history ORDER BY created_at DESC LIMIT 30")]


def _historical_rows(conn, rows, historical_start, historical_end):
    try:
        start, end = date.fromisoformat(historical_start), date.fromisoformat(historical_end)
        if start > end:
            raise ValueError()
    except ValueError:
        raise ValueError("请填写有效的历史起止日期，开始日期不能晚于结束日期。")
    variants = {r["variant_code"] for r in rows}
    for order in conn.execute("SELECT variant_code, order_date FROM style_order_lines"):
        if order["variant_code"] in variants and (not order["order_date"] or historical_start <= order["order_date"][:10] <= historical_end):
            raise ValueError("历史范围与已有订单重叠，或订单缺少日期。请缩小范围或仅保留汇总用于核对。")
    for sale in conn.execute("SELECT * FROM style_sales"):
        scope = historical_range(sale)
        if sale["variant_code"] in variants and scope and scope != (historical_start, historical_end) and scope[0] <= historical_end and historical_start <= scope[1]:
            raise ValueError("历史范围与已导入的历史补充重叠，请核对后重试。")
    rows = [{**r, "year": str(start.year), "period": f"{historical_start}:{historical_end}"} for r in rows]
    return rows


def replan_table(token: str, historical_start="", historical_end="") -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("预览编号无效，请重新选择文件。")
    path = _folder() / f"{token}.json"
    if not path.is_file():
        raise ValueError("请重新选择文件。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    preview = manifest["response"]
    if preview["kind"] != "sales" or preview["format"] == "sales_lines":
        raise ValueError("仅销量汇总可调整历史范围。")
    with catalog._database() as conn:
        if manifest["fingerprint"] != _fingerprint(conn):
            raise catalog.CatalogConflict("资料在预览后有更新，请重新选择文件。")
        rows = manifest.get("base_rows", manifest["rows"])
        if historical_start or historical_end:
            rows = _historical_rows(conn, rows, historical_start, historical_end)
        plan = _plan(conn, "sales", rows, preview["format"])
    new_token = uuid4().hex
    preview.update(token=new_token, historical_start=historical_start, historical_end=historical_end,
                   counts={action: sum(p["action"] == action for p in plan) for action in ("new", "updated", "unchanged")},
                   samples=[{**p["row"], "action": p["action"]} for p in plan[:8]])
    manifest["rows"] = rows
    (_folder() / f"{new_token}.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return preview
