"""Choose counted sales without deleting the original summary or order data.

An undated summary cannot prove that it does not overlap orders. Orders therefore
take precedence per customer variant. Explicit, disjoint historical supplements
can still count alongside dated orders.
"""
from __future__ import annotations

import json


def historical_range(row) -> tuple[str, str] | None:
    extra = json.loads(row["extra_json"] or "{}")
    scope = extra.get("historical")
    return (scope["start"], scope["end"]) if scope else None


def counted_summaries(conn) -> list:
    summaries = list(conn.execute("SELECT * FROM style_sales"))
    orders: dict[str, list[str]] = {}
    for row in conn.execute("SELECT variant_code, order_date FROM style_order_lines"):
        orders.setdefault(row["variant_code"], []).append(row["order_date"] or "")
    histories = {row["variant_code"] for row in summaries if historical_range(row)}
    result = []
    for row in summaries:
        dates = orders.get(row["variant_code"], [])
        scope = historical_range(row)
        if scope:
            start, end = scope
            # An undated order prevents us from proving that ranges are disjoint.
            if any(not date or start <= date[:10] <= end for date in dates):
                continue
        elif dates or row["variant_code"] in histories:
            continue
        result.append(row)
    return result


def policy_summary(conn) -> dict:
    all_rows = list(conn.execute("SELECT * FROM style_sales"))
    counted = counted_summaries(conn)
    return {
        "reference_rows": len(all_rows) - len(counted),
        "reference_quantity": sum(int(r["quantity"]) for r in all_rows) - sum(int(r["quantity"]) for r in counted),
        "note": "同一客户版本以订单明细为准；未注明时间范围的汇总不与明细相加。历史补充仅在时间范围不重叠时计入。",
    }
