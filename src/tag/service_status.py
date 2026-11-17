"""Explicit, read-only provider probes. Configuration alone never means connected."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from threading import Lock

import openai

from src.tag.tagger import (
    deepseek_key_is_placeholder, default_tagger_backend, get_tagger,
    openai_key_is_placeholder, provider_ready,
)

PROVIDERS = ("deepseek", "openai")
RESULT_TTL = 300
_checks: dict[str, tuple[str, float, dict]] = {}
_locks = {name: Lock() for name in PROVIDERS}


def _configuration(provider: str) -> tuple[str, dict]:
    if provider not in PROVIDERS:
        raise ValueError("不支持的分类服务")
    configured = provider_ready(provider)  # Loads the same runtime .env as the taggers.
    prefix = provider.upper()
    model_key = "DEEPSEEK_MODEL" if provider == "deepseek" else "OPENAI_VISION_MODEL"
    model = os.getenv(model_key, "deepseek-flash" if provider == "deepseek" else "gpt-4o-mini").strip()
    placeholder = (deepseek_key_is_placeholder if provider == "deepseek" else openai_key_is_placeholder)()
    # This digest stays inside the process; neither keys nor endpoint URLs are returned.
    signature = hashlib.sha256(json.dumps([
        os.getenv(prefix + "_API_KEY", ""), os.getenv(prefix + "_BASE_URL", ""), model,
    ]).encode()).hexdigest()
    return signature, {
        "provider": provider, "name": "DeepSeek" if provider == "deepseek" else "OpenAI",
        "configured": configured, "placeholder": placeholder, "model": model,
        "status": "unchecked" if configured else "placeholder" if placeholder else "not_configured",
        "message": "密钥已配置，尚未检测连接。" if configured else "密钥仍是示例值，请完成配置。" if placeholder else "尚未配置该分类服务。",
        "checked_at": None, "latency_ms": None, "model_listed": None,
    }


def provider_status(provider: str) -> dict:
    signature, result = _configuration(provider)
    previous = _checks.get(provider)
    if result["configured"] and previous and previous[0] == signature:
        if time.monotonic() - previous[1] < RESULT_TTL:
            result.update(previous[2])
        else:
            result["message"] = "上次检测已超过 5 分钟，请重新检测连接。"
    return result


def service_status() -> dict:
    return {"default_backend": default_tagger_backend(), "providers": [provider_status(p) for p in PROVIDERS]}


def _failure(exc: openai.APIStatusError) -> str:
    code = exc.status_code
    messages = {
        401: "密钥验证失败，请检查 API 密钥。",
        402: "AI 账户余额不足，需要充值 AI 后重试。",
        403: "服务商拒绝访问，请检查密钥权限或项目设置。",
        404: "模型列表接口不存在，请检查 API 服务地址。",
        429: "服务商限制了请求，请稍后重试并检查配额。",
    }
    return messages.get(code, "服务商暂时不可用，请稍后重试。" if code >= 500 else f"服务商返回异常（HTTP {code}），请检查配置。")


def check_provider(provider: str) -> dict:
    signature, configured = _configuration(provider)
    if not configured["configured"]:
        return configured
    if not _locks[provider].acquire(blocking=False):
        return {**provider_status(provider), "status": "checking", "message": "连接检测正在进行，请稍候。"}
    started = time.monotonic()
    try:
        result = {"status": "error", "model_listed": None}
        tagger = None
        try:
            # Reuse the actual tagger's key, gateway and model; never send a completion or image.
            tagger = get_tagger(provider)
            with tagger.client.with_options(timeout=8.0, max_retries=0) as client:
                models = client.models.list().data
            if not isinstance(models, list) or any(not isinstance(getattr(m, "id", None), str) for m in models):
                raise ValueError("Invalid model list")
            listed = tagger.model in {m.id for m in models}
            result.update(status="connected", model_listed=listed, message=(
                "已通过服务商认证，当前模型在可用列表中。" if listed else
                "已通过服务商认证；当前模型未出现在返回列表中，请核对模型名称。"
            ))
        except openai.APITimeoutError:
            result["message"] = "连接超时，请检查网络或 API 服务地址后重试。"
        except openai.APIConnectionError:
            result["message"] = "无法连接服务商，请检查网络或 API 服务地址。"
        except openai.APIStatusError as exc:
            if exc.status_code in (404, 405, 501):
                result.update(status="unsupported", message="服务地址未提供模型列表检测接口，暂时无法验证；请核对地址或以实际分类任务为准。")
            else:
                result["message"] = _failure(exc)
        except Exception:
            # Provider errors may include credentials or gateway URLs; do not echo their text.
            result["message"] = "未能完成检测，请检查服务配置及模型列表接口。"
        finally:
            if tagger is not None:
                tagger.client.close()
        result.update(checked_at=datetime.now(timezone.utc).isoformat(), latency_ms=round((time.monotonic() - started) * 1000))
        _checks[provider] = (signature, time.monotonic(), result)
        return provider_status(provider)
    finally:
        _locks[provider].release()
