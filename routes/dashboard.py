"""Dashboard pages, model listing, and runtime settings."""

import os
import re
import secrets
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import auth
import shared
from db import core as db_core
from db import search as db_search
from routes.memories import start_memory_embedding_backfill

router = APIRouter()
settings_router = APIRouter()
_MASKED_KEYS = {"API_KEY", "EMBEDDING_API_KEY", "MEMORY_API_KEY"}
logger = logging.getLogger(__name__)

@router.get("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login_page(request: Request):
    """Dashboard 独立登录页，网关主密钥不进入浏览器。"""
    if auth.valid_dashboard_session(request.cookies.get(shared.DASHBOARD_SESSION_COOKIE, "")):
        return RedirectResponse(url="/dashboard", status_code=303)
    configured = auth.dashboard_auth_ready()
    return shared.templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "config_error": None if configured else "请先设置 DASHBOARD_PASSWORD 和至少 32 字符的 SESSION_SECRET。",
        },
        status_code=200 if configured else 503,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login(request: Request):
    """校验 Dashboard 密码并签发短期 HttpOnly Cookie。"""
    if not auth.dashboard_auth_ready():
        return shared.templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": None,
                "config_error": "请先设置 DASHBOARD_PASSWORD 和至少 32 字符的 SESSION_SECRET。",
            },
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    body = await request.body()
    fields = parse_qs(body[:4096].decode("utf-8", errors="replace"))
    password = fields.get("password", [""])[0]
    if len(body) > 4096 or not secrets.compare_digest(
        password.encode("utf-8"),
        shared.DASHBOARD_PASSWORD.encode("utf-8"),
    ):
        return shared.templates.TemplateResponse(
            request,
            "login.html",
            {"error": "密码不正确。", "config_error": None},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        shared.DASHBOARD_SESSION_COOKIE,
        auth.make_dashboard_session(),
        max_age=shared.DASHBOARD_SESSION_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/dashboard/logout")
async def dashboard_logout():
    response = RedirectResponse(url="/dashboard/login", status_code=303)
    response.delete_cookie(
        shared.DASHBOARD_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Dashboard - 整合的记忆管理界面"""
    if not shared.DATABASE_ENABLED:
        return shared.templates.TemplateResponse(
            request,
            "database_disabled.html",
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return shared.templates.TemplateResponse(
        request,
        "dashboard.html",
        {"memory_enabled": shared.MEMORY_ENABLED},
    )



# ============================================================
# 模型列表 API（/api/models）
# 设置面板的 combo-box 用，根据 API_BASE_URL 自动适配
# ============================================================

def _google_model_sort_key(model):
    """Put versioned Gemini models first, newest major/minor version first."""
    model_id = model.get("id", "")
    match = re.search(r"(?:^|/)gemini-(\d+)(?:\.(\d+))?", model_id)
    if not match:
        return (1, 0, 0, model_id)
    return (0, -int(match.group(1)), -int(match.group(2) or 0), model_id)

@settings_router.get("/api/models")
async def get_models():
    """获取可用模型列表（根据 API_BASE_URL 自动适配）"""
    try:
        hostname = urlsplit(shared.API_BASE_URL).hostname
    except ValueError:
        hostname = None
    is_openrouter = hostname == "openrouter.ai"
    is_google = hostname == "generativelanguage.googleapis.com"
    is_openai = hostname == "api.openai.com"

    try:
        if is_openrouter:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {shared.API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [{"id": m.get("id"), "name": m.get("name"), "context_length": m.get("context_length")} for m in models]
                    simplified.sort(key=lambda x: x.get("name", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openrouter"}

        elif is_google:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": shared.API_KEY, "pageSize": 1000},
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("models", [])
                    simplified = []
                    for m in models:
                        full_name = m.get("name", "")
                        model_id = full_name.replace("models/", "") if full_name.startswith("models/") else full_name
                        display_name = m.get("displayName", model_id)
                        supported_methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in supported_methods:
                            simplified.append({"id": model_id, "name": display_name, "context_length": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit")})
                    simplified.sort(key=_google_model_sort_key)
                    return {"models": simplified, "total": len(simplified), "provider": "google"}
                else:
                    print(f"[get_models] Google API 返回 {response.status_code}: {response.text}")
                    return {"error": f"Google API 返回 {response.status_code}", "models": [], "provider": "google"}

        elif is_openai:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {shared.API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [
                        {"id": m.get("id", ""), "name": m.get("id", "")}
                        for m in models
                        if m.get("id")
                    ]
                    simplified.sort(key=lambda x: x.get("id", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openai"}
                print(f"[get_models] OpenAI API 返回 {response.status_code}: {response.text}")
                return {"error": f"OpenAI API 返回 {response.status_code}", "models": [], "provider": "openai"}

        else:
            return {"models": [], "total": 0, "provider": "unknown", "note": "未识别的 API，请手动输入模型名"}

    except Exception:
        return shared._api_failure("加载模型列表失败", models=[])


# ============================================================
# 高级设置面板 API（/api/settings）
# Dashboard 前端设置面板用，管理所有运行时可调配置
# ============================================================

def _mask_key(key_value: str) -> str:
    """API Key 打码：只露前5位和后4位"""
    if not key_value:
        return ""
    if len(key_value) < 10:
        return "****"
    return key_value[:5] + "****" + key_value[-4:]


def _is_masked(value: str) -> bool:
    """判断值是否是打码值（用户没改过）"""
    return "****" in str(value)


@settings_router.get(
    "/api/settings",
    dependencies=[Depends(auth.require_database_enabled)],
)
async def get_settings():
    """获取高级设置（数据库优先，fallback 到环境变量/运行时默认值）"""
    try:
        db = await db_core.get_all_gateway_config()

        settings = {
            key: parser(db[key]) if db.get(key) else getattr(shared, key)
            for key, parser in shared.SETTINGS_TYPES.items()
        }
        for key in _MASKED_KEYS:
            settings[key] = _mask_key(settings[key])
        settings["MEMORY_MODEL"] = db.get("MEMORY_MODEL") or os.environ.get("MEMORY_MODEL", "")
        settings["systemPrompt"] = db.get("systemPrompt") or shared._DEFAULT_SYSTEM_PROMPT or ""
        try:
            vector_dimensions = await db_core.get_vector_dimensions()
        except Exception:
            vector_dimensions = {}
        try:
            vector_presence = await db_core.get_embedding_vector_presence()
        except Exception:
            vector_presence = None

        return {
            "status": "ok",
            "settings": settings,
            "vector_dimensions": vector_dimensions,
            "vector_presence": vector_presence,
            "embedding_state": db_search.embedding_status(),
        }
    except Exception:
        return shared._api_failure("加载设置失败")


@settings_router.put(
    "/api/settings",
    dependencies=[Depends(auth.require_database_enabled)],
)
async def save_settings(request: Request):
    """保存高级设置（写入数据库 + 热更新运行时变量，立即生效无需重启）"""
    try:
        data = await request.json()
        updated = []
        skipped = []
        if data.pop("redetect_embedding", False) is True:
            await db_search.clear_embedding_detection()
            return {"status": "ok", "message": "已清除检测结果，下次向量请求会重新检测"}
        confirmed = data.pop("confirm_embedding_rebuild", False) is True
        retain_vectors = data.pop("retain_embedding_vectors", False) is True
        embedding_fields_present = retain_vectors or any(
            key in data for key in ("EMBEDDING_DIM", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
        )
        embedding_dim = data.pop("EMBEDDING_DIM", shared.EMBEDDING_DIM)
        embedding_base_url = str(data.pop("EMBEDDING_BASE_URL", shared.EMBEDDING_BASE_URL))
        embedding_model = str(data.pop("EMBEDDING_MODEL", shared.EMBEDDING_MODEL))
        embedding_result = None
        if embedding_fields_present:
            embedding_key = str(data.get("EMBEDDING_API_KEY", shared.EMBEDDING_API_KEY)).strip()
            if _is_masked(embedding_key):
                embedding_key = shared.EMBEDDING_API_KEY
            try:
                embedding_dim = int(embedding_dim)
            except (TypeError, ValueError):
                return {"error": "Embedding 维度必须为正整数"}
            try:
                async with db_search.embedding_write_lock:
                    embedding_result = await db_core.save_embedding_settings(
                        embedding_base_url, embedding_model, embedding_dim, confirmed,
                        allow_empty=not bool(embedding_key),
                        retain_vectors=retain_vectors,
                    )
                    shared.EMBEDDING_BASE_URL = embedding_base_url
                    shared.EMBEDDING_MODEL = embedding_model
                    shared.EMBEDDING_DIM = embedding_dim
                    await db_search.sync_embedding_settings(embedding_result)
            except ValueError as e:
                return {"error": str(e)}
            except Exception:
                logger.exception("Embedding settings update failed")
                return {"error": "向量设置更新未完成，请刷新页面检查状态"}
            for key, value in (
                ("EMBEDDING_BASE_URL", embedding_base_url),
                ("EMBEDDING_MODEL", embedding_model),
                ("EMBEDDING_DIM", embedding_dim),
            ):
                os.environ[key] = str(value)
                updated.append(key)

        # 只存 os.environ 的变量
        _ENV_ONLY = {"MEMORY_MODEL": str}

        for key, value in data.items():
            # --- 打码字段特殊处理 ---
            if key in _MASKED_KEYS:
                str_val = str(value).strip()
                if _is_masked(str_val):
                    skipped.append(key)
                    continue
                if not str_val:
                    await db_core.set_gateway_config(key, "")
                    if key in shared.SETTINGS_TYPES:
                        setattr(shared, key, "")
                    os.environ[key] = ""
                    updated.append(key)
                    continue
                value = str_val

            # --- systemPrompt 特殊处理 ---
            if key == "systemPrompt":
                await db_core.set_gateway_config("systemPrompt", str(value))
                shared.invalidate_system_prompt_cache()
                updated.append("systemPrompt")
                print(f"[settings] systemPrompt 已更新（{len(str(value))} 字）")
                continue

            # --- 常规字段 ---
            await db_core.set_gateway_config(key, str(value))

            if key in shared.SETTINGS_TYPES:
                typed_value = shared.SETTINGS_TYPES[key](value)
                setattr(shared, key, typed_value)
                os.environ[key] = str(value)
                updated.append(key)
                if key in _MASKED_KEYS:
                    print(f"[settings] {key} 已更新")
                else:
                    print(f"[settings] {key} = {typed_value}")

            elif key in _ENV_ONLY:
                typed_value = _ENV_ONLY[key](value)
                os.environ[key] = str(typed_value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value} (env)")

            else:
                skipped.append(key)

        if updated:
            shared.sync_memory_extractor_config()
        if embedding_result and embedding_result["applied"] and not embedding_result.get("retained"):
            db_search.kick_embedding_backfill()
            if shared.MEMORY_ENABLED and shared.MEMORY_VECTOR_ENABLED and shared.EMBEDDING_API_KEY:
                try:
                    await start_memory_embedding_backfill()
                except Exception:
                    logger.exception("Memory embedding backfill failed to start")
        if (
            "CONVERSATION_RECALL_ENABLED" in updated
            and shared.CONVERSATION_RECALL_ENABLED
        ):
            await db_search.rebuild_content_tsv()
            db_search.kick_embedding_backfill()

        return {
            "status": "ok",
            "updated": updated,
            "skipped": skipped,
            "vector_rebuilt": embedding_result["rebuilt"] if embedding_result else [],
            "vector_cleared": embedding_result["cleared"] if embedding_result else False,
            "vector_retained": embedding_result.get("retained", False) if embedding_result else False,
            "embedding_pending": embedding_result["pending"] if embedding_result else False,
            "message": f"已更新 {len(updated)} 项配置，立即生效"
        }
    except Exception:
        return shared._api_failure("保存设置失败")
