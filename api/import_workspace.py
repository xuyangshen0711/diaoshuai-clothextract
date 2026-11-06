"""Import workspace routes; covered by the application's normal site login gate."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from PIL import Image
from pydantic import BaseModel

from src import catalog, import_jobs, storage
from src.factory.codes import image_style_code, parse_style_code
from src.factory.excel_io import workbook_table_kind
from src.factory.import_workspace import confirm_table, history, preview_table, replan_table, workspace
from src.factory.tables import own_factory_images

router = APIRouter(prefix="/api/import")


@router.get("/workspace")
def get_workspace(status: str = Query("missing", pattern="^(all|missing|image|archive|no_sales)$"),
                  q: str = "", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    return workspace(status, q, page, page_size)


@router.get("/table-history")
def table_history():
    return history()


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
def inspect_files(files: list[UploadFile] = File(...), category: str = Form(...)):
    with catalog._database() as conn:
        images = own_factory_images(conn)
        known = {r[0] for table in ("style_variants", "style_sales", "style_order_lines")
                 for r in conn.execute(f"SELECT DISTINCT style_code FROM {table}")}
    result = []
    seen = set()
    with tempfile.TemporaryDirectory() as directory:
        for upload in files:
            name = (upload.filename or "upload").replace("\\", "/")
            path = Path(directory) / (uuid4().hex + Path(name).suffix.lower())
            with path.open("wb") as out:
                shutil.copyfileobj(upload.file, out)
            item = {"name": name, "kind": "media", "action": "new", "style_code": "", "message": "待抽取图片"}
            try:
                kind = workbook_table_kind(path) if path.suffix in {".xlsx", ".xlsm"} else None
                if kind in {"archive", "sales"}:
                    item.update(kind=kind, action="route", message="识别为款式档案" if kind == "archive" else "识别为销量表")
                elif category == "本厂产品" and path.suffix in {".pdf", ".pptx", ".xlsx", ".xlsm"}:
                    item.update(action="invalid", message="本厂封面请上传以款号命名的图片；档案与销量请使用专用入口")
                elif path.suffix not in {".pdf", ".pptx", ".xlsx", ".xlsm"}:
                    with Image.open(path) as image:
                        item["width"], item["height"] = image.size
                        image.verify()
                    item["message"] = "图片可导入"
                    if category == "本厂产品":
                        code = image_style_code(Path(name).name)
                        parsed = parse_style_code(code)
                        item["style_code"] = code
                        if not parsed or parsed.suffix:
                            item.update(action="invalid", message=f"文件名需为本厂款号，例如 {parsed.base if parsed else '516083'}.jpg")
                        elif code in seen:
                            item.update(action="invalid", message="本批同一款号有多张图片，请只保留一张")
                        else:
                            seen.add(code)
                            target = images.get(code)
                            if target and storage.is_file(target.image_path):
                                same = import_jobs._same_file(path, target.pending_image_path or target.image_path)
                                item.update(action="unchanged" if same else "replacement", message="相同图片，将跳过" if same else "已有图片；新图先送审核，确认后替换")
                            else:
                                item["message"] = "匹配已有款号，补充图片" if code in known else "新款号，将建立图片记录"
            except Exception:
                item.update(action="invalid", message="文件无法读取，请检查格式或重新导出")
            result.append(item)
    return {"items": result}
