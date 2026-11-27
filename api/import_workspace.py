"""Import workspace routes; covered by the application's normal site login gate."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel, Field

from src import catalog, import_jobs, storage
from src.factory.codes import image_style_code, parse_style_code
from src.factory.excel_io import KIND_LABELS, workbook_table_kind
from src.factory.import_workspace import confirm_table, history, preview_table, replan_table, workspace
from src.factory.tables import own_factory_images
from src.factory import sales_deletion

router = APIRouter(prefix="/api/import")


@router.get("/workspace")
def get_workspace(status: str = Query("missing", pattern="^(all|missing|image|archive|no_sales)$"),
                  q: str = "", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                  verify_remote: bool = True):
    return workspace(status, q, page, page_size, verify_remote=verify_remote)


@router.get("/table-history")
def table_history():
    return history()


def _sales_action(action, **kwargs):
    try:
        return action(**kwargs)
    except KeyError as exc:
        raise HTTPException(404, "找不到该批次或备份记录。") from exc
    except ValueError as exc:
        if isinstance(exc, catalog.CatalogConflict):
            raise HTTPException(409, str(exc)) from exc
        raise HTTPException(422, str(exc)) from exc


class SalesSelection(BaseModel):
    scope: Literal["selected", "batch", "all"] = "selected"
    batch_id: str = Field(default="", max_length=32)
    ids: list[str] = Field(default_factory=list, max_length=5000)


class SalesDeletion(SalesSelection):
    token: str = Field(min_length=64, max_length=64)


@router.get("/sales-data")
def sales_data(batch_id: str = "", q: str = Query("", max_length=200),
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)):
    return _sales_action(sales_deletion.list_rows, batch_id=batch_id, q=q, page=page, page_size=page_size)


@router.post("/sales-data/delete-preview")
def sales_delete_preview(body: SalesSelection):
    return _sales_action(sales_deletion.preview, **body.model_dump())


@router.post("/sales-data/delete")
def sales_delete(body: SalesDeletion):
    return _sales_action(sales_deletion.delete, **body.model_dump())


@router.get("/sales-deletions")
def sales_deletions(page: int = Query(1, ge=1)):
    return sales_deletion.deletions(page=page)


@router.get("/sales-deletions/{identifier}/backup")
def sales_deletion_backup(identifier: str):
    result = _sales_action(sales_deletion.backup, identifier=identifier)
    return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="sales-deletion-{result["id"]}.json"',
                                         "Cache-Control": "no-store"})


class SalesRestoration(BaseModel):
    token: str = Field(min_length=64, max_length=64)


@router.post("/sales-deletions/{identifier}/restore-preview")
def sales_restore_preview(identifier: str):
    return _sales_action(sales_deletion.restore_preview, identifier=identifier)


@router.post("/sales-deletions/{identifier}/restore")
def sales_restore(identifier: str, body: SalesRestoration):
    return _sales_action(sales_deletion.restore, identifier=identifier, token=body.token)


@router.post("/table-preview")
def table_preview(file: UploadFile = File(...), kind: str = Form("archive")):
    name = Path(file.filename or "upload.xlsx").name
    if Path(name).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise HTTPException(422, "请上传 .xlsx 或 .xlsm 表格。")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / name
        with path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        try:
            return preview_table(path, name, kind)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (BadZipFile, OSError, KeyError, ParseError) as exc:
            raise HTTPException(422, "表格无法读取，请重新导出 Excel 后再试。") from exc


class ConfirmBody(BaseModel):
    token: str
    acknowledge: bool = False
    historical_start: str = ""
    historical_end: str = ""
    replace_season: bool = False


@router.post("/table-confirm")
def table_confirm(body: ConfirmBody):
    try:
        return confirm_table(**body.model_dump())
    except catalog.CatalogConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/table-replan")
def table_replan(body: ConfirmBody):
    try:
        return replan_table(body.token, body.historical_start, body.historical_end)
    except catalog.CatalogConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/inspect")
def inspect_files(files: list[UploadFile] = File(...), category: str = Form(...),
                  style_codes: list[str] | None = Form(default=None)):
    if style_codes is not None and len(style_codes) != len(files):
        raise HTTPException(422, "每张图片都要有对应的关联款号")
    with tempfile.TemporaryDirectory() as directory:
        entries = []
        for index, upload in enumerate(files):
            name = (upload.filename or 'upload').replace('\\', '/')
            path = Path(directory) / (uuid4().hex + Path(name).suffix.lower())
            with path.open('wb') as out:
                shutil.copyfileobj(upload.file, out)
            entries.append((path, name, style_codes[index] if style_codes is not None else None))
        return inspect_paths(entries, category)


def inspect_paths(entries, category):
    with catalog.read_database() as conn:
        images = own_factory_images(conn)
        known = {r[0] for table in ("style_variants", "style_sales", "style_order_lines")
                 for r in conn.execute(f"SELECT DISTINCT style_code FROM {table}")}
    result = []
    seen = set()
    for path, name, style_code in entries:
        item = {"name": name, "kind": "media", "action": "new", "style_code": "", "message": "待抽取图片"}
        try:
            kind = workbook_table_kind(path) if path.suffix in {".xlsx", ".xlsm"} else None
            if kind in {"archive", "sales", "production", "guide"}:
                item.update(kind=kind, action="route", message=f"识别为{KIND_LABELS[kind]}")
            elif category == "本厂产品" and path.suffix in {".pdf", ".pptx", ".xlsx", ".xlsm"}:
                item.update(action="invalid", message="本厂封面请上传以款号命名的图片；档案与销量请使用专用入口")
            elif path.suffix not in {".pdf", ".pptx", ".xlsx", ".xlsm"}:
                with Image.open(path) as image:
                    item["width"], item["height"] = image.size
                    image.verify()
                item["message"] = "图片可导入"
                if category == "本厂产品":
                    code = style_code if style_code is not None else image_style_code(Path(name).name)
                    parsed = parse_style_code(code)
                    if parsed and not parsed.suffix:
                        code = parsed.base
                    item["style_code"] = code
                    if not parsed or parsed.suffix:
                        item.update(action="invalid", message="请填写有效的关联款号，例如 516083 或 581188*6")
                    elif code in seen:
                        item.update(action="invalid", message="本批同一款号有多张图片，请只保留一张")
                    else:
                        seen.add(code)
                        target = images.get(code)
                        if target and storage.is_file(target.image_path):
                            same = import_jobs.same_tagged_factory_image(target, path)
                            item.update(action="unchanged" if same else "replacement", message="相同图片，将跳过" if same else "已有图片，新图识别成功后自动替换")
                        else:
                            item["message"] = "匹配已有款号，补充图片" if code in known else "新款号，将建立图片记录"
        except Exception:
            item.update(action="invalid", message="文件无法读取，请检查格式或重新导出")
        result.append(item)
    return {"items": result}
