"""Read-only import projection using the existing merge and summary-counting rules."""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP

from src.factory.sales_deletion import row_id
from src.factory.sales_policy import counted_from_rows


def _totals(rows):
    rows = list(rows)
    amount = sum((Decimal(str(row["amount"])) for row in rows), Decimal(0))
    return {"quantity": sum(int(row["quantity"]) for row in rows),
            "amount": float(amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))}


def _metrics(state):
    orders = [entry["row"] for entry in state.values() if entry["kind"] == "orders"]
    summaries = [entry["row"] for entry in state.values() if entry["kind"] == "summary"]
    counted = counted_from_rows(summaries, orders)
    return {"orders": _totals(orders), "counted": _totals([*orders, *counted])}, {
        row_id("summary", row) for row in counted}


def import_impact(before, after, kind, rows):
    """Report full-library differences; do not confuse file totals with additions."""
    old, old_summaries = _metrics(before)
    new, new_summaries = _metrics(after)
    referenced = {row_id(kind, row) for row in rows}
    result = {"version": 1, "row_kind": kind, "scope": "all_records_all_seasons",
              "retained_rows": sum(entry["kind"] == kind and key not in referenced and after.get(key) == entry
                                   for key, entry in before.items()),
              "summaries_deactivated": len(old_summaries - new_summaries),
              "summaries_reactivated": len(new_summaries - old_summaries)}
    for name in ("orders", "counted"):
        result[name] = {"before": old[name], "after": new[name],
                        "delta": {"quantity": new[name]["quantity"] - old[name]["quantity"],
                                  "amount": float(Decimal(str(new[name]["amount"])) -
                                                  Decimal(str(old[name]["amount"])))} }
    return result


def projected_impact(before, plan, kind, rows, historical_start="", historical_end="", removed=()):
    # Copy only the changed entries. Existing rows, including omitted rows, stay intact
    # unless 整季替换 lists them in `removed`.
    after = dict(before)
    for key in removed:
        after.pop(key, None)
    for item in plan:
        if item["action"] == "unchanged":
            continue
        row = item["row"]
        key = row_id(kind, row)
        previous = before.get(key, {}).get("row", {})
        after[key] = {"kind": kind, "row": {"extra_json": "{}", **previous, **row}}
    if historical_start:
        extra = json.dumps({"historical": {"start": historical_start, "end": historical_end}})
        for row in rows:
            key = row_id(kind, row)
            after[key] = {"kind": kind, "row": {**after[key]["row"], "extra_json": extra}}
    return import_impact(before, after, kind, rows)
