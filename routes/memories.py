"""Memory CRUD, import, lifecycle, and backfill routes."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import shared
import memory_extractor
import memory_consolidator
from db import core as db_core
from db import search as db_search
from db import conversations as db_conversations
from db import memories as db_memories

logger = logging.getLogger(__name__)

bootstrap_router = APIRouter()
router = APIRouter()
maintenance_router = APIRouter()

# ============================================================
# 记忆管理接口
# ============================================================


@bootstrap_router.get("/import/seed-memories")
async def import_seed_memories():
    """一次性导入预置记忆（从 seed_memories.py）"""
    try:
        from seed_memories import run_seed_import
        result = await run_seed_import()
        return result
    except ImportError:
        return {"error": "未找到 seed_memories.py，请参考 seed_memories_example.py 创建"}
    except Exception:
        return shared._api_failure("导入预置记忆失败")


@bootstrap_router.get("/export/memories")
async def export_memories():
    """
    导出所有记忆为 JSON（用于备份或迁移）
    浏览器访问这个地址就会返回所有记忆数据
    """
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}

    try:
        memories = await db_memories.get_all_memories()
        database_instance_id = await db_core.get_gateway_config("database_instance_id")
        # 库内 id 在备份里叫 backup_id：只是恢复时重建 merged_from 关系的映射键，
        # 不承诺导入后保持同一 id；日期转成字符串
        for mem in memories:
            mem["backup_id"] = mem.pop("id")
            if mem.get("created_at"):
                mem["created_at"] = str(mem["created_at"])
            if mem.get("event_date"):
                mem["event_date"] = str(mem["event_date"])
            for key in ("remind_at", "reminder_delivered_at"):
                if mem.get(key):
                    mem[key] = str(mem[key])

        return {
            "schema_version": 5,
            "source_database_id": database_instance_id,
            "total": len(memories),
            "exported_at": str(datetime.now()),
            "memories": memories,
        }
    except db_memories.BrokenMergeReferencesError as e:
        return {
            "error": f"检测到 {e.count} 条记忆的合并来源已失效，可修复断裂引用后重新导出",
            "code": "broken_merge_references",
            "count": e.count,
        }
    except db_memories.BrokenSupersessionReferencesError as e:
        return {
            "error": str(e),
            "code": "broken_supersession_references",
            "count": e.count,
        }
    except Exception:
        return shared._api_failure("导出记忆失败")


@bootstrap_router.post("/api/memories/repair-broken-references")
async def api_repair_broken_merge_references():
    """显式清除失效的合并来源关系，使完整备份可以重新导出。"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    repaired = await db_memories.repair_broken_merge_references()
    return {"status": "ok", "repaired": repaired}


# ============================================================
# 管理 API
# ============================================================

@router.post("/api/memories")
async def api_create_memory(request: Request):
    """由 MCP、脚本或迁移工具显式写入一条记忆。"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON"})
    if not isinstance(data, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象"})

    content = data.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse(status_code=400, content={"error": "content 必须是非空字符串"})
    content = content.strip()

    importance = data.get("importance", 5)
    if (
        isinstance(importance, bool)
        or not isinstance(importance, int)
        or not 1 <= importance <= 10
    ):
        return JSONResponse(status_code=400, content={"error": "importance 必须是 1 到 10 的整数"})

    layer = data.get("layer", 1)
    if isinstance(layer, bool) or not isinstance(layer, int) or layer not in (1, 2, 3):
        return JSONResponse(status_code=400, content={"error": "layer 必须是 1、2 或 3"})

    title = data.get("title")
    if title is not None and not isinstance(title, str):
        return JSONResponse(status_code=400, content={"error": "title 必须是字符串或 null"})
    title = (title.strip() or None) if isinstance(title, str) else None

    source_session = data.get("source_session", "")
    if not isinstance(source_session, str):
        return JSONResponse(status_code=400, content={"error": "source_session 必须是字符串"})
    source_session = source_session.strip()

    external_id = data.get("external_id")
    if external_id is not None and (
        not isinstance(external_id, str) or not external_id.strip()
    ):
        return JSONResponse(status_code=400, content={"error": "external_id 必须是非空字符串"})
    external_id = external_id.strip() if isinstance(external_id, str) else None

    event_date = data.get("event_date")
    if event_date is not None:
        if not isinstance(event_date, str):
            return JSONResponse(status_code=400, content={"error": "event_date 必须是 YYYY-MM-DD 字符串或 null"})
        try:
            event_date = date.fromisoformat(event_date)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "event_date 必须是有效的 YYYY-MM-DD 日期"})

    try:
        memory_id = await db_memories.save_memory(
            content=content,
            importance=importance,
            source_session=source_session,
            title=title,
            layer=layer,
            event_date=event_date,
            external_id=external_id,
        )
        if memory_id is None:
            existing = await db_memories.get_memory_by_external_id(external_id)
            if existing is None:
                return JSONResponse(status_code=500, content={"error": "创建记忆失败"})
            for key in ("event_date", "created_at"):
                if existing.get(key):
                    existing[key] = existing[key].isoformat()
            return {
                "status": "ok",
                "inserted": False,
                "id": existing["id"],
                "memory": existing,
            }

        return {
            "status": "ok",
            "inserted": True,
            "id": memory_id,
            "memory": {
                "id": memory_id,
                "content": content,
                "importance": importance,
                "title": title,
                "layer": layer,
                "event_date": event_date.isoformat() if event_date else None,
                "source_session": source_session,
                "external_id": external_id,
                "is_active": True,
            },
        }
    except Exception:
        logger.exception("Explicit memory creation failed")
        return JSONResponse(status_code=500, content={"error": "创建记忆失败"})


@router.get("/api/memories")
async def api_get_memories(layer: int = None, active_only: bool = None):
    """获取所有记忆（管理页面用）

    Query params:
        layer: 筛选层级（1=碎片, 2=事件, 3=核心）
        active_only: 是否只返回活跃记忆
    """
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    memories = await db_memories.get_all_memories_detail(layer=layer, active_only=active_only)
    tz_offset = timezone(timedelta(hours=shared.TIMEZONE_HOURS))
    for m in memories:
        for key in ("created_at", "remind_at", "reminder_delivered_at"):
            if m.get(key):
                dt = m[key]
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                m[key] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
    # 获取层级统计
    try:
        layer_stats = await db_memories.get_layer_statistics()
    except Exception:
        layer_stats = None

    result = {"memories": memories}
    if layer_stats:
        result["layer_stats"] = layer_stats
    return result


async def _run_memory_search(q: str, limit: int):
    """语义搜索记忆（Dashboard用，走后端 search_memories）"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if not q.strip():
        return {"error": "搜索关键词不能为空", "results": []}
    try:
        results, mode = await db_memories.search_memories_with_mode(q.strip(), limit)
        tz_offset = timezone(timedelta(hours=shared.TIMEZONE_HOURS))
        out = []
        for r in results:
            item = dict(r)
            if item.get("created_at"):
                dt = item["created_at"]
                if hasattr(dt, 'tzinfo'):
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    item["created_at"] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
            out.append(item)
        response = {"results": out, "total": len(out), "mode": mode}
        if mode == "keyword":
            response["warning"] = (
                "向量模型本次未生效，已按关键词搜索；"
                "请检查向量搜索开关和 Embedding 配置"
            )
        return response
    except Exception:
        return shared._api_failure("搜索失败", results=[])


@router.get("/api/memories/search")
async def api_search_memories(q: str = "", limit: int = 20):
    return await _run_memory_search(q, limit)


@router.post("/api/memories/search")
async def api_search_memories_post(request: Request):
    """POST 变体避免查询原文进入 URL 与访问日志。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象"})
    q = body.get("q", "")
    limit = body.get("limit", 20)
    if not isinstance(q, str) or not isinstance(limit, int) or isinstance(limit, bool):
        return JSONResponse(status_code=400, content={"error": "q 必须是字符串，limit 必须是整数"})
    return await _run_memory_search(q, limit)


@router.put("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, request: Request):
    """更新单条记忆（支持 content / importance / title / layer）"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    await db_memories.update_memory_with_layer(
        memory_id,
        content=data.get("content"),
        importance=data.get("importance"),
        title=data.get("title"),
        layer=data.get("layer"),
    )
    return {"status": "ok", "id": memory_id}


@router.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int, soft: bool = True):
    """删除单条记忆

    Query params:
        soft: true=可恢复软删除，false=永久删除已归档记忆
    """
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if soft:
        await db_memories.update_memory_with_layer(memory_id, is_active=False)
    else:
        result = await db_memories.delete_archived_memory(memory_id)
        if result["protected"]:
            return JSONResponse(
                status_code=409,
                content={"error": "该记忆仍属于合并或版本链，不能永久删除"},
            )
        if not result["deleted"]:
            return JSONResponse(
                status_code=400,
                content={"error": "只能永久删除已归档记忆"},
            )
    return {"status": "ok", "id": memory_id}


@router.post("/api/memories/batch-update")
async def api_batch_update(request: Request):
    """批量更新记忆"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    updates = data.get("updates", [])
    if not updates:
        return {"error": "没有要更新的记忆"}
    for item in updates:
        await db_memories.update_memory_with_layer(
            item["id"],
            content=item.get("content"),
            importance=item.get("importance"),
            title=item.get("title"),
            layer=item.get("layer"),
        )
    return {"status": "ok", "updated": len(updates)}


@router.post("/api/memories/batch-delete")
async def api_batch_delete(request: Request):
    """批量软删除活跃记忆，或永久删除已归档记忆。"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"error": "未选择记忆"}
    soft = data.get("soft", True)
    if not isinstance(soft, bool):
        return JSONResponse(
            status_code=400,
            content={"error": "soft 必须是布尔值"},
        )
    if soft:
        deleted = await db_memories.soft_delete_memories_batch(ids)
        protected = 0
    else:
        result = await db_memories.delete_archived_memories_batch(ids)
        deleted = result["deleted"]
        protected = result["protected"]
    return {"status": "ok", "deleted": deleted, "protected": protected}


@router.post("/api/memories/batch-restore")
async def api_batch_restore(request: Request):
    """批量恢复已归档记忆。"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"error": "未选择记忆"}
    restored = await db_memories.restore_archived_memories_batch(ids)
    return {"status": "ok", "restored": restored}


# ============================================================
# 三层记忆架构：整理 / 合并 / 升级 / 统计
# ============================================================

_consolidate_status = {
    "running": False,
    "started_at": None,
    "result": None,
    "error": None,
}


@router.post("/api/memories/consolidate")
async def api_manual_consolidate(request: Request):
    """Generate editable organization drafts without changing memory state."""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if _consolidate_status.get("running"):
        return {"status": "already_running", "started_at": _consolidate_status.get("started_at")}

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON"})
    if not isinstance(data, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象"})

    memory_ids = data.get("ids")
    if memory_ids is not None:
        if not isinstance(memory_ids, list):
            return JSONResponse(status_code=400, content={"error": "ids 必须是整数数组"})
        if len(memory_ids) < 2:
            return JSONResponse(status_code=400, content={"error": "请至少选择 2 条记忆"})
        if any(isinstance(memory_id, bool) or not isinstance(memory_id, int) for memory_id in memory_ids):
            return JSONResponse(status_code=400, content={"error": "ids 必须是整数数组"})
        if len(set(memory_ids)) != len(memory_ids):
            return JSONResponse(status_code=400, content={"error": "ids 不能重复"})

        memories_by_id = {
            memory["id"]: memory
            for memory in await db_memories.get_all_memories_detail(
                active_only=True,
                memory_ids=memory_ids,
            )
        }
        if any(memory_id not in memories_by_id for memory_id in memory_ids):
            return JSONResponse(status_code=400, content={"error": "只能整理仍然活跃的记忆"})
        memories = [memories_by_id[memory_id] for memory_id in memory_ids]
        blocked = [
            memory["id"] for memory in memories
            if memory.get("remind_at") is not None and memory.get("reminder_delivered_at") is None
        ]
        if blocked:
            return JSONResponse(
                status_code=400,
                content={"error": f"记忆 {blocked} 带有未送达的提醒，不能整理"},
            )
        label = f"已选 {len(memories)} 条记忆"

        async def build_preview():
            return await memory_consolidator.preview_memories(memories)
    else:
        try:
            if "date" in data and "start_date" not in data:
                start_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
                end_date = start_date
            else:
                start_date_str = data.get("start_date")
                end_date_str = data.get("end_date")
                if not start_date_str or not end_date_str:
                    return {"error": "请提供已选记忆，或开始和结束日期"}
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": "日期格式必须是 YYYY-MM-DD"})
        if start_date > end_date:
            return {"error": "开始日期不能晚于结束日期"}
        label = f"{start_date}~{end_date}"

        async def build_preview():
            return await memory_consolidator.preview_date_range(start_date, end_date)

    if not memory_extractor.MEMORY_MODEL:
        return {"error": "MEMORY_MODEL 未设置，请先在 Dashboard 设置记忆模型"}

    async def _run():
        try:
            _consolidate_status["result"] = await build_preview()
            print(f"[manual/consolidate] 草稿生成完成: {label}")
        except Exception as exc:
            logger.exception("Memory organization preview failed: %s", label)
            _consolidate_status["error"] = str(exc)
        finally:
            _consolidate_status["running"] = False

    _consolidate_status.update({
        "running": True,
        "started_at": label,
        "result": None,
        "error": None,
    })
    asyncio.create_task(_run())
    return {"status": "started", "started_at": label}


@router.get("/api/memories/consolidate/status")
async def api_consolidate_status():
    """查询整理任务状态"""
    return _consolidate_status


@router.get("/api/memories/core-candidates")
async def api_core_candidates():
    """Suggest active layer-2 memories for manual promotion."""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    events = await db_memories.get_core_candidate_memories(
        memory_consolidator.MIN_CORE_MERGED_SOURCES,
        memory_consolidator.MIN_CORE_IMPORTANCE,
        memory_consolidator.MAX_CORE_CANDIDATES,
    )
    candidates = memory_consolidator.select_core_candidates(events)
    tz_offset = timezone(timedelta(hours=shared.TIMEZONE_HOURS))
    for candidate in candidates:
        for key in ("created_at", "remind_at", "reminder_delivered_at"):
            if candidate.get(key):
                value = candidate[key]
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                candidate[key] = value.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
    return {"candidates": candidates, "total": len(candidates)}


@router.post("/api/memories/{memory_id}/promote")
async def api_promote_to_core(memory_id: int, request: Request):
    """将记忆升级为核心记忆"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    data = await request.json()
    title = data.get("title")

    await db_memories.promote_to_core(memory_id, title=title)
    return {"status": "ok", "memory_id": memory_id, "layer": 3}


@router.post("/api/memories/merge")
async def api_merge_memories(request: Request):
    """手动合并多条记忆"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    data = await request.json()
    memory_ids = data.get("ids", [])
    new_title = data.get("title", "")
    new_content = data.get("content", "")
    importance = data.get("importance", 5)
    layer = data.get("layer", 2)
    if not memory_ids or not new_content:
        return {"error": "请提供记忆ID列表和合并后内容"}

    try:
        new_id = await db_memories.merge_memories(
            memory_ids,
            new_title,
            new_content,
            importance,
            layer,
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"status": "ok", "new_id": new_id, "merged": len(memory_ids)}


@router.post("/api/memories/check-duplicate")
async def api_check_duplicate(request: Request):
    """检查记忆是否重复"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    data = await request.json()
    content = data.get("content", "")
    threshold = data.get("threshold", 0.7)

    if not content:
        return {"error": "请提供记忆内容"}

    result = await db_memories.check_duplicate_memory(content, threshold)
    return result


@router.post("/api/memories/cleanup-fragments")
async def api_cleanup_fragments(request: Request):
    """清理指定天数前的归档碎片

    Body:
        days: 清理多少天前的归档碎片（默认30天）
    """
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    data = await request.json()
    days = data.get("days", 30)

    try:
        result = await db_memories.cleanup_old_fragments(days)
        return {
            "status": "ok",
            "deleted": result["deleted"],
            "revert_disabled": result["revert_disabled"],
            "days": days,
        }
    except Exception:
        return shared._api_failure("清理归档碎片失败")


@router.post("/api/memories/{memory_id}/revert-merge")
async def api_revert_merge(memory_id: int):
    """撤回合并操作：恢复原始碎片，删除合并后的事件记忆"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    try:
        result = await db_memories.revert_merge(memory_id)
        return result
    except Exception:
        return shared._api_failure("撤回合并失败")


@router.post("/api/memories/{memory_id}/restore")
async def api_restore_memory(memory_id: int):
    """恢复已归档的记忆（将 is_active 设为 TRUE）"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    try:
        result = await db_memories.restore_archived_memory(memory_id)
        if result["status"] == "not_found":
            return JSONResponse(status_code=404, content={"error": "记忆不存在"})
        if result["status"] == "superseded":
            return JSONResponse(
                status_code=409,
                content={"error": "该记忆属于版本链，请使用撤销取代"},
            )
        return {"status": "ok", "id": memory_id}
    except Exception:
        return shared._api_failure("恢复记忆失败")


@router.post("/api/memories/{memory_id}/undo-supersede")
async def api_undo_memory_supersession(memory_id: int):
    """撤销自动取代：恢复旧记忆，保留后继记忆。"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    try:
        result = await db_memories.undo_memory_supersession(memory_id)
        if result["status"] == "not_found":
            return JSONResponse(status_code=404, content={"error": "记忆不存在"})
        if result["status"] == "not_superseded":
            return JSONResponse(
                status_code=409,
                content={"error": "该记忆没有可撤销的取代关系"},
            )
        return result
    except Exception:
        return shared._api_failure("撤销取代失败")


@router.get("/api/memories/layer-stats")
async def api_layer_statistics():
    """获取各层记忆统计数据"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    try:
        stats = await db_memories.get_layer_statistics()
        return stats
    except Exception:
        return shared._api_failure("加载记忆统计失败")


@router.post("/import/text")
async def import_text_memories(request: Request):
    """从纯文本导入记忆（每行一条），可选自动评分"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}

    try:
        data = await request.json()
        lines = data.get("lines", [])
        skip_scoring = data.get("skip_scoring", False)

        if not lines:
            return {"error": "没有找到记忆条目"}

        if skip_scoring:
            scored = [{"content": t, "importance": 5} for t in lines]
        else:
            if not memory_extractor.MEMORY_MODEL:
                return {"error": "MEMORY_MODEL 未设置，请先在 Dashboard 设置记忆模型，或选择跳过评分"}
            scored = await memory_extractor.score_memories(lines)

        imported = 0
        skipped = 0

        for mem in scored:
            content = mem.get("content", "")
            if not content:
                continue

            pool = await db_core.get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )

            if existing > 0:
                skipped += 1
                continue

            await db_memories.save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session="text-import",
            )
            imported += 1

        total = await db_memories.get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception:
        return shared._api_failure("导入记忆失败")


@router.post("/import/memories")
async def import_memories(request: Request):
    """从 JSON 导入记忆（用于迁移或恢复备份）"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}

    try:
        data = await request.json()
        memories = data.get("memories", [])

        if not memories:
            return {"error": "没有找到记忆数据，请确认 JSON 格式正确"}

        # v2+ 版本化备份；跨库导入不能沿用本地 conversations.id 来源。
        schema_version = data.get("schema_version")
        if schema_version in (2, 3, 4, 5):
            try:
                database_instance_id = (
                    await db_core.get_gateway_config("database_instance_id")
                    if schema_version >= 5
                    else None
                )
                return await db_memories.import_memories_v2(
                    memories,
                    schema_version=schema_version,
                    preserve_source_message_ids=(
                        schema_version >= 5
                        and bool(database_instance_id)
                        and data.get("source_database_id") == database_instance_id
                    ),
                )
            except ValueError as e:
                return {"error": f"备份校验失败：{e}"}

        # v1 旧格式（无 schema_version）：按碎片导入，尽量保留原 created_at
        imported = 0
        skipped = 0

        for mem in memories:
            content = mem.get("content", "")
            if not content:
                continue

            pool = await db_core.get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )

            if existing > 0:
                skipped += 1
                continue

            await db_memories.save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session=mem.get("source_session", "json-import"),
                created_at=db_memories._parse_backup_datetime(mem.get("created_at")),
            )
            imported += 1

        total = await db_memories.get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception:
        return shared._api_failure("导入记忆失败")


# ============================================================
# 记忆向量补算（带进度追踪）
# ============================================================

_backfill_mem_status = {
    "running": False,
    "total": 0,
    "done": 0,
    "error": None,
    "finished_at": None,
}

@maintenance_router.post("/api/admin/backfill-memory-embeddings")
async def api_backfill_memory_embeddings():
    """给已有记忆补算embedding（后台异步执行，前端轮询进度）"""
    if not shared.MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}

    try:
        return await start_memory_embedding_backfill()
    except Exception:
        return shared._api_failure("查询待处理数量失败")


async def start_memory_embedding_backfill():
    """在后台补算缺少向量的记忆；已有任务在跑时不重复启动。"""
    if not db_search.embedding_ready():
        return {"error": "向量来源或维度待确认，请先在 Dashboard 应用"}
    if _backfill_mem_status["running"]:
        return {"error": "补算任务正在运行中，请等待完成"}

    total = await db_memories.get_pending_memory_embedding_count()
    if total == 0:
        return {"status": "done", "message": "所有活跃记忆已有embedding，无需补算", "total": 0, "done": 0}

    _backfill_mem_status["running"] = True
    _backfill_mem_status["total"] = total
    _backfill_mem_status["done"] = 0
    _backfill_mem_status["error"] = None
    _backfill_mem_status["finished_at"] = None

    async def run_backfill():
        try:
            while _backfill_mem_status["running"]:
                updated = await db_memories.backfill_memory_embeddings(batch_size=20)
                _backfill_mem_status["done"] += updated

                if updated == 0:
                    break

                await asyncio.sleep(1)

            _backfill_mem_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✅ 记忆embedding补算完成：{_backfill_mem_status['done']}/{_backfill_mem_status['total']}")
        except Exception:
            logger.exception("Memory embedding backfill failed")
            _backfill_mem_status["error"] = "记忆向量补算失败"
        finally:
            _backfill_mem_status["running"] = False

    asyncio.create_task(run_backfill())
    return {"status": "started", "total": total}


@maintenance_router.get("/api/admin/backfill-memory-embeddings/status")
async def api_backfill_memory_embeddings_status():
    """查询记忆embedding补算进度"""
    try:
        counts = await db_memories.get_active_memory_embedding_counts()
    except Exception:
        logger.exception("Memory embedding counts unavailable")
        counts = {"cumulative_embedded": None, "remaining": None}
    return {
        "running": _backfill_mem_status["running"],
        "total": _backfill_mem_status["total"],
        "done": _backfill_mem_status["done"],
        "error": _backfill_mem_status["error"],
        "finished_at": _backfill_mem_status["finished_at"],
        **counts,
        "enabled": shared.MEMORY_ENABLED,
        "vector_enabled": shared.MEMORY_VECTOR_ENABLED,
        "key_configured": bool(shared.EMBEDDING_API_KEY),
        "embedding_ready": db_search.embedding_ready(),
        "embedding_error": db_search.embedding_status()["error"],
    }
