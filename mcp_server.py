"""Official MCP transport for Pawwake memory tools."""

from datetime import date, datetime

from mcp.server.mcpserver import MCPServer

import shared
from db import memories as db_memories


server = MCPServer(
    "Pawwake",
    title="Pawwake Memory",
    description="Search and manage Pawwake memories.",
    version="4.2.1",
)


def _require_memory_enabled():
    if not shared.DATABASE_ENABLED:
        raise ValueError("数据库未启用")
    if not shared.MEMORY_ENABLED:
        raise ValueError("记忆系统未启用")


def _memory_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("memory_id 必须是正整数")
    return value


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit 必须是 1 到 100 的整数")
    return value


def _serialize_memory(memory) -> dict:
    result = dict(memory)
    for key, value in result.items():
        if isinstance(value, (date, datetime)):
            result[key] = value.isoformat()
    return result


@server.tool(
    description=(
        "添加一条 Pawwake 记忆。记忆默认进入碎片层且不会自动设为常驻；"
        "如需常驻，请再调用 pin_memory。"
    )
)
async def add_memory(
    content: str,
    importance: int = 5,
    title: str | None = None,
    layer: int = 1,
    event_date: str | None = None,
    external_id: str | None = None,
) -> dict:
    _require_memory_enabled()
    content = content.strip()
    if not content:
        raise ValueError("content 不能为空")
    if (
        isinstance(importance, bool)
        or not isinstance(importance, int)
        or not 1 <= importance <= 10
    ):
        raise ValueError("importance 必须是 1 到 10 的整数")
    if isinstance(layer, bool) or not isinstance(layer, int) or layer not in (1, 2, 3):
        raise ValueError("layer 必须是 1、2 或 3")
    title = (title or "").strip() or None
    external_id = (external_id or "").strip() or None
    parsed_event_date = None
    if event_date:
        try:
            parsed_event_date = date.fromisoformat(event_date)
        except ValueError as exc:
            raise ValueError("event_date 必须是有效的 YYYY-MM-DD 日期") from exc

    memory_id = await db_memories.save_memory(
        content=content,
        importance=importance,
        source_session="mcp",
        title=title,
        layer=layer,
        event_date=parsed_event_date,
        external_id=external_id,
    )
    if memory_id is None:
        existing = (
            await db_memories.get_memory_by_external_id(external_id)
            if external_id
            else None
        )
        if existing:
            return {"status": "exists", "memory": _serialize_memory(existing)}
        raise RuntimeError("添加记忆失败")

    return {
        "status": "created",
        "memory": {
            "id": memory_id,
            "content": content,
            "importance": importance,
            "title": title,
            "layer": layer,
            "event_date": parsed_event_date.isoformat() if parsed_event_date else None,
            "external_id": external_id,
            "is_active": True,
            "is_pinned": False,
        },
    }


@server.tool(description="搜索活跃 Pawwake 记忆，返回本次实际使用的搜索模式。")
async def search_memories(query: str, limit: int = 10) -> dict:
    _require_memory_enabled()
    query = query.strip()
    if not query:
        raise ValueError("query 不能为空")
    search_output = await db_memories.search_memories(
        query, _limit(limit), include_mode=True
    )
    results = search_output["results"]
    mode = search_output["mode"]
    return {
        "status": "ok",
        "mode": mode,
        "memories": [_serialize_memory(memory) for memory in results],
    }


@server.tool(
    description=(
        "列出最近记忆。默认只列活跃记忆；include_archived=true 时也列出已归档记忆，"
        "便于找到可恢复的 memory_id。"
    )
)
async def list_recent_memories(
    limit: int = 20,
    include_archived: bool = False,
) -> dict:
    _require_memory_enabled()
    memories = await db_memories.get_recent_memories(
        _limit(limit),
        include_archived=include_archived,
    )
    return {
        "status": "ok",
        "memories": [_serialize_memory(memory) for memory in memories],
    }


@server.tool(description="更新一条 Pawwake 记忆的正文、重要度、标题或层级。")
async def update_memory(
    memory_id: int,
    content: str | None = None,
    importance: int | None = None,
    title: str | None = None,
    layer: int | None = None,
) -> dict:
    _require_memory_enabled()
    memory_id = _memory_id(memory_id)
    if content is not None:
        content = content.strip()
        if not content:
            raise ValueError("content 不能为空")
    if importance is not None and (
        isinstance(importance, bool)
        or not isinstance(importance, int)
        or not 1 <= importance <= 10
    ):
        raise ValueError("importance 必须是 1 到 10 的整数")
    if layer is not None and (
        isinstance(layer, bool)
        or not isinstance(layer, int)
        or layer not in (1, 2, 3)
    ):
        raise ValueError("layer 必须是 1、2 或 3")
    if title is not None:
        title = title.strip()
    if all(value is None for value in (content, importance, title, layer)):
        raise ValueError("至少提供一个要更新的字段")

    updated = await db_memories.update_memory_with_layer(
        memory_id,
        content=content,
        importance=importance,
        title=title,
        layer=layer,
    )
    if not updated:
        raise ValueError("记忆不存在")
    return {"status": "updated", "id": memory_id}


@server.tool(
    description=(
        "删除一条 Pawwake 记忆。此工具只做可恢复的软删除：记忆会被移入归档并立即停止召回；"
        "不会永久删除，可用 restore_memory 恢复。"
    )
)
async def delete_memory(memory_id: int) -> dict:
    _require_memory_enabled()
    memory_id = _memory_id(memory_id)
    deleted = await db_memories.update_memory_with_layer(memory_id, is_active=False)
    if not deleted:
        raise ValueError("记忆不存在")
    return {
        "status": "archived",
        "id": memory_id,
        "message": "已删除并移入归档；不会再被召回，可用 restore_memory 恢复。",
    }


@server.tool(
    description=(
        "恢复一条已归档记忆。若它保留常驻标记，恢复时会重新检查 4000 字常驻预算。"
    )
)
async def restore_memory(memory_id: int) -> dict:
    _require_memory_enabled()
    memory_id = _memory_id(memory_id)
    result = await db_memories.restore_archived_memory(memory_id)
    if result["status"] == "not_found":
        raise ValueError("记忆不存在")
    if result["status"] == "superseded":
        raise ValueError("该记忆属于版本链，请在 Dashboard 撤销取代")
    return {"status": "restored", "id": memory_id}


@server.tool(
    description=(
        "将一条记忆设为常驻。活跃常驻正文总计最多 4000 字；常驻记忆会占用每轮记忆注入名额。"
    )
)
async def pin_memory(memory_id: int) -> dict:
    _require_memory_enabled()
    memory_id = _memory_id(memory_id)
    pinned = await db_memories.update_memory_with_layer(memory_id, is_pinned=True)
    if not pinned:
        raise ValueError("记忆不存在或已归档")
    return {"status": "pinned", "id": memory_id}


@server.tool(description="取消一条记忆的常驻标记。记忆本身仍保持活跃或归档状态。")
async def unpin_memory(memory_id: int) -> dict:
    _require_memory_enabled()
    memory_id = _memory_id(memory_id)
    unpinned = await db_memories.update_memory_with_layer(memory_id, is_pinned=False)
    if not unpinned:
        raise ValueError("记忆不存在")
    return {"status": "unpinned", "id": memory_id}


http_app = server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)
