"""Tokenization, embeddings, recall search, and search backfill."""

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import List

from db import core as db_core

logger = logging.getLogger(__name__)

# ============================================================
# 中文分词工具（基于 jieba）
# ============================================================

import jieba
import jieba.analyse

import shared

# 静默加载词典
jieba.setLogLevel(jieba.logging.INFO)

EN_WORD_PATTERN = re.compile(r'[a-zA-Z][a-zA-Z0-9]*')
NUM_PATTERN = re.compile(r'\d{2,}')
# 清理查询开头的时间戳（如 "2026-05-02 20:26"）
TIMESTAMP_PATTERN = re.compile(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*\d{1,2}:\d{1,2}\s*')
# 清理查询结尾的 Cuplivo 时间戳（如 "(Sun 26-09-06 22:52:55)"）
CUPLIVO_TIMESTAMP_SUFFIX_PATTERN = re.compile(
    r'\s*\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\)\s*$'
)
_CONVERSATION_CANDIDATE_POOL = 20
_CONVERSATION_KEYWORD_LIMIT = 16
_CONVERSATION_SINGLE_CHARACTER_WEIGHT = 0.3

# 中文停用词（高频但无搜索价值的词）
_STOP_WORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
    "这", "那", "有", "和", "与", "也", "都", "又", "就", "但",
    "而", "或", "到", "被", "把", "让", "从", "对", "为", "以",
    "及", "等", "个", "不", "没", "很", "太", "吗", "呢", "吧",
    "啊", "嗯", "哦", "哈", "呀", "嘛", "么", "啦", "哇", "喔",
    "会", "能", "要", "想", "去", "来", "说", "做", "看", "给",
    "上", "下", "里", "中", "大", "小", "多", "少", "好", "可以",
    "什么", "怎么", "如何", "哪里", "哪个", "为什么", "还是",
    "然后", "因为", "所以", "虽然", "但是", "可以", "已经",
    "一个", "一些", "一下", "一点", "一起", "一样",
    "比较", "应该", "可能", "如果", "这个", "那个",
    "自己", "知道", "觉得", "感觉", "时候", "现在",
    "不是", "今天", "昨天", "明天", "这样", "其实", "记得",
})

# jieba 用户词典补充（默认词典缺失的词）
for _w in ["手账", "手帐", "搭子", "种草", "拔草", "安利", "内卷", "摆烂", "emo", "网关"]:
    jieba.add_word(_w)


def extract_search_keywords(query: str) -> List[str]:
    """
    从查询中提取搜索关键词（TF-IDF + 正则）

    1. 去掉明确的时间戳前后缀噪音
    2. 用 jieba.analyse.extract_tags (TF-IDF) 提取中文关键词
    3. 正则提取英文单词
    4. 保留4位以上数字（年份等，过滤短数字噪音）

    例如：
    "2026-05-02 20:26 写写手账看看书 放松大脑" → ["手账", "放松", "大脑"]
    "我昨天在手机上部署了Render然后吃了晚饭" → ["手机", "部署", "Render", "晚饭"]
    "春节干了什么" → ["春节"]
    "2026除夕"    → ["2026", "除夕"]
    """
    # 去掉明确的时间戳前后缀
    cleaned = CUPLIVO_TIMESTAMP_SUFFIX_PATTERN.sub('', TIMESTAMP_PATTERN.sub('', query)).strip()
    if not cleaned:
        cleaned = query

    keywords = set()

    # 英文单词（2字符以上）
    for match in EN_WORD_PATTERN.finditer(cleaned):
        word = match.group()
        if len(word) >= 2:
            keywords.add(word)

    # 数字串（只保留4位以上，过滤 "05" "20" 这种时间噪音）
    for match in NUM_PATTERN.finditer(cleaned):
        num = match.group()
        if len(num) >= 4:
            keywords.add(num)

    # TF-IDF 关键词提取（比手动分词+停用词好很多）
    tags = jieba.analyse.extract_tags(cleaned, topK=10)
    for tag in tags:
        # 跳过纯英文/数字（已在上面处理）
        if EN_WORD_PATTERN.fullmatch(tag) or NUM_PATTERN.fullmatch(tag):
            continue
        if tag in _STOP_WORDS:
            continue
        keywords.add(tag)

    return list(keywords)


def jieba_tokenize_for_tsv(text: str) -> str:
    """把文本转换为 PostgreSQL simple tsvector 的分词输入。"""
    if not text:
        return ""
    return " ".join(
        word.lower()
        for raw_word in jieba.cut(text, cut_all=False)
        if (word := raw_word.strip()) and word not in _STOP_WORDS
    )


def _conversation_query_terms(query: str) -> tuple[list[str], list[str]]:
    """返回普通分词和需要核对原文的完整词组。"""
    keywords = sorted(extract_search_keywords(query))
    lowered_query = query.lower()
    phrases = [
        phrase for phrase in shared.MEMORY_PHRASES
        if phrase.lower() in lowered_query
    ]
    if phrases:
        phrase_keys = {phrase.lower() for phrase in phrases}
        ordinary = [keyword for keyword in keywords if keyword.lower() not in phrase_keys]
        return ordinary, sorted(set(phrases))
    if keywords:
        return keywords, []

    fallback_phrases = []
    current = []
    for raw_word in jieba.cut(query, cut_all=False):
        word = raw_word.strip()
        if (
            len(word) == 1
            and "\u4e00" <= word <= "\u9fff"
            and word not in _STOP_WORDS
        ):
            current.append(word)
            continue
        if len(current) >= 2:
            fallback_phrases.append("".join(current))
        current = []
    if len(current) >= 2:
        fallback_phrases.append("".join(current))
    return [], sorted(set(fallback_phrases))


def _quote_tsquery_lexeme(token: str) -> str:
    return "'" + token.replace("\\", "\\\\").replace("'", "''") + "'"


def build_tsquery(query: str) -> str:
    """把对话搜索词编码成 tsquery；未知中文词组改走精确子串后备。"""
    tokens, exact_phrases = _conversation_query_terms(query)
    if exact_phrases:
        return ""
    escaped = [_quote_tsquery_lexeme(token) for token in tokens]
    return " & ".join(escaped)


# ============================================================
# 向量搜索（OpenAI 兼容 Embedding API）
# ============================================================

_embedding_source = None
_embedding_detection = None
_embedding_dimensions = {}
_embedding_state_error = None
embedding_write_lock = asyncio.Lock()


class _EmbeddingVector(list):
    def __init__(self, values, target):
        super().__init__(values)
        self.target = target


def _embedding_target():
    return [shared.EMBEDDING_BASE_URL.strip().rstrip("/"),
            shared.EMBEDDING_MODEL.strip(), shared.EMBEDDING_DIM]


def _active_detection():
    record = _embedding_detection
    target = _embedding_target()
    if not record or record.get("base_url") != target[0] or record.get("model") != target[1]:
        return None
    if (not record.get("no_dimensions")
            and record.get("requested_dim") != target[2]
            and record.get("dimension") != target[2]):
        return None
    return record


def embedding_ready() -> bool:
    target = _embedding_target()
    record = _active_detection()
    return (
        not _embedding_state_error
        and _embedding_source == target
        and all(dim == target[2] for dim in _embedding_dimensions.values())
        and (record is None or (not record.get("reprobe") and record["dimension"] == target[2]))
    )


def embedding_status() -> dict:
    record = _active_detection()
    return {
        "ready": embedding_ready(),
        "source": _embedding_source,
        "detection": record,
        "error": _embedding_state_error,
    }


def pause_embedding_state(reason: str):
    global _embedding_source, _embedding_detection, _embedding_dimensions
    global _embedding_state_error
    _embedding_source = None
    _embedding_detection = None
    _embedding_dimensions = {"unavailable": None}
    _embedding_state_error = reason
    _query_embed_cache.clear()


async def load_embedding_state():
    """Restore vector provenance after Dashboard settings have been loaded."""
    global _embedding_source, _embedding_detection, _embedding_dimensions
    global _embedding_state_error
    pause_embedding_state("向量状态加载中")
    raw_source = await db_core.get_gateway_config("embedding_vector_source")
    if raw_source:
        source = json.loads(raw_source)
    else:
        source = _embedding_target()
        await db_core.set_gateway_config(
            "embedding_vector_source", json.dumps(source)
        )
    raw_detection = await db_core.get_gateway_config("embedding_detection")
    detection = json.loads(raw_detection) if raw_detection else None
    dimensions = await db_core.get_vector_dimensions()
    _embedding_source = source
    _embedding_detection = detection
    _embedding_dimensions = dimensions
    if _embedding_detection and _active_detection() is None:
        await clear_embedding_detection()
    _embedding_state_error = None


async def clear_embedding_detection():
    """A retry only forgets provider capability, never vector provenance."""
    global _embedding_detection
    target = _embedding_target()
    marker = {
        "base_url": target[0], "model": target[1],
        "requested_dim": target[2], "dimension": 0,
        "no_dimensions": False, "reprobe": True,
    }
    _embedding_detection = marker
    _query_embed_cache.clear()
    await db_core.set_gateway_config("embedding_detection", json.dumps(marker))


async def sync_embedding_settings(result: dict):
    """Keep hot runtime state aligned with the committed Dashboard transaction."""
    global _embedding_source, _embedding_detection, _embedding_dimensions
    _embedding_source = result["source"]
    _embedding_dimensions = result["dimensions"]
    if _embedding_detection and _active_detection() is None:
        await clear_embedding_detection()
    _query_embed_cache.clear()


def _embedding_error_detail(resp) -> str:
    """只记录上游错误码和说明，不把任意响应体写进日志。"""
    try:
        payload = resp.json()
    except ValueError:
        return "未提供可解析的错误码和说明"
    if not isinstance(payload, dict):
        return "未提供可解析的错误码和说明"
    error = payload.get("error")
    if not isinstance(error, dict):
        error = payload
    code = error.get("code", payload.get("code"))
    message = error.get("message", payload.get("message"))
    parts = []
    if isinstance(code, (str, int)):
        parts.append(f"code={str(code)[:50]}")
    if isinstance(message, str):
        parts.append(f"message={' '.join(message.split())[:300]}")
    return " ".join(parts) or "未提供错误码和说明"


async def compute_embedding(text: str) -> list:
    """调用 OpenAI 兼容的 Embedding API 计算文本向量"""
    global _embedding_detection
    if not shared.EMBEDDING_API_KEY or _embedding_state_error:
        return []

    was_ready = embedding_ready()
    # Pending migration gets one real request to learn the provider's dimension.
    # Once learned, no more paid calls can produce unusable vectors.
    if not embedding_ready() and _active_detection() and not _active_detection().get("reprobe"):
        return []

    base_url = shared.EMBEDDING_BASE_URL.strip().rstrip("/")
    model = shared.EMBEDDING_MODEL.strip()
    dim = shared.EMBEDDING_DIM
    target = [base_url, model, dim]
    detection = _active_detection()
    dropped = ""
    try:
        import httpx

        if len(text) > 4000:
            text = text[:4000]

        body = {
            "model": model,
            "input": text,
        }
        send_dimensions = dim > 0 and not (detection and detection["no_dimensions"])
        if send_dimensions:
            body["dimensions"] = dim

        async with httpx.AsyncClient() as client:
            async def post():
                return await client.post(
                    f"{base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {shared.EMBEDDING_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=30.0,
                )

            resp = await post()
            if send_dimensions and resp.status_code in (400, 422):
                # A 400/422 can be a rejected dimension or an unsupported parameter.
                dropped = (
                    f"不接受 dimensions={dim}"
                    f"（HTTP {resp.status_code}: {_embedding_error_detail(resp)}）"
                )
                del body["dimensions"]
                resp = await post()
            if not resp.is_success:
                print(
                    f"⚠️ Embedding计算失败: HTTP {resp.status_code} {base_url}/embeddings "
                    f"model={model} {dropped + '；' if dropped else ''} "
                    f"响应: {_embedding_error_detail(resp)}"
                )
                return []
            vector = resp.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"⚠️ Embedding计算失败: {e}")
        return []

    if target != _embedding_target() or not isinstance(vector, list) or not vector:
        return []
    record = {
        "base_url": base_url,
        "model": model,
        "requested_dim": dim,
        "dimension": len(vector),
        "no_dimensions": bool(dropped or (detection and detection["no_dimensions"])),
    }
    if record != _embedding_detection:
        try:
            await db_core.set_gateway_config("embedding_detection", json.dumps(record))
        except Exception as e:
            print(f"⚠️ Embedding检测结果保存失败: {e}")
            return []
        _embedding_detection = record

    if dim > 0 and len(vector) != dim:
        print(
            f"⚠️ Embedding维度不一致: 模型 {model} "
            f"{dropped + '，省略后' if dropped else ''}返回 {len(vector)} 维，"
            f"当前 Embedding 维度设置为 {dim}。请在 Dashboard「向量搜索」里把"
            f"「Embedding 维度」改成 {len(vector)}"
        )
        return []
    if dropped:
        print(f"ℹ️ Embedding模型 {model} {dropped}，已自动省略该参数，后续请求不再发送")
    if not was_ready and embedding_ready():
        kick_embedding_backfill()
        if shared.MEMORY_ENABLED and shared.MEMORY_VECTOR_ENABLED:
            try:
                from routes.memories import start_memory_embedding_backfill
                await start_memory_embedding_backfill()
            except Exception:
                logger.exception("Memory embedding backfill failed to restart")
    return _EmbeddingVector(vector, tuple(target)) if embedding_ready() else []


# 搜索记忆与对话时复用同一条 query embedding。持久写入与 backfill 绕过缓存。
QUERY_EMBED_CACHE_TTL = float(os.getenv("QUERY_EMBED_CACHE_TTL", "5"))
QUERY_EMBED_CACHE_MAX = 128
_query_embed_cache = {}
_query_embed_inflight = {}
_query_embed_locks = {}


def _get_query_embed_lock():
    import asyncio

    loop = asyncio.get_running_loop()
    lock = _query_embed_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _query_embed_locks[loop] = lock
    return lock


async def _query_embed_worker(key, query: str) -> list:
    import time

    try:
        vector = await compute_embedding(query)
        if vector:
            if len(_query_embed_cache) >= QUERY_EMBED_CACHE_MAX:
                _query_embed_cache.pop(next(iter(_query_embed_cache)), None)
            _query_embed_cache[key] = (
                time.monotonic() + QUERY_EMBED_CACHE_TTL,
                tuple(vector),
            )
        return vector
    finally:
        _query_embed_inflight.pop(key, None)


async def get_query_embedding(query: str) -> list:
    """短窗口复用 query 向量；失败和空向量不进入缓存。"""
    import asyncio
    import time

    if not shared.EMBEDDING_API_KEY or _embedding_state_error:
        return []
    if not embedding_ready() and _active_detection() and not _active_detection().get("reprobe"):
        return []
    normalized_query = query.strip()
    if not normalized_query:
        return []

    key = (normalized_query, *_embedding_target())
    lock = _get_query_embed_lock()
    async with lock:
        hit = _query_embed_cache.get(key)
        if hit is not None:
            expires_at, vector = hit
            if time.monotonic() < expires_at:
                _query_embed_cache.pop(key, None)
                _query_embed_cache[key] = (expires_at, vector)
                return _EmbeddingVector(vector, key[1:])
            _query_embed_cache.pop(key, None)

        task = _query_embed_inflight.get(key)
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(
                _query_embed_worker(key, normalized_query)
            )
            _query_embed_inflight[key] = task

    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"⚠️ query向量共享任务失败: {e}")
        return []
    return (_EmbeddingVector(result, key[1:])
            if result and key[1:] == tuple(_embedding_target()) else [])


async def save_memory_embedding(conn, memory_id: int, embedding: list):
    """保存记忆向量到memories表"""
    async with embedding_write_lock:
        if (not embedding or not embedding_ready()
                or getattr(embedding, "target", tuple(_embedding_target())) != tuple(_embedding_target())):
            return

        if db_core.HAS_PGVECTOR:
            vec_str = '[' + ','.join(str(f) for f in embedding) + ']'
            await conn.execute(
                "UPDATE memories SET embedding = $1::vector WHERE id = $2",
                vec_str, memory_id
            )
        else:
            await conn.execute(
                "UPDATE memories SET embedding_json = $1 WHERE id = $2",
                json.dumps(embedding), memory_id
            )


async def save_conversation_embedding(conn, message_id: int, embedding: list):
    """保存单条原始对话向量。"""
    async with embedding_write_lock:
        if (not embedding or not embedding_ready()
                or getattr(embedding, "target", tuple(_embedding_target())) != tuple(_embedding_target())):
            return
        if db_core.HAS_PGVECTOR:
            vector_text = "[" + ",".join(str(value) for value in embedding) + "]"
            await conn.execute(
                """UPDATE conversations SET embedding = $1::vector
                   WHERE id = $2 AND deleted_at IS NULL""",
                vector_text,
                message_id,
            )
        else:
            await conn.execute(
                """UPDATE conversations SET embedding_json = $1
                   WHERE id = $2 AND deleted_at IS NULL""",
                json.dumps(embedding),
                message_id,
            )


def _cosine_sim(a, b):
    """余弦相似度（纯Python）"""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0
    return dot / (norm_a * norm_b)


def _min_max_normalize(scores: dict) -> dict:
    """min-max归一化到0-1"""
    if not scores:
        return {}
    vals = list(scores.values())
    min_v, max_v = min(vals), max(vals)
    spread = max_v - min_v
    if spread == 0:
        return {k: 0.0 if max_v == 0 else 1.0 for k in scores}
    return {k: (v - min_v) / spread for k, v in scores.items()}


# ============================================================
# 原始对话片段召回
# ============================================================

def _fragment_id(anchor_ids) -> str | None:
    """由命中消息的持久 conversations.id 生成稳定片段 ID。"""
    import hashlib

    ids = sorted({message_id for message_id in anchor_ids if message_id is not None})
    if not ids:
        return None
    payload = "chat-fragment:v1:" + ",".join(str(message_id) for message_id in ids)
    return f"v1:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _content_excerpt(content: str, max_chars: int, terms=()) -> str:
    if len(content) <= max_chars:
        return content
    lowered = content.lower()
    anchor = next(
        (
            (lowered.find(term.lower()), len(term))
            for term in terms
            if term and lowered.find(term.lower()) >= 0
        ),
        None,
    )
    if anchor is None:
        return content[:max_chars] + "…（省略）"
    position, term_chars = anchor
    start = max(0, position - max(0, max_chars - term_chars) // 2)
    end = min(len(content), start + max_chars)
    start = max(0, end - max_chars)
    return (
        ("…（省略）" if start else "")
        + content[start:end]
        + ("…（省略）" if end < len(content) else "")
    )


def _assemble_fragments(all_messages, sorted_indices, matched_indices, match_terms=()):
    fragments = []
    fragment_ids = []
    current = []
    current_anchors = []
    previous_index = -2

    for index in sorted_indices:
        if index != previous_index + 1 and current:
            fragments.append(current)
            fragment_ids.append(_fragment_id(current_anchors))
            current = []
            current_anchors = []

        message = all_messages[index]
        content = message["content"] or ""
        is_match = index in matched_indices
        max_chars = 200 if is_match else 80
        content = _content_excerpt(
            content, max_chars, match_terms if is_match else ()
        )
        created_at = message["created_at"]
        if created_at is not None and hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        current.append({
            "role": message["role"],
            "content": content,
            "created_at": created_at,
            "is_match": is_match,
        })
        if is_match:
            current_anchors.append(message["id"])
        previous_index = index

    if current:
        fragments.append(current)
        fragment_ids.append(_fragment_id(current_anchors))
    return fragments, fragment_ids


async def _conversation_tsv_ready(conn) -> bool:
    pending = await conn.fetchval(
        r"""SELECT COUNT(*) FROM conversations
            WHERE deleted_at IS NULL AND content_tsv IS NULL
              AND content IS NOT NULL AND content !~ '^\s*$'"""
    )
    return not pending


def _session_exclusion_sql(
    exclude_session_ids: list,
    param_index: int,
    include_session_before: tuple | None = None,
) -> tuple[str, list]:
    if not exclude_session_ids:
        return "", []
    if not include_session_before:
        return f" AND NOT (session_id = ANY(${param_index}::text[]))", [exclude_session_ids]
    session_id, created_at, message_id = include_session_before
    return (
        f""" AND (
                 NOT (session_id = ANY(${param_index}::text[]))
                 OR (session_id = ${param_index + 1}
                     AND (created_at, id) <= (
                         ${param_index + 2}::timestamptz,
                         ${param_index + 3}::integer
                     ))
               )""",
        [exclude_session_ids, session_id, created_at, message_id],
    )


def _keyword_weights(tokens, total, dfs):
    """Return the highest-IDF searchable terms, their weights, and denominator."""
    idf = [
        math.log((total + 1) / (df + 1)) + 1 if df > 0 else 0.0
        for df in dfs
    ]
    eligible = sorted(
        (index for index, df in enumerate(dfs) if df > 0),
        key=lambda index: (-idf[index], index),
    )
    kept = sorted(eligible[:_CONVERSATION_KEYWORD_LIMIT])
    if not kept:
        return [], [], 0.0
    has_multi_character_term = any(len(tokens[index]) > 1 for index in kept)
    weights = [
        idf[index] * (
            _CONVERSATION_SINGLE_CHARACTER_WEIGHT
            if has_multi_character_term and len(tokens[index]) == 1
            else 1.0
        )
        for index in kept
    ]
    return [tokens[index] for index in kept], weights, sum(weights)


def _keyword_score_ctes(kw, first: int, where_sql: str, use_tsv: bool,
                        exact_terms=()):
    """Build the shared per-message keyword score used by ranking and fragments."""
    kept, weights, denominator = kw
    if not kept:
        return (
            "kwt AS (SELECT NULL::text AS term, NULL::float8 AS weight WHERE false),\n"
            "hits AS (SELECT c.id, c.session_id, c.created_at, 0::float8 AS score "
            "FROM conversations c WHERE false)"
        ), []
    exact_keys = {term.lower() for term in exact_terms}
    exact_flags = [term.lower() in exact_keys for term in kept]
    kept_exact_terms = [
        term for term, exact in zip(kept, exact_flags) if exact
    ]
    if any(exact_flags):
        if use_tsv:
            terms = (
                f"kwt AS MATERIALIZED (SELECT t.term, t.weight, t.exact, "
                f"CASE WHEN t.exact THEN NULL "
                f"ELSE to_tsquery('simple', t.term) END AS query "
                f"FROM unnest(${first}::text[], ${first + 1}::float8[], "
                f"${first + 2}::boolean[]) AS t(term, weight, exact))"
            )
            ordinary_match = "c.content_tsv @@ k.query"
        else:
            terms = (
                f"kwt AS MATERIALIZED (SELECT t.term, t.weight, t.exact "
                f"FROM unnest(${first}::text[], ${first + 1}::float8[], "
                f"${first + 2}::boolean[]) AS t(term, weight, exact))"
            )
            ordinary_match = "c.content ILIKE '%' || k.term || '%'"
        branches = [
            f"SELECT c.id, c.session_id, c.created_at, k.weight "
            f"FROM conversations c JOIN kwt k "
            f"ON NOT k.exact AND {ordinary_match} WHERE {where_sql}"
        ]
        exact_match = "strpos(lower(c.content), lower(k.term)) > 0"
        if db_core.HAS_CONVERSATION_BIGRAM_INDEX:
            if any(len(term) >= 2 for term in kept_exact_terms):
                branches.append(
                    f"SELECT c.id, c.session_id, c.created_at, k.weight "
                    f"FROM conversations c JOIN kwt k ON k.exact "
                    f"AND char_length(k.term) >= 2 "
                    f"AND pawwake_text_bigrams_v1(c.content) "
                    f"@> pawwake_text_bigrams_v1(k.term) "
                    f"AND {exact_match} WHERE {where_sql}"
                )
            if any(len(term) < 2 for term in kept_exact_terms):
                branches.append(
                    f"SELECT c.id, c.session_id, c.created_at, k.weight "
                    f"FROM conversations c JOIN kwt k ON k.exact "
                    f"AND char_length(k.term) < 2 AND {exact_match} "
                    f"WHERE {where_sql}"
                )
        else:
            branches.append(
                f"SELECT c.id, c.session_id, c.created_at, k.weight "
                f"FROM conversations c JOIN kwt k ON k.exact "
                f"AND {exact_match} WHERE {where_sql}"
            )
        hits = (
            f"raw_hits AS ({' UNION ALL '.join(branches)}),\n"
            f"hits AS (SELECT id, session_id, created_at, "
            f"SUM(weight) / NULLIF(${first + 3}::float8, 0) AS score "
            f"FROM raw_hits GROUP BY id, session_id, created_at)"
        )
        encoded_terms = [
            term if exact else _quote_tsquery_lexeme(term)
            for term, exact in zip(kept, exact_flags)
        ] if use_tsv else list(kept)
        return f"{terms},\n{hits}", [
            encoded_terms,
            [float(weight) for weight in weights],
            exact_flags,
            float(denominator),
        ]

    if use_tsv:
        terms = (
            f"kwt AS MATERIALIZED (SELECT t.term, t.weight, "
            f"to_tsquery('simple', t.term) AS query "
            f"FROM unnest(${first}::text[], ${first + 1}::float8[]) "
            "AS t(term, weight))"
        )
        match = "c.content_tsv @@ k.query"
    else:
        terms = (
            f"kwt AS MATERIALIZED (SELECT t.term, t.weight "
            f"FROM unnest(${first}::text[], ${first + 1}::float8[]) "
            "AS t(term, weight))"
        )
        match = "c.content ILIKE '%' || k.term || '%'"
    hits = (
        f"hits AS (SELECT c.id, c.session_id, c.created_at, "
        f"SUM(k.weight) / NULLIF(${first + 2}::float8, 0) AS score "
        f"FROM conversations c JOIN kwt k ON {match} "
        f"WHERE {where_sql} GROUP BY c.id, c.session_id, c.created_at)"
    )
    query_terms = (
        [_quote_tsquery_lexeme(term) for term in kept]
        if use_tsv
        else list(kept)
    )
    return f"{terms},\n{hits}", [
        query_terms,
        [float(weight) for weight in weights],
        float(denominator),
    ]


async def _keyword_weights_for(conn, tokens: list[str], use_tsv: bool,
                               exact_terms=()):
    """Calculate IDF over active searchable conversation messages."""
    if not tokens:
        return [], [], 0.0
    exact_keys = {term.lower() for term in exact_terms}
    if use_tsv:
        searchable = "deleted_at IS NULL AND content_tsv IS NOT NULL"
    else:
        searchable = (
            "deleted_at IS NULL AND content IS NOT NULL "
            "AND content !~ '^\\s*$'"
        )
    matches = []
    query_terms = []
    for index, token in enumerate(tokens):
        param = f"${index + 1}"
        if token.lower() in exact_keys:
            exact_match = f"strpos(lower(content), lower({param})) > 0"
            if db_core.HAS_CONVERSATION_BIGRAM_INDEX and len(token) >= 2:
                exact_match = (
                    f"pawwake_text_bigrams_v1(content) "
                    f"@> pawwake_text_bigrams_v1({param}) AND {exact_match}"
                )
            matches.append(exact_match)
            query_terms.append(token)
        elif use_tsv:
            matches.append(f"content_tsv @@ to_tsquery('simple', {param})")
            query_terms.append(_quote_tsquery_lexeme(token))
        else:
            matches.append(f"content ILIKE '%' || {param} || '%'")
            query_terms.append(token)
    df_columns = ", ".join(
        f"(SELECT COUNT(*) FROM conversations WHERE {searchable} "
        f"AND {match}) AS df_{index}"
        for index, match in enumerate(matches)
    )
    rows = await conn.fetch(
        f"""SELECT (SELECT COUNT(*) FROM conversations
                    WHERE {searchable}) AS total,
                   {df_columns}""",
        *query_terms,
    )
    if not rows:
        return [], [], 0.0
    row = rows[0]
    return _keyword_weights(
        tokens,
        row["total"],
        [row[f"df_{index}"] for index in range(len(tokens))],
    )


async def _keyword_session_scores(conn, kw, pool_size: int,
                                  exclude_session_ids: list,
                                  include_session_before: tuple | None = None,
                                  use_tsv: bool = True,
                                  exact_terms=()):
    if not kw[0]:
        return {}

    exact_keys = {term.lower() for term in exact_terms}
    kept_exact_terms = [
        term for term in kw[0] if term.lower() in exact_keys
    ]
    exclusion_sql, exclusion_params = _session_exclusion_sql(
        exclude_session_ids,
        5 if kept_exact_terms else 4,
        include_session_before,
    )
    ctes, params = _keyword_score_ctes(
        kw,
        1,
        f"c.deleted_at IS NULL {exclusion_sql}",
        use_tsv,
        kept_exact_terms,
    )
    params.extend(exclusion_params)
    params.append(pool_size)
    rows = await conn.fetch(
        f"""WITH {ctes},
            best AS (
                SELECT DISTINCT ON (session_id) session_id, score,
                       created_at AS anchor_at,
                       MAX(created_at) OVER (PARTITION BY session_id) AS latest_match
                FROM hits
                WHERE score > 0
                ORDER BY session_id, score DESC, created_at DESC, id DESC
            )
            SELECT session_id, score, latest_match
            FROM best
            ORDER BY score DESC, anchor_at DESC, session_id
            LIMIT ${len(params)}""",
        *params,
    )
    return {
        row["session_id"]: {
            "score": float(row["score"]),
            "latest": row["latest_match"],
        }
        for row in rows
    }


async def _semantic_session_scores(conn, query_embedding: list, pool_size: int,
                                   exclude_session_ids: list,
                                   include_session_before: tuple | None = None):
    """先按原始余弦阈值过滤，再交给融合层归一化。"""
    if not query_embedding:
        return {}

    if db_core.HAS_PGVECTOR:
        vector_text = "[" + ",".join(str(value) for value in query_embedding) + "]"
        params = [vector_text]
        exclusion_sql, exclusion_params = _session_exclusion_sql(
            exclude_session_ids, len(params) + 1, include_session_before
        )
        params.extend(exclusion_params)
        ranked_limit = max(100, pool_size * 10)
        params.extend([ranked_limit, shared.CONVERSATION_MIN_SCORE_THRESHOLD, pool_size])
        ranked_limit_index = len(params) - 2
        threshold_index = len(params) - 1
        pool_index = len(params)
        rows = await conn.fetch(
            f"""WITH ranked AS (
                    SELECT session_id,
                           1 - (embedding <=> $1::vector) AS similarity,
                           created_at
                    FROM conversations
                    WHERE deleted_at IS NULL
                      AND embedding IS NOT NULL {exclusion_sql}
                    ORDER BY embedding <=> $1::vector
                    LIMIT ${ranked_limit_index}
                )
                SELECT session_id, MAX(similarity) AS score,
                       MAX(created_at) AS latest_match
                FROM ranked
                WHERE similarity >= ${threshold_index}
                GROUP BY session_id
                ORDER BY score DESC
                LIMIT ${pool_index}""",
            *params,
        )
        return {
            row["session_id"]: {
                "score": float(row["score"]),
                "latest": row["latest_match"],
            }
            for row in rows
        }

    params = []
    exclusion_sql, exclusion_params = _session_exclusion_sql(
        exclude_session_ids, 1, include_session_before
    )
    params.extend(exclusion_params)
    rows = await conn.fetch(
        f"""SELECT session_id, created_at, embedding_json
            FROM conversations
            WHERE deleted_at IS NULL
              AND embedding_json IS NOT NULL {exclusion_sql}""",
        *params,
    )
    session_best = {}
    for row in rows:
        try:
            similarity = _cosine_sim(query_embedding, json.loads(row["embedding_json"]))
        except Exception:
            continue
        if similarity < shared.CONVERSATION_MIN_SCORE_THRESHOLD:
            continue
        current = session_best.get(row["session_id"])
        if current is None or similarity > current["score"]:
            session_best[row["session_id"]] = {
                "score": similarity,
                "latest": row["created_at"],
            }
        elif row["created_at"] and row["created_at"] > current["latest"]:
            current["latest"] = row["created_at"]
    return dict(
        sorted(session_best.items(), key=lambda item: -item[1]["score"])[:pool_size]
    )


def _mark_conversation_message(message, keyword_score: float,
                               semantic_similarity: float, mode: str):
    """Mark a fragment anchor with the same keyword score used for session ranking."""
    threshold = shared.CONVERSATION_MIN_SCORE_THRESHOLD
    span = 1 - threshold
    semantic_part = (
        min(1.0, max(0.0, (semantic_similarity - threshold) / span))
        if span > 0
        else 0.0
    )
    semantic_match = mode == "hybrid" and semantic_similarity >= threshold
    is_match = keyword_score > 0 or semantic_match
    return {
        "id": message["id"],
        "is_match": is_match,
        "keyword_match": keyword_score > 0,
        "relevance": (
            shared.CONVERSATION_HW_KEYWORD * keyword_score
            + shared.CONVERSATION_HW_SEMANTIC * semantic_part
            if is_match
            else 0.0
        ),
    }


async def search_chat_fragments(
    query: str,
    max_sessions: int = 3,
    max_matches_per_session: int = 1,
    context: int = 1,
    mode: str = "hybrid",
    exclude_session_ids: list | None = None,
    exclude_fragment_ids: list | None = None,
    include_session_before: tuple | None = None,
    disjoint_fragments: bool = False,
    include_message_ids: bool = False,
):
    """检索历史对话。raw API 无状态，排除集合完全由调用方传入。"""
    from datetime import datetime, timezone

    if not shared.CONVERSATION_RECALL_ENABLED:
        return [], 0
    query = query.strip()
    if not query or mode not in {"keyword", "hybrid"}:
        return [], 0

    exclude_session_ids = sorted({str(value) for value in (exclude_session_ids or []) if value})
    excluded_fragments = {str(value) for value in (exclude_fragment_ids or []) if value}
    max_sessions = min(50, max(1, int(max_sessions)))
    max_matches_per_session = min(5, max(1, int(max_matches_per_session)))
    context = min(5, max(0, int(context)))

    ordinary_terms, exact_terms = _conversation_query_terms(query)
    keyword_terms = sorted(set(ordinary_terms + exact_terms))
    query_embedding = (
        await get_query_embedding(query)
        if mode == "hybrid" and shared.EMBEDDING_API_KEY
        else []
    )
    pool_size = max(_CONVERSATION_CANDIDATE_POOL, max_sessions * 3)
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        use_tsv = await _conversation_tsv_ready(conn)
        keyword_weights = await _keyword_weights_for(
            conn, keyword_terms, use_tsv, exact_terms
        )
        excerpt_terms = [
            term for term, _weight in sorted(
                zip(keyword_weights[0], keyword_weights[1]),
                key=lambda item: -item[1],
            )
        ]
        keyword_scores = await _keyword_session_scores(
            conn,
            keyword_weights,
            pool_size,
            exclude_session_ids,
            include_session_before=include_session_before,
            use_tsv=use_tsv,
            exact_terms=exact_terms,
        )
        semantic_scores = (
            await _semantic_session_scores(
                conn,
                query_embedding,
                pool_size,
                exclude_session_ids,
                include_session_before=include_session_before,
            )
            if mode == "hybrid"
            else {}
        )

    session_ids = set(keyword_scores) | set(semantic_scores)
    if not session_ids:
        return [], 0

    keyword_coverage = {
        session_id: item["score"] for session_id, item in keyword_scores.items()
    }
    semantic_normalized = _min_max_normalize({
        session_id: item["score"] for session_id, item in semantic_scores.items()
    })
    now = datetime.now(timezone.utc)
    recency = {}
    for session_id in session_ids:
        timestamps = [
            source[session_id]["latest"]
            for source in (keyword_scores, semantic_scores)
            if session_id in source and source[session_id]["latest"]
        ]
        if timestamps:
            age_days = (now - max(timestamps)).total_seconds() / 86400.0
            recency[session_id] = 1.0 / (1.0 + max(0.0, age_days))
        else:
            recency[session_id] = 0.0
    recency_normalized = _min_max_normalize(recency)

    if mode == "keyword":
        final_scores = keyword_coverage
    else:
        final_scores = {
            session_id: (
                shared.CONVERSATION_HW_KEYWORD * keyword_coverage.get(session_id, 0.0)
                + shared.CONVERSATION_HW_SEMANTIC * semantic_normalized.get(session_id, 0.0)
                + shared.CONVERSATION_HW_RECENCY * recency_normalized.get(session_id, 0.0)
            )
            for session_id in session_ids
        }
    ranked = sorted(final_scores.items(), key=lambda item: (-item[1], item[0]))

    vector_text = None
    if db_core.HAS_PGVECTOR and query_embedding:
        vector_text = "[" + ",".join(str(value) for value in query_embedding) + "]"
    plans = []
    async with pool.acquire() as conn:
        for offset in range(0, len(ranked), max_sessions):
            if len(plans) >= max_sessions:
                break
            batch = ranked[offset:offset + max_sessions]
            batch_session_ids = [session_id for session_id, _ in batch]
            keyword_ctes, keyword_args = _keyword_score_ctes(
                keyword_weights,
                2,
                "c.session_id = ANY($1::text[]) AND c.deleted_at IS NULL",
                use_tsv,
                exact_terms,
            )
            params = [batch_session_ids, *keyword_args]
            if db_core.HAS_PGVECTOR and vector_text:
                params.append(vector_text)
                detail_column = f"""CASE WHEN c.embedding IS NOT NULL
                                          THEN 1 - (c.embedding <=> ${len(params)}::vector)
                                          ELSE 0 END AS sem_sim"""
            else:
                embedding_column = (
                    "c.embedding_json" if not db_core.HAS_PGVECTOR else "NULL::text"
                )
                detail_column = f"{embedding_column} AS embedding_json"
            prefix_sql = ""
            if include_session_before:
                params.extend(include_session_before)
                prefix_sql = (
                    f" AND (c.session_id <> ${len(params) - 2} "
                    f"OR (c.created_at, c.id) <= "
                    f"(${len(params) - 1}::timestamptz, ${len(params)}::integer))"
                )
            messages = await conn.fetch(
                f"""WITH {keyword_ctes}
                    SELECT c.session_id, c.id,
                           COALESCE(h.score, 0) AS keyword_score,
                           {detail_column}
                    FROM conversations c LEFT JOIN hits h ON h.id = c.id
                    WHERE c.session_id = ANY($1::text[]) AND c.deleted_at IS NULL
                          {prefix_sql}
                    ORDER BY c.session_id, c.created_at ASC, c.id ASC""",
                *params,
            )
            messages_by_session = {}
            for message in messages:
                messages_by_session.setdefault(message["session_id"], []).append(message)

            for session_id, final_score in batch:
                if len(plans) >= max_sessions:
                    break
                marked = []
                for message in messages_by_session.get(session_id, []):
                    keyword_score = float(message["keyword_score"] or 0.0)
                    semantic_similarity = 0.0
                    if db_core.HAS_PGVECTOR and vector_text:
                        semantic_similarity = float(message["sem_sim"] or 0)
                    elif query_embedding and message["embedding_json"]:
                        try:
                            semantic_similarity = _cosine_sim(
                                query_embedding, json.loads(message["embedding_json"])
                            )
                        except Exception:
                            semantic_similarity = 0.0
                    marked.append(_mark_conversation_message(
                        message,
                        keyword_score,
                        semantic_similarity,
                        mode,
                    ))

                match_candidates = [
                    (index, item["relevance"])
                    for index, item in enumerate(marked)
                    if item["is_match"]
                ]
                match_candidates.sort(key=lambda item: -item[1])
                if not match_candidates:
                    continue
                total_matched = len(match_candidates)
                kept = []
                full_content_ids = set()
                used_context_indices = set()
                for match_index, _ in match_candidates:
                    context_indices = set(range(
                        max(0, match_index - context),
                        min(len(marked), match_index + context + 1),
                    ))
                    if disjoint_fragments and used_context_indices & context_indices:
                        continue
                    fragment_id = _fragment_id([marked[match_index]["id"]])
                    if not fragment_id or fragment_id in excluded_fragments:
                        continue
                    kept.append((
                        sorted(context_indices),
                        {match_index},
                        fragment_id,
                        [marked[index]["id"] for index in sorted(context_indices)],
                    ))
                    if marked[match_index]["keyword_match"]:
                        full_content_ids.add(marked[match_index]["id"])
                    used_context_indices.update(context_indices)
                    if len(kept) >= max_matches_per_session:
                        break
                if not kept:
                    continue
                plans.append({
                    "session_id": session_id,
                    "title": session_id,
                    "total_messages": len(marked),
                    "match_count": total_matched,
                    "kept": kept,
                    "full_content_ids": full_content_ids,
                    "has_more_matches": total_matched > len(kept),
                    "hybrid_scores": {
                        "kw_raw": round(
                            keyword_scores.get(session_id, {}).get("score", 0.0), 6
                        ),
                        "kw": round(keyword_coverage.get(session_id, 0.0), 3),
                        "sem_raw": round(
                            semantic_scores.get(session_id, {}).get("score", 0.0), 6
                        ),
                        "sem": round(semantic_normalized.get(session_id, 0.0), 3),
                        "rec": round(recency_normalized.get(session_id, 0.0), 3),
                        "final": round(final_score, 3),
                    },
                })

        needed_ids = sorted({
            message_id
            for plan in plans
            for _, _, _, message_ids in plan["kept"]
            for message_id in message_ids
        })
        full_content_ids = sorted({
            message_id
            for plan in plans
            for message_id in plan["full_content_ids"]
        })
        detail_rows = await conn.fetch(
            """SELECT id, role,
                      CASE WHEN id = ANY($2::int[])
                           THEN content ELSE LEFT(content, 201) END AS content,
                      created_at
               FROM conversations
               WHERE id = ANY($1::int[]) AND deleted_at IS NULL""",
            needed_ids,
            full_content_ids,
        ) if needed_ids else []
        details = {row["id"]: row for row in detail_rows}

    results = []
    for plan in plans:
        plan.pop("full_content_ids", None)
        selected_ids = [
            message_id
            for _, _, _, message_ids in plan["kept"]
            for message_id in message_ids
        ]
        if any(message_id not in details for message_id in selected_ids):
            continue
        fragments = []
        fragment_ids = []
        fragment_message_ids = []
        for indices, matched_indices, fragment_id, message_ids in plan.pop("kept"):
            messages = {
                index: details[message_id]
                for index, message_id in zip(indices, message_ids)
            }
            piece_fragments, _ = _assemble_fragments(
                messages, indices, matched_indices, excerpt_terms
            )
            fragments.extend(piece_fragments)
            fragment_ids.append(fragment_id)
            fragment_message_ids.append(message_ids)
        plan["fragments"] = fragments
        plan["fragment_ids"] = fragment_ids
        if include_message_ids:
            plan["fragment_message_ids"] = fragment_message_ids
        results.append(plan)

    return results, len(results)


# ============================================================
# 对话检索索引与向量持续补算
# ============================================================

async def rebuild_content_tsv(batch_size: int = 200):
    """以 content_tsv IS NULL 为持久账本，分批补齐关键词索引。"""
    if not shared.CONVERSATION_RECALL_ENABLED:
        return 0
    pool = await db_core.get_pool()
    total_updated = 0
    last_id = 0
    while True:
        if not shared.CONVERSATION_RECALL_ENABLED:
            break
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                r"""SELECT id, content FROM conversations
                    WHERE deleted_at IS NULL AND content_tsv IS NULL AND id > $2
                      AND content IS NOT NULL AND content !~ '^\s*$'
                    ORDER BY id
                    LIMIT $1""",
                batch_size, last_id,
            )
        if not rows:
            break
        last_id = rows[-1]["id"]
        row_ids = [row["id"] for row in rows]
        tsv_texts = [jieba_tokenize_for_tsv(row["content"] or "") for row in rows]
        if not shared.CONVERSATION_RECALL_ENABLED:
            break
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE conversations AS c
                   SET content_tsv = array_to_tsvector(
                       string_to_array(batch.tsv_text, ' ')
                   )
                   FROM UNNEST($1::int[], $2::text[]) AS batch(id, tsv_text)
                   WHERE c.id = batch.id AND c.deleted_at IS NULL
                     AND c.content_tsv IS NULL""",
                row_ids, tsv_texts,
            )
        total_updated += len(rows)
    return total_updated


EMBED_BACKFILL_SLEEP = float(os.getenv("EMBED_BACKFILL_SLEEP", "0.7"))
EMBED_BACKFILL_FAIL_LIMIT = int(os.getenv("EMBED_BACKFILL_FAIL_LIMIT", "10"))
EMBED_BACKFILL_BATCH = int(os.getenv("EMBED_BACKFILL_BATCH", "50"))

_embed_backfill_task = None
_embed_backfill_rerun = False
_embed_backfill_state = {
    "running": False,
    "done_count": 0,
    "fail_count": 0,
    "last_error": None,
    "stopped_reason": None,
    "last_run_at": None,
}


def _conversation_embedding_pending_condition() -> str:
    column = "embedding" if db_core.HAS_PGVECTOR else "embedding_json"
    return rf"{column} IS NULL AND content IS NOT NULL AND content !~ '^\s*$'"


async def backfill_conversation_embeddings_once(
    sleep_seconds: float | None = None,
    fail_limit: int | None = None,
):
    """补算非空 NULL 向量；失败项保持 NULL，下一次可续跑。"""
    import asyncio as _asyncio

    state = _embed_backfill_state
    state.update({
        "running": True,
        "done_count": 0,
        "fail_count": 0,
        "last_error": None,
        "stopped_reason": None,
    })
    if sleep_seconds is None:
        sleep_seconds = EMBED_BACKFILL_SLEEP
    if fail_limit is None:
        fail_limit = EMBED_BACKFILL_FAIL_LIMIT

    try:
        if not shared.CONVERSATION_RECALL_ENABLED:
            state["stopped_reason"] = "recall_disabled"
            return 0, 0, None
        if not embedding_ready():
            state["stopped_reason"] = "embedding_migration_pending"
            return 0, 0, None
        if not shared.EMBEDDING_API_KEY:
            state["last_error"] = "EMBEDDING_API_KEY未设置"
            state["stopped_reason"] = "no_api_key"
            return 0, 0, state["last_error"]

        pool = await db_core.get_pool()
        condition = _conversation_embedding_pending_condition()
        consecutive_failures = 0
        last_id = 0
        while True:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""SELECT id, content FROM conversations
                        WHERE deleted_at IS NULL AND {condition} AND id > $2
                        ORDER BY id
                        LIMIT $1""",
                    EMBED_BACKFILL_BATCH, last_id,
                )
            if not rows:
                break
            last_id = rows[-1]["id"]

            for row in rows:
                if not shared.CONVERSATION_RECALL_ENABLED:
                    state["stopped_reason"] = "recall_disabled"
                    return state["done_count"], state["fail_count"], state["last_error"]
                if not embedding_ready():
                    state["stopped_reason"] = "embedding_migration_pending"
                    return state["done_count"], state["fail_count"], state["last_error"]
                try:
                    vector = await compute_embedding(row["content"] or "")
                except Exception as exc:
                    vector = []
                    state["last_error"] = str(exc)

                if vector:
                    try:
                        async with pool.acquire() as conn:
                            await save_conversation_embedding(conn, row["id"], vector)
                        state["done_count"] += 1
                        consecutive_failures = 0
                    except Exception as exc:
                        state["fail_count"] += 1
                        consecutive_failures += 1
                        state["last_error"] = f"写回失败 id={row['id']}: {exc}"
                else:
                    state["fail_count"] += 1
                    consecutive_failures += 1
                    state["last_error"] = f"embedding计算返回空 id={row['id']}"

                if consecutive_failures >= fail_limit:
                    state["stopped_reason"] = f"连续失败{consecutive_failures}条，本轮停止"
                    return state["done_count"], state["fail_count"], state["last_error"]
                if sleep_seconds > 0:
                    await _asyncio.sleep(sleep_seconds)

        return state["done_count"], state["fail_count"], state["last_error"]
    finally:
        state["running"] = False
        state["last_run_at"] = datetime.now(dt_timezone.utc).isoformat()


async def _conversation_embedding_backfill_runner():
    global _embed_backfill_rerun

    try:
        while True:
            _embed_backfill_rerun = False
            await backfill_conversation_embeddings_once()
            if not _embed_backfill_rerun or _embed_backfill_state["stopped_reason"]:
                break
    except Exception as exc:
        _embed_backfill_state["last_error"] = str(exc)
        _embed_backfill_state["running"] = False


def kick_embedding_backfill() -> bool:
    """单实例唤醒补算器；运行中只登记再跑一轮。"""
    global _embed_backfill_task, _embed_backfill_rerun
    import asyncio as _asyncio

    if (not shared.CONVERSATION_RECALL_ENABLED or not shared.EMBEDDING_API_KEY
            or not embedding_ready()):
        return False
    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _embed_backfill_task is not None and not _embed_backfill_task.done():
        _embed_backfill_rerun = True
        return False
    _embed_backfill_task = loop.create_task(_conversation_embedding_backfill_runner())
    return True


async def get_embedding_backfill_status():
    remaining = None
    cumulative_embedded = None
    content_tsv_remaining = None
    try:
        pool = await db_core.get_pool()
        async with pool.acquire() as conn:
            remaining = await conn.fetchval(
                f"""SELECT COUNT(*) FROM conversations
                    WHERE deleted_at IS NULL
                      AND {_conversation_embedding_pending_condition()}"""
            )
            embedding_column = "embedding" if db_core.HAS_PGVECTOR else "embedding_json"
            cumulative_embedded = await conn.fetchval(
                f"""SELECT COUNT(*) FROM conversations
                    WHERE deleted_at IS NULL AND {embedding_column} IS NOT NULL"""
            )
            content_tsv_remaining = await conn.fetchval(
                r"""SELECT COUNT(*) FROM conversations
                    WHERE deleted_at IS NULL AND content_tsv IS NULL
                      AND content IS NOT NULL AND content !~ '^\s*$'"""
            )
    except Exception as exc:
        if not _embed_backfill_state["last_error"]:
            _embed_backfill_state["last_error"] = f"查询补算状态失败: {exc}"

    return {
        "enabled": shared.CONVERSATION_RECALL_ENABLED,
        "running": _embed_backfill_state["running"],
        "last_run_done_count": _embed_backfill_state["done_count"],
        "fail_count": _embed_backfill_state["fail_count"],
        "last_error": _embed_backfill_state["last_error"],
        "stopped_reason": _embed_backfill_state["stopped_reason"],
        "last_run_at": _embed_backfill_state["last_run_at"],
        "remaining": remaining,
        "cumulative_embedded": cumulative_embedded,
        "content_tsv_remaining": content_tsv_remaining,
    }
