"""Core OpenAI-compatible chat routes."""

import asyncio
import json
import logging
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

import shared
import partition_engine
import memory_pipeline
from db import core as db_core
from db import search as db_search
from db import conversations as db_conversations
from db import memories as db_memories

router = APIRouter()
logger = logging.getLogger(__name__)


class _ReminderLeaseKeeper:
    """发送期间延长到期提醒的处理时限。

    停止时先等正在更新的时间落库，避免结算使用旧时间而无法标记送达。
    """

    def __init__(self, pending_reminder_claims):
        self.claims = pending_reminder_claims
        self._task = None
        self._inflight = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def _renew_once(self):
        renewed = await db_memories.renew_reminder_claims(list(self.claims))
        self.claims[:] = renewed

    async def _loop(self):
        while True:
            await asyncio.sleep(memory_pipeline.REMINDER_LEASE_RENEW_SECONDS)
            self._inflight = asyncio.ensure_future(self._renew_once())
            try:
                await asyncio.shield(self._inflight)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"⚠️ 提醒处理时限延长失败: {e}")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            try:
                await inflight
            except Exception as e:
                print(f"⚠️ 提醒处理时限延长失败: {e}")

    @property
    def done(self) -> bool:
        return self._task is None or self._task.done()


def _start_reminder_lease_keeper(pending_reminder_claims):
    """没有待处理提醒时不启动延时。"""
    if not pending_reminder_claims:
        return None
    return _ReminderLeaseKeeper(pending_reminder_claims).start()


async def _stop_reminder_lease_keeper(keeper):
    """停稳延时循环，确保结算拿到最新的待处理提醒。"""
    if keeper is not None:
        await keeper.stop()


async def _release_reminder_claims(pending_reminder_claims):
    """上游失败时交回到期提醒，让下次请求立即重试。"""
    if pending_reminder_claims:
        try:
            await db_memories.release_reminder_claims(list(pending_reminder_claims))
            pending_reminder_claims.clear()
        except Exception as e:
            print(f"⚠️ 提醒处理权交回失败，等处理时限结束后自动重试: {e}")


async def _settle_reminder_claims(lease_keeper, pending_reminder_claims, delivered_ok: bool):
    """请求结束时标记已送达提醒；未完整送达的交回给下次请求。"""
    await _stop_reminder_lease_keeper(lease_keeper)
    if not pending_reminder_claims:
        return
    if delivered_ok:
        try:
            delivered = await db_memories.mark_reminders_delivered(list(pending_reminder_claims))
        except Exception as e:
            print(f"⚠️ 提醒送达标记失败，交回后等下次请求重试: {e}")
            delivered = 0
        print(f"⏰ 提醒送达 {delivered}/{len(pending_reminder_claims)} 条")
        if delivered == len(pending_reminder_claims):
            pending_reminder_claims.clear()
            return
        print("⚠️ 提醒送达数不足，未送达项交回等待重试")
    await _release_reminder_claims(pending_reminder_claims)


async def _close_stream_and_release(stream, pending_reminder_claims):
    """响应任务退出后（自然结束、客户端断开、建好响应却没开始消费）显式关掉生成器，
    让它的 finally 立即运行；生成器未启动或收尾被取消时，这里交回剩余提醒。
    自然结束时两步都是空操作。"""
    try:
        await stream.aclose()
    except Exception as e:
        print(f"⚠️ 关闭流式生成器失败: {e}")
    finally:
        await _release_reminder_claims(pending_reminder_claims)


def _streaming_response(stream, pending_reminder_claims) -> StreamingResponse:
    """流式响应统一在这里建。Starlette 收到 http.disconnect 只取消发送任务，不会替我们关 body iterator，
    所以挂 BackgroundTask 在响应任务退出后显式 aclose 并交回剩余提醒。"""
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        background=BackgroundTask(_close_stream_and_release, stream, pending_reminder_claims),
    )


async def _mark_pending_seen(session_id, pending_fragment_ids, pending_memory_ids):
    if pending_fragment_ids:
        try:
            await db_conversations.mark_fragments_seen(
                session_id,
                pending_fragment_ids,
                shared.CONVERSATION_SEEN_TTL_HOURS,
            )
        except Exception as e:
            print(f"⚠️ 对话召回 seen 写入失败，保留待下次重试: {e}")

    if pending_memory_ids:
        try:
            await db_conversations.mark_memories_seen(
                session_id,
                pending_memory_ids,
                shared.MEMORY_SEEN_TTL_HOURS,
            )
        except Exception as e:
            print(f"⚠️ 记忆注入 seen 写入失败，保留待下次重试: {e}")

# ============================================================
# API 接口
# ============================================================

@router.get("/")
async def health_check():
    """健康检查"""
    resolved_system_prompt = await shared.get_system_prompt()
    memory_count = 0
    if shared.MEMORY_ENABLED:
        try:
            memory_count = await db_memories.get_all_memories_count()
        except:
            pass

    return {
        "status": "running",
        "gateway": "Pawwake v4.1.8",
        "system_prompt_loaded": len(resolved_system_prompt) > 0,
        "system_prompt_length": len(resolved_system_prompt),
        "database_enabled": shared.DATABASE_ENABLED,
        "conversation_persistence_enabled": shared.conversation_persistence_enabled(),
        "memory_enabled": shared.MEMORY_ENABLED,
        "memory_source_dedupe_enabled": shared.MEMORY_SOURCE_DEDUPE_ENABLED,
        "cache_partition_enabled": shared.CACHE_PARTITION_ENABLED,
        "conversation_recall_enabled": shared.CONVERSATION_RECALL_ENABLED,
        "memory_count": memory_count,
        "memory_extract_interval": shared.MEMORY_EXTRACT_INTERVAL,
    }


@router.get("/v1/models")
async def list_models():
    """模型列表（让客户端不报错）"""
    models = []
    if shared.DEFAULT_MODEL:
        models.append({
            "id": shared.DEFAULT_MODEL,
            "object": "model",
            "created": 1700000000,
            "owned_by": "pawwake",
        })
    return {
        "object": "list",
        "data": models,
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """核心转发接口"""
    if not shared.API_KEY:
        return JSONResponse(
            status_code=500,
            content={"error": "API_KEY 未设置，请先在 Dashboard 的基础连接中配置"},
        )

    # 到期提醒由这条路由负责兜底：里层抛异常或非流式返回时仍未结清，就在这里交回；
    # 流式由生成器的 finally 结算
    pending_reminder_claims = []
    try:
        response = await _chat_completions_inner(request, pending_reminder_claims)
    except Exception:
        logger.exception("Chat completion gateway failed")
        await _release_reminder_claims(pending_reminder_claims)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Gateway internal error", "type": "gateway_error"}},
        )
    if pending_reminder_claims and not isinstance(response, StreamingResponse):
        await _release_reminder_claims(pending_reminder_claims)
    return response


async def _chat_completions_inner(request: Request, pending_reminder_claims: list = None):
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model") or shared.DEFAULT_MODEL
    if not model:
        return JSONResponse(
            status_code=400,
            content={"error": "模型未设置，请在请求中指定 model，或先在 Dashboard 设置默认模型"},
        )
    body["model"] = model
    pending_fragment_ids = []
    pending_memory_ids = []
    if pending_reminder_claims is None:
        pending_reminder_claims = []

    # ---------- 检测是否应跳过对话存储 ----------
    # 优先尊重客户端显式声明；无法加 header 的客户端则识别其标题生成模板。
    explicit_skip = request.headers.get("X-Skip-Conversation-Log", "").lower() == "true"
    auxiliary_title_request = partition_engine._is_title_generation_request(messages)
    skip_conversation_log = explicit_skip or auxiliary_title_request
    if auxiliary_title_request:
        print("⏭️  检测到标题生成请求：跳过分区缓存、记忆注入、对话存储和会话 Token 统计")

    # ---------- 提取用户最新消息 ----------
    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_message = content
            elif isinstance(content, list):
                user_message = " ".join(
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            break

    # ---------- 构建 system prompt ----------
    # 先保存原始对话消息（不含 system prompt），用于记忆提取
    original_messages = [dict(msg) for msg in messages if msg.get("role") != "system"]
    extraction_context_messages = original_messages
    extraction_round_count = None
    resolved_system_prompt = "" if skip_conversation_log else await shared.get_system_prompt()

    # ---------- 检测工具调用消息 ----------
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    if tool_messages:
        print(f"🔧 检测到 {len(tool_messages)} 条工具结果消息")

    # ---------- 生成 session ID ----------
    session_id = str(uuid.uuid4())[:8]

    # ---------- 分区缓存模式 ----------
    if shared.CACHE_PARTITION_ENABLED and not skip_conversation_log:
        active_sid = shared.get_active_session_id()
        if active_sid:
            session_id = active_sid

        # 从DB读取历史
        try:
            db_history = await db_conversations.get_conversation_messages(session_id, limit=10000)
            db_msgs = []
            recall_history = []
            db_extraction_msgs = []
            for m in (db_history or []):
                msg = db_conversations.db_row_to_message(m)
                msg['created_at'] = m.get('created_at')  # 保留时间戳供分区时间窗口判断
                db_msgs.append(msg)
                recall_history.append({**msg, "id": m.get("id")})
                db_extraction_msgs.append({**msg, "_source_message_id": m.get("id")})
        except Exception as e:
            print(f"❌ 分区缓存不可用：读取对话历史失败: {e}")
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "分区缓存需要数据库，当前无法读取对话历史。请检查数据库连接或暂时关闭 CACHE_PARTITION_ENABLED。",
                        "type": "partition_database_unavailable",
                    }
                },
            )

        # 提取客户端新消息（非system），可能是user、tool、或带tool_calls的assistant
        client_new_msgs = [m for m in messages if m.get("role") != "system"]
        # 分区模式下，assistant消息来自上一轮response（DB里已存），过滤掉避免重复
        client_new_msgs = [m for m in client_new_msgs if m.get("role") != "assistant"]
        # 分区模式下DB已有完整历史，客户端发来的旧user是冗余的。
        # 但有些客户端把图片和文字拆成多条连续的user发送（图在前文字在后），
        # 只留最后一条会把图那条当冗余丢掉（图片不入库，DB里也找不回来）。
        # 所以按原始消息顺序保留"末尾连续的user块"：历史冗余user总是被assistant隔开，不会混入。
        tail_user_ids = set()
        for m in reversed([m for m in messages if m.get("role") != "system"]):
            if m.get("role") == "user":
                tail_user_ids.add(id(m))
            else:
                break
        user_msgs = [m for m in client_new_msgs if m.get("role") == "user"]
        if len(user_msgs) > len(tail_user_ids):
            client_new_msgs = [
                m for m in client_new_msgs
                if m.get("role") != "user" or id(m) in tail_user_ids
            ]
            print(f"🔧 去重: 过滤{len(user_msgs)-len(tail_user_ids)}条冗余user，保留末尾连续{len(tail_user_ids)}条")
        # 工具结果轮次处理：基于DB状态 + 当前轮次tool_call_id精确判断
        client_tools = [m for m in client_new_msgs if m.get("role") == "tool"]
        if client_tools:
            # 判断DB是否处于"等待tool结果"状态（最后一条是assistant(tool_calls)）
            db_last = db_msgs[-1] if db_msgs else None
            db_expecting_tool = (db_last and db_last.get("role") == "assistant" and db_last.get("tool_calls"))

            if not db_expecting_tool:
                # DB不在等待tool结果 → 客户端的所有tool都是历史残留（含手动删除后的幽灵）
                stale_ids = [m.get('tool_call_id', '?') for m in client_tools]
                print(f"🔧 去重: DB未在等待tool结果，丢弃{len(client_tools)}条客户端tool (ids: {stale_ids})")
                client_new_msgs = [m for m in client_new_msgs if m.get("role") != "tool"]
            else:
                # DB在等待tool → 只保留匹配当前轮次assistant(tool_calls)的tool
                expected_tool_ids = {tc.get("id") for tc in db_last.get("tool_calls", []) if tc.get("id")}
                new_tools = [m for m in client_tools if m.get("tool_call_id") in expected_tool_ids]
                stale_tools = [m for m in client_tools if m.get("tool_call_id") not in expected_tool_ids]

                if stale_tools:
                    print(f"🔧 去重: 丢弃{len(stale_tools)}条非当前轮次tool (ids: {[m.get('tool_call_id','?') for m in stale_tools]})")
                if new_tools:
                    print(f"🔧 保留{len(new_tools)}条当前轮次tool (ids: {[m.get('tool_call_id','?') for m in new_tools]})")

                # 重建 client_new_msgs（user此时已只剩末尾连续块，全部保回，别把拆条发送的图丢了）
                tail_users = [m for m in client_new_msgs if m.get("role") == "user"]
                client_new_msgs = new_tools[:] + tail_users

                if new_tools:
                    # Race condition 防护：DB的assistant(tool_calls)已确认存在（db_expecting_tool=True），
                    # 但仍需检查是否被其他并发请求意外清除
                    new_tool_ids = {m.get("tool_call_id") for m in new_tools if m.get("tool_call_id")}
                    db_has_matching_ast = False
                    for m in db_msgs:
                        if m.get("role") == "assistant" and m.get("tool_calls"):
                            ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                            if new_tool_ids & ast_tc_ids:
                                db_has_matching_ast = True
                                break
                    if not db_has_matching_ast and new_tool_ids:
                        for m in messages:
                            if m.get("role") == "assistant" and m.get("tool_calls"):
                                ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                                if new_tool_ids & ast_tc_ids:
                                    client_new_msgs.insert(0, m)
                                    print(f"⚠️ Race防护: 从客户端补充assistant(tool_calls)")
                                    break
        all_msgs = db_msgs + client_new_msgs
        extraction_context_messages = db_extraction_msgs + [dict(m) for m in client_new_msgs]
        extraction_round_count = len(partition_engine.group_by_rounds(all_msgs))

        # 同步更新tool_messages，避免process_memories_background存重复的旧tool
        tool_messages = [m for m in client_new_msgs if m.get("role") == "tool"]

        print(f"📦 分区模式: DB历史{len(db_msgs)}条 + 客户端消息{len(client_new_msgs)}条")

        partition_prompt = resolved_system_prompt
        if shared.memory_injection_enabled():
            partition_prompt = (resolved_system_prompt or "") + partition_engine.MEMORY_USAGE_GUIDE
        # 保留客户端自带的 system（工具说明等），拼接到网关 prompt 之后，
        # 与非分区路径的行为对齐（前端 system 稳定时不影响 BP1 缓存命中）
        client_system_text = partition_engine._extract_client_system_text(messages)
        if client_system_text:
            partition_prompt = ((partition_prompt or "") + "\n\n" + client_system_text).strip()
        try:
            recall_result = await memory_pipeline.build_conversation_recall_text(
                user_message,
                session_id,
                recall_history,
            )
            conversation_recall_text, pending_fragment_ids = recall_result[:2]
            recalled_message_ids = recall_result[2] if len(recall_result) > 2 else []

            async def build_session_memory_text(message: str):
                return await memory_pipeline.build_memory_text(
                    message,
                    session_id,
                    pending_memory_ids,
                    pending_reminder_claims,
                    recalled_message_ids,
                )

            messages = await partition_engine.build_partitioned_messages(
                session_id,
                all_msgs,
                partition_prompt,
                user_message,
                conversation_recall_text,
                build_session_memory_text,
            )
        except Exception as e:
            print(f"❌ 分区缓存不可用：读取轮转状态失败: {e}")
            await _release_reminder_claims(pending_reminder_claims)
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "分区缓存需要数据库，当前无法读取轮转状态。请检查数据库连接或暂时关闭 CACHE_PARTITION_ENABLED。",
                        "type": "partition_database_unavailable",
                    }
                },
            )
        body["messages"] = messages

    else:
        # ---------- 原有逻辑：system prompt + 记忆注入 ----------
        if not skip_conversation_log and (resolved_system_prompt or (shared.MEMORY_ENABLED and user_message)):
            if shared.MEMORY_ENABLED and user_message:
                enhanced_prompt = await memory_pipeline.build_system_prompt_with_memories(
                    user_message,
                    resolved_system_prompt,
                    pending_reminder_claims,
                )
            else:
                enhanced_prompt = resolved_system_prompt

            if enhanced_prompt:
                has_system = any(msg.get("role") == "system" for msg in messages)
                if has_system:
                    for i, msg in enumerate(messages):
                        if msg.get("role") == "system":
                            messages[i]["content"] = enhanced_prompt + "\n\n" + msg["content"]
                            break
                else:
                    messages.insert(0, {"role": "system", "content": enhanced_prompt})

        body["messages"] = messages

    # ---------- cache_control 兼容性处理 ----------
    if shared.CACHE_PARTITION_ENABLED and not partition_engine._is_anthropic_model(model):
        partition_engine._strip_cache_control(body.get("messages", []))

    # ---------- 转发请求 ----------
    headers = {
        "Authorization": f"Bearer {shared.API_KEY}",
        "Content-Type": "application/json",
    }
    # OpenRouter 需要的额外头
    if "openrouter" in shared.API_BASE_URL:
        headers["HTTP-Referer"] = shared.EXTRA_REFERER
        headers["X-Title"] = shared.EXTRA_TITLE

    is_stream = body.get("stream", False)

    # 强制流式传输（解决部分客户端不发stream=true的问题）
    if shared.FORCE_STREAM and not is_stream:
        is_stream = True
        body["stream"] = True
        print(f"⚡ 强制开启流式传输（FORCE_STREAM=true）")

    # 注入推理参数（解决客户端走网关时不带reasoning参数的问题）
    if shared.REASONING_EFFORT and not skip_conversation_log:
        # 统一用 reasoning_effort（Claude/OpenAI/Google Gemini OpenAI兼容端点都支持）
        # 先删除客户端可能已带的值，确保用我们配置的
        body.pop("reasoning_effort", None)
        body.pop("google", None)
        body["reasoning_effort"] = shared.REASONING_EFFORT
        print(f"🧠 注入推理参数: reasoning_effort={shared.REASONING_EFFORT}")

    print(f"📡 请求: model={model}, stream={is_stream}, memory={'on' if shared.MEMORY_ENABLED else 'off'}", flush=True)

    # 调试：打印请求体中的推理相关字段
    debug_keys = {k: v for k, v in body.items() if k in ('reasoning_effort', 'google', 'reasoning')}
    if debug_keys:
        print(f"📡 推理字段: {debug_keys}", flush=True)

    if is_stream:
        stream = stream_and_capture(
            headers,
            body,
            session_id,
            user_message,
            model,
            extraction_context_messages,
            skip_conversation_log,
            tool_messages,
            pending_fragment_ids,
            extraction_round_count,
            pending_memory_ids,
            pending_reminder_claims,
        )
        return _streaming_response(stream, pending_reminder_claims)
    else:
        # 非流式发送期间延长提醒处理时限；退出时标记送达或交回等待重试
        delivered_ok = False
        lease_keeper = _start_reminder_lease_keeper(pending_reminder_claims)
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(shared.API_BASE_URL, headers=headers, json=body)

                if response.status_code == 200:
                    resp_data = response.json()
                    assistant_msg = ""
                    assistant_tool_calls = None
                    assistant_reasoning = None
                    try:
                        msg_obj = resp_data["choices"][0]["message"]
                        assistant_msg = msg_obj.get("content") or ""
                        if msg_obj.get("tool_calls"):
                            assistant_tool_calls = msg_obj["tool_calls"]
                            print(f"🔧 Response 包含 {len(assistant_tool_calls)} 个工具调用")
                        if msg_obj.get("reasoning_content"):
                            assistant_reasoning = msg_obj["reasoning_content"]
                            print(f"🧠 Response 包含 reasoning_content ({len(assistant_reasoning)}字符)")
                    except (KeyError, IndexError):
                        pass

                    await _mark_pending_seen(session_id, pending_fragment_ids, pending_memory_ids)
                    delivered_ok = True

                    if shared.conversation_persistence_enabled() and (user_message or tool_messages):
                        asyncio.create_task(
                            memory_pipeline.process_memories_background(session_id, user_message, assistant_msg, model,
                                                        context_messages=extraction_context_messages,
                                                        context_round_count=extraction_round_count,
                                                        skip_conversation_log=skip_conversation_log,
                                                        tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                                        assistant_reasoning=assistant_reasoning)
                        )

                    return JSONResponse(status_code=200, content=resp_data)
                else:
                    try:
                        error_content = response.json()
                    except Exception:
                        error_content = {"error": {"message": response.text[:500], "type": "upstream_error"}}
                    return JSONResponse(status_code=response.status_code, content=error_content)
        finally:
            await _settle_reminder_claims(lease_keeper, pending_reminder_claims, delivered_ok)


async def stream_and_capture(
    headers: dict,
    body: dict,
    session_id: str,
    user_message: str,
    model: str,
    extraction_context_messages: list = None,
    skip_conversation_log: bool = False,
    tool_messages: list = None,
    pending_fragment_ids: list = None,
    extraction_round_count: int = None,
    pending_memory_ids: list = None,
    pending_reminder_claims: list = None,
):
    """流式发送期间延长提醒处理时限；结束时标记送达或交回等待重试。"""
    stream_state = {"terminal": False}
    lease_keeper = _start_reminder_lease_keeper(pending_reminder_claims)
    try:
        async for chunk in _stream_and_capture_inner(
            headers,
            body,
            session_id,
            user_message,
            model,
            extraction_context_messages,
            skip_conversation_log,
            tool_messages,
            pending_fragment_ids,
            extraction_round_count,
            pending_memory_ids,
            pending_reminder_claims,
            stream_state,
        ):
            yield chunk
    finally:
        await _settle_reminder_claims(lease_keeper, pending_reminder_claims, stream_state["terminal"])


async def _stream_and_capture_inner(
    headers: dict,
    body: dict,
    session_id: str,
    user_message: str,
    model: str,
    extraction_context_messages: list = None,
    skip_conversation_log: bool = False,
    tool_messages: list = None,
    pending_fragment_ids: list = None,
    extraction_round_count: int = None,
    pending_memory_ids: list = None,
    pending_reminder_claims: list = None,
    stream_state: dict = None,
):
    """流式响应 + 捕获完整回复（原始字节透传，确保SSE格式和thinking数据完整）"""
    full_response = []
    full_reasoning = []
    stream_usage = {}
    line_buffer = ""
    accumulated_tool_calls = {}  # index -> OpenAI-compatible tool call
    stream_succeeded = False
    saw_terminal = False  # 见到 data: [DONE] 或 finish_reason 才算 SSE 完整结束

    def _mark_terminal():
        nonlocal saw_terminal
        saw_terminal = True
        if stream_state is not None:
            stream_state["terminal"] = True

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", shared.API_BASE_URL, headers=headers, json=body) as response:
            # 打印上游响应头（排查thinking问题用）
            upstream_ct = response.headers.get("content-type", "")
            print(f"📨 上游响应: status={response.status_code}, content-type={upstream_ct}", flush=True)

            # 上游非200时，提前打印messages结构方便debug
            if response.status_code != 200:
                msg_summary = [{"role": m.get("role"), "tool_calls": bool(m.get("tool_calls")), "tool_call_id": m.get("tool_call_id", ""), "content_type": type(m.get("content")).__name__} for m in body.get("messages", [])]
                print(f"❌ 发送的messages结构({len(msg_summary)}条): {msg_summary}", flush=True)

            error_body_parts = []
            is_error = response.status_code != 200

            async for chunk in response.aiter_bytes():
                if is_error:
                    error_body_parts.append(chunk)
                    yield chunk
                    continue

                # 旁路解析先于透传：客户端收到 [DONE] 后可能立刻断开、生成器随即被关，
                # 终止信号要在 yield 之前登记，外壳才能据此结算送达
                text = chunk.decode("utf-8", errors="ignore")
                line_buffer += text
                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    line = line.strip()
                    if line == "data: [DONE]":
                        _mark_terminal()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            data = json.loads(line[6:])

                            if "usage" in data:
                                stream_usage = data["usage"]

                            first_choice = data.get("choices", [{}])[0] if data.get("choices") else {}
                            if first_choice.get("finish_reason"):
                                _mark_terminal()
                            delta = first_choice.get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_response.append(content)

                            # 收集reasoning_content（deepseek thinking mode）
                            reasoning = delta.get("reasoning_content", "")
                            if reasoning:
                                full_reasoning.append(reasoning)

                            # 累积tool_calls
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    if idx not in accumulated_tool_calls:
                                        accumulated_tool_calls[idx] = {
                                            "index": idx,
                                            "id": tc.get("id", ""),
                                            "type": tc.get("type", "function"),
                                            "function": {"name": "", "arguments": ""}
                                        }
                                    if tc.get("id"):
                                        accumulated_tool_calls[idx]["id"] = tc["id"]
                                    for key, value in tc.items():
                                        if key not in {"index", "id", "type", "function"}:
                                            accumulated_tool_calls[idx][key] = value
                                    if "function" in tc:
                                        fn = tc["function"]
                                        if fn.get("name"):
                                            accumulated_tool_calls[idx]["function"]["name"] = fn["name"]
                                        if "arguments" in fn:
                                            accumulated_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                                        for key, value in fn.items():
                                            if key not in {"name", "arguments"}:
                                                accumulated_tool_calls[idx]["function"][key] = value
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
                # 原始字节直接透传给客户端
                yield chunk
            stream_succeeded = response.status_code == 200

    assistant_msg = "".join(full_response)
    assistant_reasoning = "".join(full_reasoning) if full_reasoning else None
    assistant_tool_calls = list(accumulated_tool_calls.values()) if accumulated_tool_calls else None

    if assistant_reasoning:
        print(f"🧠 Stream response 包含 reasoning_content ({len(assistant_reasoning)}字符)")

    # 打印上游错误内容
    if error_body_parts:
        error_text = b"".join(error_body_parts).decode("utf-8", errors="ignore")[:500]
        print(f"❌ 上游错误内容: {error_text}", flush=True)

    if assistant_tool_calls:
        print(f"🔧 Stream response 包含 {len(assistant_tool_calls)} 个工具调用")

    if stream_succeeded:
        # seen 账沿用 200 即成功的旧口径；提醒送达账由外壳按终止信号统一结算
        await _mark_pending_seen(session_id, pending_fragment_ids, pending_memory_ids)
        if pending_reminder_claims and not saw_terminal:
            print("⚠️ 流式 200 但未见到 [DONE]/finish_reason，提醒不记送达并交回")

    if stream_usage:
        pt = stream_usage.get("prompt_tokens", 0)
        ct = stream_usage.get("completion_tokens", 0)
        tt = stream_usage.get("total_tokens", 0)
        if shared.DATABASE_ENABLED and tt > 0 and not skip_conversation_log:
            asyncio.create_task(db_conversations.save_token_usage(session_id, model, pt, ct, tt))
            print(f"📊 Stream Token: {pt} + {ct} = {tt}")

    if shared.conversation_persistence_enabled() and (user_message or tool_messages):
        asyncio.create_task(
            memory_pipeline.process_memories_background(session_id, user_message, assistant_msg, model,
                                        context_messages=extraction_context_messages,
                                        context_round_count=extraction_round_count,
                                        skip_conversation_log=skip_conversation_log,
                                        tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                        assistant_reasoning=assistant_reasoning)
        )
