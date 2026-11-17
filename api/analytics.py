"""Sales analytics endpoints with an independent, read-only access boundary."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import OrderedDict
from threading import Lock
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from src.analytics import (
    AnalyticsUnavailable, FilterLabel, ReportFilters, filter_options, read_business_data, report_data, report_observations,
)
from src.analytics_chat import ChatRequest, answer_chat
from src.analytics_access import allow_http_analytics
from src.analytics_pdf import render_report
from src.analytics_questions import QuestionRequest, answer_question, choose_with_deepseek
from src.analytics_yarn import YarnReportFilters, build_yarn_preview, render_yarn_workbook, yarn_filter_options
from src.db import _load_env, mysql_config
from src.site_gate import site_password
from src.tag.tagger import provider_ready

COOKIE = "diaoshuai_analytics"
COOKIE_PATH = "/api/analytics"
SESSION_TTL = 8 * 60 * 60
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
NO_STORE = {"Cache-Control": "no-store"}
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
ATTEMPTS: OrderedDict[tuple[str, str], tuple[int, int]] = OrderedDict()
ATTEMPTS_LOCK = Lock()

Observation = Literal["category", "style", "replenishment"]


class AnalyticsRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def route(request: Request):
            try:
                response = await handle(request)
            except RequestValidationError:
                # FastAPI's default validation response echoes invalid inputs, including passwords.
                response = JSONResponse({"detail": "请检查填写内容和日期范围。"}, status_code=422)
            except AnalyticsUnavailable as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=503)
            response.headers.update(NO_STORE)
            return response

        return route


def _local_request(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    return request.url.hostname in LOCAL_HOSTS and peer in {"127.0.0.1", "::1"} and not mysql_config()


def _same_origin(request: Request):
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    host = f"{request.url.scheme}://{request.url.netloc}"
    if site in {"cross-site", "same-site"} or (origin and origin.casefold() != host.casefold()):
        raise HTTPException(403, "请从本站打开数据分析。", headers=NO_STORE)
    if request.url.scheme != "https" and not _local_request(request):
        if not allow_http_analytics():
            raise HTTPException(403, "请通过 HTTPS 打开数据分析，或由管理员开启 HTTP 兼容访问。", headers=NO_STORE)
        if not site_password():
            raise HTTPException(403, "HTTP 兼容访问需要先设置网站登录密码。", headers=NO_STORE)


router = APIRouter(
    prefix="/api/analytics", tags=["analytics"], route_class=AnalyticsRoute, dependencies=[Depends(_same_origin)],
)


def _password() -> str:
    _load_env()
    password = os.getenv("ANALYTICS_PASSWORD", "").strip()
    return password if MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH and password != site_password() else ""


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), ("analytics:" + payload).encode(), hashlib.sha256).hexdigest()


def _authenticated(request: Request, secret: str) -> bool:
    if len(secret) < MIN_PASSWORD_LENGTH:
        return False
    cookie = request.cookies.get(COOKIE, "")
    if len(cookie) > 150:
        return False
    expiry, nonce, signature = (cookie.split(".", 2) + ["", "", ""])[:3]
    if not expiry.isascii() or not expiry.isdigit() or int(expiry) <= int(time.time()) or not nonce:
        return False
    return hmac.compare_digest(_sign(secret, f"{expiry}.{nonce}").encode(), signature.encode())


def _require_access(request: Request):
    if not _authenticated(request, _password()):
        raise HTTPException(403, "请先解锁数据分析。", headers=NO_STORE)


def _throttle(request: Request, bucket: str, per_minute: int):
    now = int(time.monotonic() // 60)
    key = (bucket, request.client.host if request.client else "")
    with ATTEMPTS_LOCK:
        previous, count = ATTEMPTS.get(key, (now, 0))
        count = count if previous == now else 0
        if count >= per_minute:
            raise HTTPException(429, "操作太频繁，请稍后再试。", headers=NO_STORE)
        ATTEMPTS[key] = (now, count + 1)
        ATTEMPTS.move_to_end(key)
        if len(ATTEMPTS) > 1024:
            ATTEMPTS.popitem(last=False)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=256)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: ReportFilters
    use_deepseek: bool = False


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: ReportFilters
    snapshot: str = Field(pattern=r"^[a-f0-9]{64}$")
    emphasis: Observation | None = None


class YarnPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: YarnReportFilters
    acknowledge_missing: bool = False


class YarnExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: YarnReportFilters
    snapshot: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_missing: bool = False
    selected_materials: list[FilterLabel] | None = Field(default=None, max_length=200)


@router.get("/session")
def session(request: Request):
    password = _password()
    authenticated = _authenticated(request, password)
    return {
        "configured": bool(password),
        "authenticated": authenticated,
        "deepseek_configured": provider_ready("deepseek") if authenticated else False,
        "unencrypted_connection": request.url.scheme != "https" and not _local_request(request),
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    _throttle(request, "login", 10)
    password = _password()
    if not password:
        raise HTTPException(403, "请管理员先设置独立的数据分析密码。", headers=NO_STORE)
    if not hmac.compare_digest(hashlib.sha256(body.password.encode()).digest(), hashlib.sha256(password.encode()).digest()):
        raise HTTPException(403, "分析密码不正确。", headers=NO_STORE)
    token = f"{int(time.time()) + SESSION_TTL}.{secrets.token_urlsafe(18)}"
    response.set_cookie(
        COOKIE, f"{token}.{_sign(password, token)}", max_age=SESSION_TTL, path=COOKIE_PATH,
        httponly=True, samesite="strict", secure=request.url.scheme == "https",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path=COOKIE_PATH, samesite="strict", httponly=True)
    return {"ok": True}


@router.get("/options", dependencies=[Depends(_require_access)])
def options():
    with read_business_data() as conn:
        return filter_options(conn)


def _observations(report: dict, emphasis: Observation | None = None) -> list[str]:
    notes = report_observations(report)
    if emphasis in notes:
        return [notes[emphasis], *(note for key, note in notes.items() if key != emphasis)]
    return list(notes.values())


@router.post("/report", dependencies=[Depends(_require_access)])
def preview(body: PreviewRequest, request: Request):
    _throttle(request, "report", 30)
    with read_business_data() as conn:
        report = report_data(conn, body.filters)
    emphasis = None
    errors = []
    if body.use_deepseek and report["totals"]["styles"]:
        totals = report["totals"]
        cues = ["本厂订货报告"]
        if totals["styles"] == 1:
            cues.append("只有一个下单款")
        if totals["quantity"] > 0 and totals["replenishment"] / totals["quantity"] >= 0.15:
            cues.append("补单占比超过或等于百分之十五")
        if len(report["categories"]) > 1:
            cues.append("存在多个品类")
        emphasis = choose_with_deepseek(("category", "style", "replenishment"), cues, errors=errors)
    return {
        **report, "observations": _observations(report, emphasis),
        "emphasis": emphasis, "provider": "deepseek" if emphasis else "rules",
        "notice": errors[0] if errors else "未使用 AI 选择重点，当前报告按已核验的订单数据生成。" if body.use_deepseek and not emphasis else "",
    }


@router.post("/report/pdf", dependencies=[Depends(_require_access)])
def pdf(body: ExportRequest, request: Request):
    _throttle(request, "pdf", 15)
    with read_business_data() as conn:
        report = report_data(conn, body.filters)
    if not hmac.compare_digest(report["snapshot"], body.snapshot):
        raise HTTPException(409, "订单数据已变化，请重新预览再下载。", headers=NO_STORE)
    filename = f"factory-order-report-{body.filters.start.isoformat()}-{body.filters.end.isoformat()}.pdf"
    return Response(
        render_report(report, _observations(report, body.emphasis)),
        media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/question", dependencies=[Depends(_require_access)])
def question(body: QuestionRequest, request: Request):
    _throttle(request, "question", 15)
    return answer_question(body)


@router.post("/chat", dependencies=[Depends(_require_access)])
def chat(body: ChatRequest, request: Request):
    _throttle(request, "question", 15)
    return answer_chat(body)


@router.get("/yarn-table/options", dependencies=[Depends(_require_access)])
def yarn_options():
    with read_business_data() as conn:
        return yarn_filter_options(conn)


@router.post("/yarn-table/preview", dependencies=[Depends(_require_access)])
def yarn_preview(body: YarnPreviewRequest, request: Request):
    _throttle(request, "yarn-report", 40)
    with read_business_data() as conn:
        return build_yarn_preview(conn, body.filters, acknowledge_missing=body.acknowledge_missing)


@router.post("/yarn-table/xlsx", dependencies=[Depends(_require_access)])
def yarn_xlsx(body: YarnExportRequest, request: Request):
    _throttle(request, "yarn-xlsx", 20)
    with read_business_data() as conn:
        result = build_yarn_preview(conn, body.filters, acknowledge_missing=body.acknowledge_missing)
    if result["requires_confirmation"] or result["report"] is None:
        raise HTTPException(409, "存在缺失数据提醒，请先在页面确认后再导出。", headers=NO_STORE)
    report = result["report"]
    if not hmac.compare_digest(report["snapshot"], body.snapshot):
        raise HTTPException(409, "订单数据已变化，请重新预览再导出。", headers=NO_STORE)
    end_date = report["scope"]["effective_end_date"]
    filename = f"factory-yarn-color-report-{end_date}.xlsx"
    payload = render_yarn_workbook(
        report,
        selected_materials=body.selected_materials,
        missing=result["missing"],
    )
    return Response(
        payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
