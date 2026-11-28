"""Memory persistence, search, backup, and lifecycle operations."""

import json
import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone

import shared
from db import core as db_core
from db import search as db_search
from db.core import BrokenMergeReferencesError, BrokenSupersessionReferencesError

logger = logging.getLogger(__name__)

_EXTRACTION_RELEVANT_LIMIT = 10
_EXTRACTION_RECENT_LIMIT = 10
_MEMORY_PHRASE_BONUS = 0.15
PINNED_MEMORY_BUDGET_CHARS = 4000


async def _check_pinned_budget(
    conn,
    memory_id: int,
    *,
    content=None,
    is_pinned=None,
    is_active=None,
):
    """Serialize active-pinned writes and reject a result over the fixed budget."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('pawwake_pinned_memory_budget'))"
    )
    row = await conn.fetchrow(
        """SELECT id, content, is_pinned, is_active
           FROM memories WHERE id = $1 FOR UPDATE""",
        memory_id,
    )
    if not row:
        raise ValueError("记忆不存在")
    target_content = row["content"] if content is None else content
    target_pinned = row["is_pinned"] if is_pinned is None else is_pinned
    target_active = row["is_active"] if is_active is None else is_active
    if target_pinned and target_active:
        used = await conn.fetchval(
            """SELECT COALESCE(SUM(char_length(content)), 0)
               FROM memories
               WHERE is_pinned = TRUE AND is_active = TRUE AND id <> $1""",
            memory_id,
        )
        if used + len(target_content) > PINNED_MEMORY_BUDGET_CHARS:
            raise ValueError(
                f"活跃常驻记忆超过 {PINNED_MEMORY_BUDGET_CHARS} 字预算"
            )
    return row

# ============================================================
# 记忆操作
# ============================================================

async def save_memory(content: str, importance: int = 5, source_session: str = "",
                      created_at: datetime = None, title: str = None,
                      layer: int = 1, event_date=None, external_id: str = None):
    """created_at 传入时保留原时间（备份恢复用），否则落库默认 NOW()"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO memories "
            "(content, importance, source_session, created_at, title, layer, "
            "event_date, external_id, is_active) "
            "VALUES ($1, $2, $3, COALESCE($4, NOW()), $5, $6, $7, $8, TRUE) "
            "ON CONFLICT (external_id) WHERE external_id IS NOT NULL "
            "DO NOTHING RETURNING id",
            content, importance, source_session, created_at, title, layer,
            event_date, external_id,
        )

        # MEMORY_VECTOR_ENABLED 时自动计算 embedding
        if shared.MEMORY_VECTOR_ENABLED and row:
            try:
                embedding = await db_search.compute_embedding(content)
                if embedding:
                    await db_search.save_memory_embedding(conn, row['id'], embedding)
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding自动计算失败: {e}")
        return row["id"] if row else None


async def save_extracted_memory(
    content: str,
    importance: int,
    source_session: str,
    source_message_ids=None,
    supersede_id=None,
    candidate_ids=None,
    remind_at=None,
):
    """Save one extracted fact and atomically retire an allowed active predecessor."""
    allowed_ids = {
        memory_id for memory_id in (candidate_ids or [])
        if isinstance(memory_id, int) and not isinstance(memory_id, bool)
    }
    requested_id = (
        supersede_id
        if isinstance(supersede_id, int) and not isinstance(supersede_id, bool)
        else None
    )

    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO memories
                    (content, importance, source_session, layer, is_active, remind_at,
                     source_message_ids, source_content_intact)
                VALUES ($1, $2, $3, 1, TRUE, $4, $5, $5::integer[] IS NOT NULL)
                RETURNING id
                """,
                content,
                importance,
                source_session,
                remind_at,
                source_message_ids,
            )
            new_id = int(row["id"])
            retired_id = None
            if requested_id in allowed_ids:
                predecessor = await conn.fetchrow(
                    """
                    SELECT id, is_active, superseded_by, layer, is_pinned
                    FROM memories
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    requested_id,
                )
                predecessor_layer = (predecessor.get("layer") if predecessor else None) or 1
                if (
                    predecessor
                    and predecessor["is_active"] is True
                    and predecessor["superseded_by"] is None
                    and (predecessor_layer != 1 or predecessor["is_pinned"] is True)
                ):
                    # 后台提取只允许接管普通碎片；事件、核心和常驻记忆不在这里静默归档，
                    # 新事实照常按碎片保存，旧行保持 active，冲突留给整理流程处理。
                    print(
                        f"🛡️ 记忆 {requested_id}（layer {predecessor_layer}"
                        f"{'，常驻' if predecessor['is_pinned'] else ''}）拒绝后台自动取代，"
                        f"新事实已按碎片保存为 {new_id}，旧记忆保持活跃"
                    )
                elif (
                    predecessor
                    and predecessor["is_active"] is True
                    and predecessor["superseded_by"] is None
                ):
                    result = await conn.execute(
                        """
                        UPDATE memories
                        SET is_active = FALSE, superseded_by = $2
                        WHERE id = $1
                          AND is_active = TRUE
                          AND superseded_by IS NULL
                        """,
                        requested_id,
                        new_id,
                    )
                    if result == "UPDATE 1":
                        retired_id = requested_id

    if shared.MEMORY_VECTOR_ENABLED:
        try:
            embedding = await db_search.compute_embedding(content)
            if embedding:
                async with pool.acquire() as conn:
                    await db_search.save_memory_embedding(conn, new_id, embedding)
        except Exception as exc:
            print(f"⚠️ 记忆 {new_id} embedding自动计算失败: {exc}")

    return {
        "id": new_id,
        "action": "supersede" if retired_id is not None else "new",
        "superseded_id": retired_id,
    }


async def get_memory_by_external_id(external_id: str):
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, content, importance, title, layer, event_date,
                   source_session, external_id, is_active, is_pinned, created_at
            FROM memories
            WHERE external_id = $1
            """,
            external_id,
        )
    return dict(row) if row else None


async def claim_due_reminders(lease_seconds: float) -> list:
    """取出全部到期未送达的提醒，并确保同一条只交给一个请求处理。

    处理超时或进程崩溃后，提醒可由下一次请求重新处理。不设条数上限。
    返回行含 reminder_claimed_at，调用方用 (id, claimed_at) 延长处理时限、标记送达或交回。
    """
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE memories SET reminder_claimed_at = NOW()
            WHERE id IN (
                SELECT id FROM memories
                WHERE is_active = TRUE
                  AND remind_at IS NOT NULL AND remind_at <= NOW()
                  AND reminder_delivered_at IS NULL
                  AND (reminder_claimed_at IS NULL
                       OR reminder_claimed_at < NOW() - make_interval(secs => $1))
                ORDER BY remind_at ASC, importance DESC
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, content, importance, created_at, event_date, remind_at, reminder_claimed_at
            """,
            float(lease_seconds),
        )
    return [dict(row) for row in rows]


def _normalize_reminder_claims(claims) -> list:
    return [
        (int(memory_id), claimed_at)
        for memory_id, claimed_at in (claims or [])
        if isinstance(memory_id, int) and not isinstance(memory_id, bool) and claimed_at is not None
    ]


async def mark_reminders_delivered(claims) -> int:
    """按 (id, claimed_at) 标记送达；处理时间已变化的行不会被旧请求覆盖。"""
    pairs = _normalize_reminder_claims(claims)
    if not pairs:
        return 0
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE memories SET reminder_delivered_at = NOW()
            WHERE reminder_delivered_at IS NULL
              AND (id, reminder_claimed_at) IN (
                  SELECT * FROM unnest($1::int[], $2::timestamptz[])
              )
            RETURNING id
            """,
            [memory_id for memory_id, _ in pairs],
            [claimed_at for _, claimed_at in pairs],
        )
    return len(rows)


async def renew_reminder_claims(claims) -> list:
    """发送期间延长处理时限，只返回仍由本次请求处理且未送达的提醒。"""
    pairs = _normalize_reminder_claims(claims)
    if not pairs:
        return []
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE memories SET reminder_claimed_at = NOW()
            WHERE reminder_delivered_at IS NULL
              AND (id, reminder_claimed_at) IN (
                  SELECT * FROM unnest($1::int[], $2::timestamptz[])
              )
            RETURNING id, reminder_claimed_at
            """,
            [memory_id for memory_id, _ in pairs],
            [claimed_at for _, claimed_at in pairs],
        )
    return [(int(row["id"]), row["reminder_claimed_at"]) for row in rows]


async def set_memory_reminder(memory_id: int, remind_at) -> bool:
    """给已有活跃记忆设置或改期提醒，并清空旧的处理时间与送达记录。"""
    if not isinstance(memory_id, int) or isinstance(memory_id, bool) or remind_at is None:
        return False
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE memories
            SET remind_at = $2, reminder_claimed_at = NULL, reminder_delivered_at = NULL
            WHERE id = $1 AND is_active = TRUE
            RETURNING id
            """,
            memory_id,
            remind_at,
        )
    return row is not None


async def release_reminder_claims(claims) -> int:
    """失败时交回仍由本次请求处理的提醒，让下一次请求立即重试。"""
    pairs = _normalize_reminder_claims(claims)
    if not pairs:
        return 0
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE memories SET reminder_claimed_at = NULL
            WHERE reminder_delivered_at IS NULL
              AND (id, reminder_claimed_at) IN (
                  SELECT * FROM unnest($1::int[], $2::timestamptz[])
              )
            RETURNING id
            """,
            [memory_id for memory_id, _ in pairs],
            [claimed_at for _, claimed_at in pairs],
        )
    return len(rows)


def _normalize_excluded_ids(exclude_ids) -> list:
    return sorted({
        int(value)
        for value in (exclude_ids or [])
        if isinstance(value, int) and not isinstance(value, bool)
    })


async def _source_covered_memory_ids(source_message_ids) -> list[int]:
    message_ids = _normalize_excluded_ids(source_message_ids)
    if not message_ids:
        return []
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id FROM memories
               WHERE is_active = TRUE
                 AND layer = 1
                 AND source_content_intact = TRUE
                 AND cardinality(source_message_ids) > 0
                 AND source_message_ids <@ $1::integer[]""",
            message_ids,
        )
    return [int(row["id"]) for row in rows]


def _memory_search_output(
    results,
    include_mode: bool,
    mode: str,
    keywords=(),
    include_keywords: bool = False,
):
    if not include_mode:
        return results
    output = {"results": results, "mode": mode}
    if include_keywords:
        output["keywords"] = list(keywords)
    return output


def _keywords_with_phrases(query: str):
    """Keep ordinary keywords and append matching configured proper nouns."""
    keywords = list(db_search.extract_search_keywords(query))
    lowered_query = query.lower()
    phrases = [
        phrase for phrase in shared.MEMORY_PHRASES
        if phrase.lower() in lowered_query
    ]
    seen = {keyword.lower() for keyword in keywords}
    for phrase in phrases:
        if phrase.lower() not in seen:
            keywords.append(phrase)
            seen.add(phrase.lower())
    return keywords, phrases


def _phrase_bonus(phrases: list[str], content: str) -> float:
    if not phrases:
        return 0.0
    lowered_content = content.lower()
    matched = sum(phrase.lower() in lowered_content for phrase in phrases)
    return _MEMORY_PHRASE_BONUS * matched / len(phrases)


async def search_memories(
    query: str,
    limit: int = 10,
    exclude_ids=None,
    recalled_message_ids=None,
    include_mode: bool = False,
    include_keywords: bool = False,
):
    """
    搜索相关记忆

    MEMORY_VECTOR_ENABLED=true 时走混合搜索（关键词 + 向量）
    否则走纯关键词搜索
    include_mode=True 时统一返回 results + mode；默认保留裸列表契约
    """
    covered_ids = (
        await _source_covered_memory_ids(recalled_message_ids)
        if shared.MEMORY_SOURCE_DEDUPE_ENABLED
        else []
    )
    excluded_ids = sorted(set(_normalize_excluded_ids(exclude_ids)) | set(covered_ids))
    if covered_ids:
        print(f"🧹 原文已完整覆盖，跳过 {len(covered_ids)} 条碎片记忆")
    if shared.MEMORY_VECTOR_ENABLED:
        return await search_memories_hybrid(
            query,
            limit,
            exclude_ids=excluded_ids,
            include_mode=include_mode,
            include_keywords=include_keywords,
        )

    # ---- 纯关键词搜索 ----
    keywords, _ = _keywords_with_phrases(query)

    if not keywords:
        return _memory_search_output(
            [], include_mode, "keyword", keywords, include_keywords
        )

    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        # 每个关键词命中得1分
        case_parts = []
        params = []
        for i, kw in enumerate(keywords):
            case_parts.append(f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END")
            params.append(kw)

        hit_count_expr = " + ".join(case_parts)
        max_hits = len(keywords)

        # 至少命中一个关键词（只搜索活跃记忆）
        where_parts = [f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords))]
        where_clause = f"is_active = TRUE AND ({' OR '.join(where_parts)})"

        if excluded_ids:
            exclude_idx = len(params) + 1
            params.append(excluded_ids)
            where_clause += f" AND NOT (id = ANY(${exclude_idx}::int[]))"

        limit_idx = len(params) + 1
        params.append(limit)

        # 时效天数：事件记忆按本地日历日差（AT TIME ZONE 'UTC' 拿到无时区的 UTC 挂钟，
        # 加偏移后取日期，不依赖数据库会话时区）；普通碎片按 created_at 精确时长
        recency_days_expr = (
            "CASE WHEN event_date IS NOT NULL "
            f"THEN GREATEST(0, ((NOW() AT TIME ZONE 'UTC' + INTERVAL '{shared.TIMEZONE_HOURS} hours')::date - event_date))::float "
            "ELSE GREATEST(0, EXTRACT(EPOCH FROM (NOW() - created_at))) / 86400.0 END"
        )
        sql = f"""
            SELECT
                id, content, importance, created_at, event_date, is_pinned,
                ({hit_count_expr}) AS hit_count,
                ({recency_days_expr}) AS effective_days,
                (
                    {shared.WEIGHT_KEYWORD} * ({hit_count_expr})::float / {max_hits}.0 +
                    {shared.WEIGHT_IMPORTANCE} * importance::float / 10.0 +
                    {shared.WEIGHT_RECENCY} * (1.0 / (1.0 + ({recency_days_expr})))
                ) AS score
            FROM memories
            WHERE {where_clause}
            ORDER BY score DESC, importance DESC, effective_days ASC
            LIMIT ${limit_idx}
        """

        results = await conn.fetch(sql, *params)

        # 过滤低分记忆
        if shared.MIN_SCORE_THRESHOLD > 0:
            before_count = len(results)
            results = [r for r in results if r['score'] >= shared.MIN_SCORE_THRESHOLD]
            filtered = before_count - len(results)
        else:
            filtered = 0

        if results:
            print(f"🔍 搜索 '{query}' → 关键词 {keywords[:8]}{'...' if len(keywords)>8 else ''} → 命中 {len(results)} 条" + (f"（过滤 {filtered} 条低分）" if filtered else ""))
            for r in results[:3]:
                print(f"   📌 [score={r['score']:.3f}] (hits={r['hit_count']}, imp={r['importance']}) {r['content'][:60]}...")

            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        else:
            print(f"🔍 搜索 '{query}' → 关键词 {keywords[:8]} → 无结果" + (f"（{filtered} 条被分数阈值过滤）" if filtered else ""))

        return _memory_search_output(
            results, include_mode, "keyword", keywords, include_keywords
        )


def _effective_days_ago(event_date, created_at, now_utc):
    """时效天数：事件记忆按本地日历日差（event_date 是本地日期，不做 UTC 换算），
    普通碎片按 created_at 精确时长"""
    if event_date:
        local_today = (now_utc + timedelta(hours=shared.TIMEZONE_HOURS)).date()
        return max(0.0, float((local_today - event_date).days))
    return max(0.0, (now_utc - created_at).total_seconds() / 86400.0)


def _anchored_semantic_normalize(scores: dict, threshold: float) -> dict:
    """语义轴归一化：下界取批内最低，上界不低于语义阈值。
    池里有候选过阈值时与 min-max 一致；全弱池不再把最高项拉满"""
    if not scores:
        return {}
    vals = list(scores.values())
    min_v = min(vals)
    max_v = max(max(vals), threshold)
    spread = max_v - min_v
    if spread == 0:
        return {k: 0.0 if max_v == 0 else 1.0 for k in scores}
    return {k: (v - min_v) / spread for k, v in scores.items()}


async def search_memories_hybrid(
    query: str,
    limit: int = 10,
    exclude_ids=None,
    include_mode: bool = False,
    include_keywords: bool = False,
):
    """
    记忆混合搜索：关键词 + 向量，归一化后四维加权

    权重：MEMORY_HW_KEYWORD + MEMORY_HW_SEMANTIC + MEMORY_HW_IMPORTANCE + MEMORY_HW_RECENCY
    include_mode=True 时统一返回 results + mode；默认保留裸列表契约
    """
    excluded_ids = _normalize_excluded_ids(exclude_ids)
    keywords, phrases = _keywords_with_phrases(query)
    query_embedding = await db_search.get_query_embedding(query) if shared.EMBEDDING_API_KEY else []
    search_mode = "hybrid" if query_embedding else "keyword"

    if not keywords and not query_embedding:
        return _memory_search_output(
            [], include_mode, search_mode, keywords, include_keywords
        )

    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        candidates = {}  # id -> {content, importance, created_at, kw_score, similarity}

        # ---- 关键词路 ----
        if keywords:
            case_parts = []
            params = []
            for i, kw in enumerate(keywords):
                case_parts.append(f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END")
                params.append(kw)

            hit_count_expr = " + ".join(case_parts)
            where_parts = [f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords))]
            where_clause = f"is_active = TRUE AND ({' OR '.join(where_parts)})"

            # IDF：ln((活跃总数+1)/(df+1))+1，df 与命中同一 ILIKE 口径；
            # df=0 的词谁都命中不了，权重记 0、不进分母，全为 0 时无候选
            df_cols = ", ".join(
                f"COUNT(*) FILTER (WHERE content ILIKE '%' || ${i+1} || '%') AS df_{i}"
                for i in range(len(keywords))
            )
            idf_cols = ", ".join(
                f"CASE WHEN df_{i} > 0 THEN LN((total + 1)::float8 / (df_{i} + 1)) + 1 ELSE 0 END AS w_{i}"
                for i in range(len(keywords))
            )
            weighted_expr = " + ".join(
                f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN w_{i} ELSE 0 END"
                for i in range(len(keywords))
            )
            weight_total_expr = " + ".join(f"w_{i}" for i in range(len(keywords)))

            if excluded_ids:
                exclude_idx = len(params) + 1
                params.append(excluded_ids)
                where_clause += f" AND NOT (id = ANY(${exclude_idx}::int[]))"

            limit_idx = len(params) + 1
            params.append(limit * 3)

            kw_sql = f"""
                WITH kw_df AS (
                    SELECT COUNT(*) AS total, {df_cols}
                    FROM memories
                    WHERE is_active = TRUE
                ), kw_idf AS (
                    SELECT {idf_cols} FROM kw_df
                )
                SELECT id, content, importance, created_at, event_date, is_pinned,
                       ({hit_count_expr}) AS hit_count,
                       ({weighted_expr}) / NULLIF({weight_total_expr}, 0) AS kw_score
                FROM memories CROSS JOIN kw_idf
                WHERE {where_clause}
                ORDER BY kw_score DESC
                LIMIT ${limit_idx}
            """
            kw_rows = await conn.fetch(kw_sql, *params)

            for r in kw_rows:
                candidates[r['id']] = {
                    'content': r['content'],
                    'importance': r['importance'],
                    'created_at': r['created_at'],
                    'event_date': r['event_date'],
                    'is_pinned': r['is_pinned'],
                    'hit_count': r['hit_count'],
                    'kw_score': float(r['kw_score']),
                    'similarity': 0.0,
                }

        # ---- 向量路 ----
        if query_embedding:
            keyword_candidate_ids = list(candidates)
            if db_core.HAS_PGVECTOR:
                vec_str = '[' + ','.join(str(f) for f in query_embedding) + ']'
                sem_rows = await conn.fetch("""
                    SELECT id, content, importance, created_at, event_date, is_pinned,
                           1 - (embedding <=> $1::vector) as similarity
                    FROM memories
                    WHERE embedding IS NOT NULL AND is_active = TRUE
                      AND NOT (id = ANY($2::int[]))
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                """, vec_str, excluded_ids, limit * 3)

                if keyword_candidate_ids:
                    keyword_sim_rows = await conn.fetch("""
                        SELECT id, 1 - (embedding <=> $1::vector) as similarity
                        FROM memories
                        WHERE embedding IS NOT NULL AND is_active = TRUE
                          AND id = ANY($2::int[])
                    """, vec_str, keyword_candidate_ids)
                    for r in keyword_sim_rows:
                        candidates[r['id']]['similarity'] = float(r['similarity'])
            else:
                # Python端计算cosine
                all_mem = await conn.fetch("""
                    SELECT id, content, importance, created_at, event_date, is_pinned, embedding_json
                    FROM memories
                    WHERE embedding_json IS NOT NULL AND is_active = TRUE
                      AND NOT (id = ANY($1::int[]))
                """, excluded_ids)

                scored = []
                for row in all_mem:
                    try:
                        emb = json.loads(row['embedding_json'])
                        sim = db_search._cosine_sim(query_embedding, emb)
                        scored.append({**dict(row), 'similarity': sim})
                    except Exception:
                        continue
                for r in scored:
                    if r['id'] in candidates:
                        candidates[r['id']]['similarity'] = float(r['similarity'])
                scored.sort(key=lambda x: -x['similarity'])
                sem_rows = scored[:limit * 3]

            for r in sem_rows:
                sim = float(r['similarity'])
                mid = r['id']
                if mid in candidates:
                    candidates[mid]['similarity'] = sim
                elif sim >= shared.MEMORY_SEMANTIC_THRESHOLD:
                    candidates[mid] = {
                        'content': r['content'],
                        'importance': r['importance'],
                        'created_at': r['created_at'],
                        'event_date': r['event_date'],
                        'is_pinned': r['is_pinned'],
                        'hit_count': 0,
                        'kw_score': 0.0,
                        'similarity': sim,
                    }

            # debug：向量路统计
            sem_total = len(sem_rows)
            sem_passed = sum(1 for r in sem_rows if float(r['similarity']) >= shared.MEMORY_SEMANTIC_THRESHOLD)
            sem_max = max((float(r['similarity']) for r in sem_rows), default=0)
            if sem_total > 0 and sem_passed == 0:
                print(f"   🔢 向量路: {sem_total}条候选全被阈值过滤（最高sim={sem_max:.3f}, 阈值={shared.MEMORY_SEMANTIC_THRESHOLD}）")
            elif sem_total > 0:
                print(f"   🔢 向量路: {sem_passed}/{sem_total}条通过阈值（最高sim={sem_max:.3f}）")

        if not candidates:
            print(f"🔍 混合搜索 '{query}' → 两路均无结果")
            return _memory_search_output(
                [], include_mode, search_mode, keywords, include_keywords
            )

        # ---- 归一化 + 加权 ----
        # 关键词轴直接用 IDF 覆盖率（0~1），不做批内拉伸，泛词池不会被顶满
        kw_norm = {mid: v['kw_score'] for mid, v in candidates.items()}
        sem_norm = _anchored_semantic_normalize(
            {mid: v['similarity'] for mid, v in candidates.items()},
            shared.MEMORY_SEMANTIC_THRESHOLD,
        )

        now = datetime.now(dt_timezone.utc)
        final = []
        for mid, info in candidates.items():
            kw = kw_norm.get(mid, 0.0)
            sem = sem_norm.get(mid, 0.0)
            imp = info['importance'] / 10.0
            days = _effective_days_ago(info.get('event_date'), info['created_at'], now)
            rec = 1.0 / (1.0 + days)

            score = (shared.MEMORY_HW_KEYWORD * kw +
                     shared.MEMORY_HW_SEMANTIC * sem +
                     shared.MEMORY_HW_IMPORTANCE * imp +
                     shared.MEMORY_HW_RECENCY * rec +
                     _phrase_bonus(phrases, info['content']))

            final.append({
                'id': mid,
                'content': info['content'],
                'importance': info['importance'],
                'created_at': info['created_at'],
                'event_date': info.get('event_date'),
                'is_pinned': info.get('is_pinned', False),
                'hit_count': info['hit_count'],
                'similarity': info['similarity'],
                'score': score,
            })

        final.sort(key=lambda x: (-x['score'], -x['importance']))

        # 过滤低分
        if shared.MIN_SCORE_THRESHOLD > 0:
            before_count = len(final)
            final = [r for r in final if r['score'] >= shared.MIN_SCORE_THRESHOLD]
            filtered = before_count - len(final)
        else:
            filtered = 0

        results = final[:limit]

        if results:
            mode_tag = "混合" if query_embedding else "关键词"
            kw_tag = f"关键词 {keywords[:6]}" if keywords else "无关键词"
            print(f"🔍 {mode_tag}搜索 '{query}' → {kw_tag} → 命中 {len(results)} 条" + (f"（过滤 {filtered} 条低分）" if filtered else ""))
            for r in results[:3]:
                print(f"   📌 [score={r['score']:.3f}] (kw={r['hit_count']}, sim={r['similarity']:.2f}, imp={r['importance']}) {r['content'][:60]}...")

            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        else:
            print(f"🔍 混合搜索 '{query}' → 无结果" + (f"（{filtered} 条被过滤）" if filtered else ""))

        output = [dict(r) for r in results]
        return _memory_search_output(
            output, include_mode, search_mode, keywords, include_keywords
        )


async def get_extraction_candidates(
    query: str,
    relevant_limit: int = _EXTRACTION_RELEVANT_LIMIT,
    recent_limit: int = _EXTRACTION_RECENT_LIMIT,
):
    """Return read-only dedup candidates: relevant top K union latest M active."""
    def candidate_row(row):
        return {
            key: row[key]
            for key in ("id", "content", "importance", "created_at", "event_date")
        }

    keywords = db_search.extract_search_keywords(query)
    query_embedding = (
        await db_search.get_query_embedding(query)
        if relevant_limit > 0 and shared.MEMORY_VECTOR_ENABLED and shared.EMBEDDING_API_KEY
        else []
    )

    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        relevant = {}
        candidate_pool_size = relevant_limit * 3

        if keywords and relevant_limit > 0:
            hit_parts = [
                f"CASE WHEN content ILIKE '%' || ${index + 1} || '%' THEN 1 ELSE 0 END"
                for index in range(len(keywords))
            ]
            hit_count = " + ".join(hit_parts)
            where_parts = [
                f"content ILIKE '%' || ${index + 1} || '%'"
                for index in range(len(keywords))
            ]
            keyword_rows = await conn.fetch(
                f"""
                SELECT id, content, importance, created_at, event_date,
                       ({hit_count})::float / {len(keywords)}.0 AS keyword_score
                FROM memories
                WHERE is_active = TRUE AND ({' OR '.join(where_parts)})
                ORDER BY keyword_score DESC, id ASC
                LIMIT ${len(keywords) + 1}
                """,
                *keywords,
                candidate_pool_size,
            )
            for row in keyword_rows:
                relevant[row["id"]] = {
                    "row": candidate_row(row),
                    "keyword_score": float(row["keyword_score"]),
                    "similarity": None,
                }

        if query_embedding and relevant_limit > 0:
            semantic_rows = []
            try:
                if db_core.HAS_PGVECTOR:
                    vector_text = "[" + ",".join(str(value) for value in query_embedding) + "]"
                    semantic_rows = await conn.fetch(
                        """
                        SELECT id, content, importance, created_at, event_date,
                               1 - (embedding <=> $1::vector) AS similarity
                        FROM memories
                        WHERE embedding IS NOT NULL AND is_active = TRUE
                        ORDER BY embedding <=> $1::vector, id ASC
                        LIMIT $2
                        """,
                        vector_text,
                        candidate_pool_size,
                    )
                else:
                    embedded_rows = await conn.fetch(
                        """
                        SELECT id, content, importance, created_at, event_date,
                               embedding_json
                        FROM memories
                        WHERE embedding_json IS NOT NULL AND is_active = TRUE
                        """
                    )
                    for row in embedded_rows:
                        try:
                            semantic_rows.append({
                                **dict(row),
                                "similarity": db_search._cosine_sim(
                                    query_embedding,
                                    json.loads(row["embedding_json"]),
                                ),
                            })
                        except Exception:
                            continue
                    semantic_rows.sort(key=lambda row: (-row["similarity"], row["id"]))
                    semantic_rows = semantic_rows[:candidate_pool_size]
            except Exception as exc:
                print(f"⚠️ 提取候选向量检索失败，退回关键词和最新记忆: {exc}")

            for row in semantic_rows:
                item = relevant.setdefault(row["id"], {
                    "row": candidate_row(row),
                    "keyword_score": None,
                    "similarity": None,
                })
                item["similarity"] = float(row["similarity"])

        def relevance_key(item):
            keyword_score = item["keyword_score"]
            similarity = item["similarity"]
            best_score = max(
                score for score in (keyword_score, similarity) if score is not None
            )
            return (
                -best_score,
                -(keyword_score if keyword_score is not None else float("-inf")),
                -(similarity if similarity is not None else float("-inf")),
                item["row"]["id"],
            )

        relevant_rows = [
            item["row"]
            for item in sorted(relevant.values(), key=relevance_key)[:relevant_limit]
        ]
        recent_rows = []
        if recent_limit > 0:
            recent_rows = await conn.fetch(
                """
                SELECT id, content, importance, created_at, event_date
                FROM memories
                WHERE is_active = TRUE
                ORDER BY created_at DESC, id DESC
                LIMIT $1
                """,
                recent_limit,
            )

    candidates = []
    seen_ids = set()
    for row in [*relevant_rows, *recent_rows]:
        memory = candidate_row(row)
        if memory["id"] in seen_ids:
            continue
        seen_ids.add(memory["id"])
        candidates.append(memory)
    return candidates


async def get_pending_memory_embedding_count():
    """查询还没有embedding的活跃记忆数量"""
    embedding_column = "embedding" if db_core.HAS_PGVECTOR else "embedding_json"
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT COUNT(*) FROM memories WHERE is_active = TRUE AND {embedding_column} IS NULL AND content IS NOT NULL"
        )


async def get_active_memory_embedding_counts():
    """Dashboard 只展示活跃记忆的向量覆盖率。"""
    embedding_column = "embedding" if db_core.HAS_PGVECTOR else "embedding_json"
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"""
            SELECT COUNT(*) FILTER (WHERE {embedding_column} IS NOT NULL) AS cumulative_embedded,
                   COUNT(*) FILTER (WHERE {embedding_column} IS NULL) AS remaining
            FROM memories WHERE is_active = TRUE AND content IS NOT NULL
        """)
    return dict(row)


async def backfill_memory_embeddings(batch_size: int = 20):
    """给已有记忆补算embedding（没有embedding的记忆）"""
    if not db_search.embedding_ready():
        return 0
    if not shared.EMBEDDING_API_KEY:
        print("⚠️ EMBEDDING_API_KEY 未设置，无法补算embedding")
        return 0

    embedding_column = "embedding" if db_core.HAS_PGVECTOR else "embedding_json"
    pool = await db_core.get_pool()
    total_updated = 0

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT id, content FROM memories
            WHERE is_active = TRUE AND {embedding_column} IS NULL AND content IS NOT NULL
            ORDER BY id
            LIMIT $1
        """, batch_size)

    if not rows:
        print("✅ 所有活跃记忆已有embedding，无需补算")
        return 0

    print(f"🔄 开始补算记忆embedding... 本批 {len(rows)} 条")

    async with pool.acquire() as conn:
        for row in rows:
            if not db_search.embedding_ready():
                break
            try:
                embedding = await db_search.compute_embedding(row['content'] or '')
                if embedding:
                    await db_search.save_memory_embedding(conn, row['id'], embedding)
                    total_updated += 1
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding计算失败: {e}")

    # 检查剩余
    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            f"SELECT COUNT(*) FROM memories WHERE is_active = TRUE AND {embedding_column} IS NULL AND content IS NOT NULL"
        )

    print(f"✅ 本批补算完成：{total_updated}/{len(rows)} 条成功" + (f"，剩余 {remaining} 条待处理" if remaining > 0 else ""))
    return total_updated


async def get_recent_memories(limit: int = 20, include_archived: bool = False):
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, content, importance, source_session, created_at,
                      layer, title, is_active, is_pinned, event_date,
                      remind_at, reminder_delivered_at
               FROM memories
               WHERE ($2::boolean OR is_active = TRUE)
               ORDER BY id DESC LIMIT $1""",
            limit,
            include_archived,
        )
        return [dict(row) for row in rows]


async def get_all_memories_count():
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM memories")
        return row["cnt"]


async def get_all_memories():
    """导出所有记忆（用于备份，含归档记录与三层结构字段）

    embedding/embedding_json 和 last_accessed 是可重算的派生数据与访问状态，不进备份。
    """
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, is_pinned, merged_from, event_date, superseded_by,
                   remind_at, reminder_delivered_at,
                   source_message_ids, source_content_intact
            FROM memories ORDER BY id
        """)
        memories = [dict(r) for r in rows]

    memory_ids = {memory["id"] for memory in memories}
    broken_references = []
    for memory in memories:
        missing = sorted(set(memory.get("merged_from") or []) - memory_ids)
        if missing:
            broken_references.append((memory["id"], missing))
    if broken_references:
        logger.warning(
            "Backup blocked by broken merged_from references: %s",
            broken_references,
        )
        raise BrokenMergeReferencesError(len(broken_references))

    broken_supersession_references = [
        memory["id"]
        for memory in memories
        if memory.get("superseded_by") is not None
        and memory["superseded_by"] not in memory_ids
    ]
    if broken_supersession_references:
        logger.warning(
            "Backup blocked by broken superseded_by references: %s",
            broken_supersession_references,
        )
        raise BrokenSupersessionReferencesError(
            len(broken_supersession_references)
        )

    return memories


async def repair_broken_merge_references():
    """清除已经无法完整撤回的 merged_from 关系，保留父记忆本身。"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("""
                WITH broken AS (
                    SELECT parent.id
                    FROM memories AS parent
                    WHERE parent.merged_from IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM unnest(parent.merged_from) AS refs(source_id)
                          WHERE NOT EXISTS (
                              SELECT 1
                              FROM memories AS source
                              WHERE source.id = refs.source_id
                          )
                      )
                )
                UPDATE memories AS parent
                SET merged_from = NULL
                FROM broken
                WHERE parent.id = broken.id
                RETURNING parent.id
            """)
            return len(rows)


def _parse_backup_datetime(value):
    """解析备份里的时间字符串；解析不了返回 None（落库走默认 NOW()）"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt_timezone.utc)
        except ValueError:
            return None


def _parse_backup_date(value):
    """解析备份里的日期字符串（event_date 用），解析不了返回 None"""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def import_memories_v2(
    memories: list,
    schema_version: int = 2,
    preserve_source_message_ids: bool = False,
):
    """恢复版本化备份：单事务建映射，再回填合并与版本关系。

    - 同内容且库中唯一 → 跳过并映射到已有行（同一份备份重复导入幂等）
    - 同内容但库中多行 → 不猜挂哪条，计入 conflicts 回执，引用它的合并链降级
    - merged_from 引用了备份中不存在的 backup_id → 备份损坏，抛错整批回滚
    - embedding 不随导入计算，恢复后由 backfill 重算
    """
    # ---- 事务外纯格式校验：先收集全部 backup_id，再验证引用封闭性 ----
    backup_ids = set()
    for mem in memories:
        if not isinstance(mem, dict):
            raise ValueError("记忆条目必须是 JSON 对象")
        bid = mem.get("backup_id")
        if isinstance(bid, bool) or not isinstance(bid, int):
            raise ValueError(f"backup_id 缺失或非法: {bid!r}")
        if bid in backup_ids:
            raise ValueError(f"backup_id 重复: {bid}")
        backup_ids.add(bid)
        if not isinstance(mem.get("content"), str) or not mem["content"].strip():
            raise ValueError(f"记忆 {bid} 缺少 content")
        if mem.get("layer", 1) not in (1, 2, 3):
            raise ValueError(f"记忆 {bid} 层级非法: {mem.get('layer')!r}")
    for mem in memories:
        for ref in (mem.get("merged_from") or []):
            if isinstance(ref, bool) or not isinstance(ref, int):
                raise ValueError(f"记忆 {mem['backup_id']} 的 merged_from 含非法引用: {ref!r}")
            if ref not in backup_ids:
                raise ValueError(
                    f"记忆 {mem['backup_id']} 的 merged_from 引用了备份中不存在的 {ref}，备份不完整"
                )
        successor = mem.get("superseded_by") if schema_version >= 3 else None
        if successor is not None:
            if isinstance(successor, bool) or not isinstance(successor, int):
                raise ValueError(
                    f"记忆 {mem['backup_id']} 的 superseded_by 引用非法: {successor!r}"
                )
            if successor not in backup_ids:
                raise ValueError(
                    f"记忆 {mem['backup_id']} 的 superseded_by 引用了备份中不存在的 {successor}，备份不完整"
                )
    if schema_version >= 6:
        pinned_chars = sum(
            len(mem["content"])
            for mem in memories
            if mem.get("is_active", True) and mem.get("is_pinned") is True
        )
        if pinned_chars > PINNED_MEMORY_BUDGET_CHARS:
            raise ValueError(
                f"活跃常驻记忆共 {pinned_chars} 字，超过 {PINNED_MEMORY_BUDGET_CHARS} 字预算"
            )

    pool = await db_core.get_pool()
    imported = 0
    skipped = 0
    conflicts = []
    degraded = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            if schema_version >= 6:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('pawwake_pinned_memory_budget'))"
                )
            # ---- 第一遍：插入并建立 旧 backup_id → 新库 id 映射 ----
            id_map = {}
            for mem in memories:
                bid = mem["backup_id"]
                content = mem["content"]
                rows = await conn.fetch(
                    "SELECT id FROM memories WHERE content = $1", content
                )
                if len(rows) == 1:
                    id_map[bid] = int(rows[0]["id"])
                    skipped += 1
                    continue
                if len(rows) > 1:
                    conflicts.append({
                        "backup_id": bid,
                        "matched_ids": sorted(int(r["id"]) for r in rows),
                    })
                    skipped += 1
                    continue
                if (
                    schema_version >= 6
                    and mem.get("is_active", True)
                    and mem.get("is_pinned") is True
                ):
                    used = await conn.fetchval(
                        """SELECT COALESCE(SUM(char_length(content)), 0)
                           FROM memories
                           WHERE is_active = TRUE AND is_pinned = TRUE"""
                    )
                    if used + len(content) > PINNED_MEMORY_BUDGET_CHARS:
                        raise ValueError(
                            f"导入后活跃常驻记忆超过 {PINNED_MEMORY_BUDGET_CHARS} 字预算"
                        )
                source_message_ids = None
                if schema_version >= 5 and preserve_source_message_ids:
                    raw_source_ids = mem.get("source_message_ids")
                    if (
                        isinstance(raw_source_ids, list)
                        and raw_source_ids
                        and all(
                            isinstance(value, int) and not isinstance(value, bool) and value > 0
                            for value in raw_source_ids
                        )
                    ):
                        source_message_ids = sorted(set(raw_source_ids))
                        source_count = await conn.fetchval(
                            """SELECT COUNT(*) FROM conversations
                               WHERE id = ANY($1::integer[]) AND session_id = $2""",
                            source_message_ids,
                            mem.get("source_session") or "json-import",
                        )
                        if source_count != len(source_message_ids):
                            source_message_ids = None
                row = await conn.fetchrow("""
                    INSERT INTO memories (content, importance, source_session, created_at,
                                          layer, title, is_active, event_date,
                                          remind_at, reminder_delivered_at,
                                          source_message_ids, source_content_intact, is_pinned)
                    VALUES ($1, $2, $3, COALESCE($4, NOW()), $5, $6, $7, $8,
                            $9, $10, $11, $12, $13)
                    RETURNING id
                """,
                    content,
                    mem.get("importance", 5),
                    mem.get("source_session") or "json-import",
                    _parse_backup_datetime(mem.get("created_at")),
                    mem.get("layer", 1),
                    mem.get("title") or "",
                    bool(mem.get("is_active", True)),
                    _parse_backup_date(mem.get("event_date")),
                    # schema 4 起备份提醒与送达时间；临时处理时间不进备份
                    _parse_backup_datetime(mem.get("remind_at")),
                    _parse_backup_datetime(mem.get("reminder_delivered_at")),
                    source_message_ids,
                    bool(source_message_ids and mem.get("source_content_intact") is True),
                    bool(schema_version >= 6 and mem.get("is_pinned") is True),
                )
                id_map[bid] = int(row["id"])
                imported += 1

            # ---- 第二遍：用映射回填 merged_from ----
            for mem in memories:
                refs = mem.get("merged_from") or []
                if not refs:
                    continue
                bid = mem["backup_id"]
                new_id = id_map.get(bid)
                if new_id is None:
                    # 父条本身因内容冲突被跳过，没有落库行可回填
                    continue
                unresolved = [ref for ref in refs if ref not in id_map]
                if unresolved:
                    # 来源条目因冲突未建立映射：不猜关系，保持 NULL 并回执降级
                    degraded.append({"backup_id": bid, "unresolved": unresolved})
                    continue
                await conn.execute(
                    "UPDATE memories SET merged_from = $1 WHERE id = $2",
                    [id_map[ref] for ref in refs], new_id,
                )

            if schema_version >= 3:
                for mem in memories:
                    successor = mem.get("superseded_by")
                    if successor is None:
                        continue
                    old_id = id_map.get(mem["backup_id"])
                    successor_id = id_map.get(successor)
                    if old_id is None or successor_id is None:
                        raise ValueError(
                            f"记忆 {mem['backup_id']} 的版本链因内容冲突无法完整恢复"
                        )
                    await conn.execute(
                        """UPDATE memories
                           SET superseded_by = $1, is_active = FALSE
                           WHERE id = $2""",
                        successor_id,
                        old_id,
                    )

                restored_ids = list(id_map.values())
                broken_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM memories AS old
                    LEFT JOIN memories AS successor
                      ON successor.id = old.superseded_by
                    WHERE old.id = ANY($1::int[])
                      AND old.superseded_by IS NOT NULL
                      AND successor.id IS NULL
                    """,
                    restored_ids,
                )
                if broken_count:
                    raise ValueError("版本链恢复后校验失败")

    total = await get_all_memories_count()
    result = {
        "status": "done",
        "schema_version": schema_version,
        "imported": imported,
        "skipped": skipped,
        "conflicts": conflicts,
        "degraded": degraded,
        "source_lineage_preserved": bool(schema_version >= 5 and preserve_source_message_ids),
        "total": total,
    }
    if shared.MEMORY_VECTOR_ENABLED:
        try:
            result["pending_embeddings"] = await get_pending_memory_embedding_count()
        except Exception:
            pass
    return result


async def get_all_memories_detail(limit: int = None, layer: int = None,
                                  active_only: bool = None, memory_ids: list = None):
    """获取所有记忆（含 id，用于管理页面）

    Args:
        limit: 可选，限制返回数量
        layer: 可选，筛选指定层级（1=原始碎片, 2=事件记忆, 3=核心记忆）
        active_only: 可选，是否只返回 is_active=true 的记忆
        memory_ids: 可选，只返回指定 ID
    """
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        param_idx = 1

        if layer is not None:
            conditions.append(f"layer = ${param_idx}")
            params.append(layer)
            param_idx += 1

        if active_only is not None:
            conditions.append(f"is_active = ${param_idx}")
            params.append(active_only)
            param_idx += 1

        if memory_ids is not None:
            conditions.append(f"id = ANY(${param_idx}::int[])")
            params.append(memory_ids)
            param_idx += 1

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        if limit is not None:
            limit_clause = f"LIMIT ${param_idx}"
            params.append(limit)
        else:
            limit_clause = ""

        rows = await conn.fetch(f"""
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, is_pinned, merged_from, event_date, superseded_by,
                   remind_at, reminder_delivered_at
            FROM memories
            {where_clause}
            ORDER BY id
            {limit_clause}
        """, *params)
        return [dict(r) for r in rows]


async def get_active_pinned_memories():
    """Return every active pinned memory in stable order."""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, is_pinned, event_date,
                   remind_at, reminder_delivered_at
            FROM memories
            WHERE is_active = TRUE AND is_pinned = TRUE
            ORDER BY id
        """)
        return [dict(row) for row in rows]


async def get_core_candidate_memories(min_merged_sources: int, min_importance: int,
                                      limit: int):
    """Return the bounded active layer-2 rows that match core-candidate rules."""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        # Dashboard-only scan; add an expression index only if measured latency warrants it.
        rows = await conn.fetch("""
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, merged_from, event_date, superseded_by,
                   remind_at, reminder_delivered_at
            FROM memories
            WHERE layer = 2
              AND is_active = TRUE
              AND (
                  cardinality(COALESCE(merged_from, '{}'::int[])) >= $1
                  OR importance >= $2
              )
            ORDER BY (
                (cardinality(COALESCE(merged_from, '{}'::int[])) >= $1)::int
                + (importance >= $2)::int
            ) DESC, id
            LIMIT $3
        """, min_merged_sources, min_importance, limit)
        return [dict(row) for row in rows]


async def delete_archived_memory(memory_id: int):
    """永久删除一条未被合并关系引用的已归档记忆。"""
    return await delete_archived_memories_batch([memory_id])


async def delete_archived_memories_batch(memory_ids: list):
    """批量永久删除未被合并关系引用的已归档记忆。"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            """WITH targets AS (
               SELECT memory.id,
                          memory.superseded_by IS NOT NULL
                          OR EXISTS (
                              SELECT 1
                              FROM memories AS parent
                              WHERE memory.id = ANY(
                                  COALESCE(parent.merged_from, '{}'::int[])
                              )
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM memories AS predecessor
                              WHERE predecessor.superseded_by = memory.id
                          ) AS protected
                   FROM memories AS memory
                   WHERE memory.id = ANY($1::int[])
                     AND memory.is_active = FALSE
               ), deleted AS (
                   DELETE FROM memories AS memory
                   USING targets
                   WHERE memory.id = targets.id AND NOT targets.protected
                   RETURNING memory.id
               )
               SELECT (SELECT COUNT(*) FROM deleted)::int AS deleted,
                      (SELECT COUNT(*) FROM targets WHERE protected)::int AS protected""",
            memory_ids,
        )
        return {
            "deleted": result["deleted"] if result else 0,
            "protected": result["protected"] if result else 0,
        }


async def soft_delete_memories_batch(memory_ids: list):
    """批量软删除记忆，返回实际转为不活跃的数量。"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE memories
               SET is_active = FALSE
               WHERE id = ANY($1::int[]) AND is_active = TRUE""",
            memory_ids,
        )
        return int(result.split()[-1]) if result else 0


async def restore_archived_memories_batch(memory_ids: list):
    """批量恢复普通归档记忆；版本前驱必须走显式撤销。"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('pawwake_pinned_memory_budget'))"
            )
            rows = await conn.fetch(
                """SELECT id, content, is_pinned
                   FROM memories
                   WHERE id = ANY($1::int[])
                     AND is_active = FALSE
                     AND superseded_by IS NULL
                   FOR UPDATE""",
                memory_ids,
            )
            pinned_chars = sum(
                len(row["content"]) for row in rows if row["is_pinned"] is True
            )
            used = await conn.fetchval(
                """SELECT COALESCE(SUM(char_length(content)), 0)
                   FROM memories
                   WHERE is_pinned = TRUE AND is_active = TRUE"""
            )
            if used + pinned_chars > PINNED_MEMORY_BUDGET_CHARS:
                raise ValueError(
                    f"恢复后活跃常驻记忆超过 {PINNED_MEMORY_BUDGET_CHARS} 字预算"
                )
            result = await conn.execute(
                """UPDATE memories
                   SET is_active = TRUE
                   WHERE id = ANY($1::int[])
                     AND is_active = FALSE
                     AND superseded_by IS NULL""",
                memory_ids,
            )
            return int(result.split()[-1]) if result else 0


async def restore_archived_memory(memory_id: int):
    """恢复一条普通归档记忆；版本前驱必须走显式撤销。"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('pawwake_pinned_memory_budget'))"
            )
            row = await conn.fetchrow(
                """
                SELECT id, is_active, superseded_by
                FROM memories
                WHERE id = $1
                FOR UPDATE
                """,
                memory_id,
            )
            if not row:
                return {"status": "not_found"}
            if row["superseded_by"] is not None:
                return {"status": "superseded"}
            if row["is_active"] is False:
                await _check_pinned_budget(conn, memory_id, is_active=True)
                await conn.execute(
                    "UPDATE memories SET is_active = TRUE WHERE id = $1",
                    memory_id,
                )
            return {"status": "ok"}


async def undo_memory_supersession(memory_id: int):
    """Restore one superseded predecessor and keep its successor untouched."""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('pawwake_pinned_memory_budget'))"
            )
            row = await conn.fetchrow(
                """
                SELECT id, superseded_by
                FROM memories
                WHERE id = $1
                FOR UPDATE
                """,
                memory_id,
            )
            if not row:
                return {"status": "not_found"}
            if row["superseded_by"] is None:
                return {"status": "not_superseded"}
            await _check_pinned_budget(conn, memory_id, is_active=True)
            await conn.execute(
                """
                UPDATE memories
                SET is_active = TRUE, superseded_by = NULL
                WHERE id = $1
                """,
                memory_id,
            )
            return {"status": "ok"}


# ============================================================
# 三层记忆架构（碎片/事件/核心）
# ============================================================

async def get_organizable_memories_by_date(event_date):
    """获取指定本地日期的活跃碎片和事件记忆。"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, created_at, event_date, layer, title
            FROM memories
            WHERE layer IN (1, 2) AND is_active = TRUE
            AND COALESCE(
                event_date,
                ((created_at AT TIME ZONE 'UTC') + make_interval(hours => $2))::date
            ) = $1
            AND NOT (remind_at IS NOT NULL AND reminder_delivered_at IS NULL)
            AND is_pinned = FALSE
            ORDER BY created_at, id
        """, event_date, shared.TIMEZONE_HOURS)
        return [dict(r) for r in rows]


async def promote_to_core(memory_id: int, title: str = None):
    """将记忆升级为核心记忆"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        if title:
            await conn.execute("""
                UPDATE memories SET layer = 3, title = $2
                WHERE id = $1
            """, memory_id, title)
        else:
            await conn.execute("""
                UPDATE memories SET layer = 3
                WHERE id = $1
            """, memory_id)


async def merge_memories(memory_ids: list, new_title: str, new_content: str,
                         importance: int, layer: int = 2):
    """合并多条记忆为一条新记忆"""
    if not memory_ids:
        return None
    if any(isinstance(memory_id, bool) or not isinstance(memory_id, int) for memory_id in memory_ids):
        raise ValueError("记忆 ID 必须是整数")
    requested_ids = set(memory_ids)
    if len(requested_ids) != len(memory_ids):
        raise ValueError("记忆 ID 不能重复")

    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        new_id = None
        async with conn.transaction():
            # 锁住全部来源行，在同一事务、同一连接里复验、插入、归档：
            # 后台此刻给某条来源写提醒会等在锁上，提交后它看到的是已归档行，set_memory_reminder 落空、按 new 保住提醒
            rows = await conn.fetch("""
                SELECT id, layer, is_active, is_pinned, remind_at, reminder_delivered_at,
                       source_message_ids
                FROM memories
                WHERE id = ANY($1::int[])
                ORDER BY id
                FOR UPDATE
            """, memory_ids)
            rows_by_id = {int(row["id"]): row for row in rows}
            unavailable = sorted(
                (requested_ids - rows_by_id.keys())
                | {memory_id for memory_id, row in rows_by_id.items() if not row["is_active"]}
            )
            if unavailable:
                raise ValueError(f"记忆 {unavailable} 不存在或已失效，不能合并")
            pinned = sorted(int(row["id"]) for row in rows if row["is_pinned"] is True)
            if pinned:
                raise ValueError(f"常驻记忆 {pinned} 不能合并，请先取消常驻")
            # 带未送达提醒的记忆不能被合并归档，否则提醒随之消失；先等送达或清除提醒
            blocked = sorted(
                int(r["id"]) for r in rows
                if r["remind_at"] is not None and r["reminder_delivered_at"] is None
            )
            if blocked:
                raise ValueError(f"记忆 {blocked} 带有未送达的提醒，不能合并；请等提醒送达后再合并")

            # 取最早发生日；事件记忆用 event_date，碎片按 Dashboard 的本地时区换算 created_at
            date_rows = await conn.fetch("""
                SELECT MIN(COALESCE(
                    event_date,
                    ((created_at AT TIME ZONE 'UTC') + make_interval(hours => $2))::date
                )) as event_date
                FROM memories WHERE id = ANY($1::int[])
            """, memory_ids, shared.TIMEZONE_HOURS)
            event_date = date_rows[0]['event_date'] if date_rows else None

            source_message_ids = None
            if all(row["source_message_ids"] for row in rows):
                source_message_ids = sorted({
                    message_id
                    for row in rows
                    for message_id in row["source_message_ids"]
                })

            # 创建新记忆
            row = await conn.fetchrow("""
                INSERT INTO memories (
                    content, importance, layer, title, is_active, merged_from, event_date,
                    source_message_ids, source_content_intact
                )
                VALUES ($1, $2, $3, $4, TRUE, $5, $6, $7, FALSE)
                RETURNING id
            """, new_content, importance, layer, new_title, memory_ids, event_date,
                source_message_ids)
            new_id = row['id'] if row else None

            # 将来源记忆标记为不活跃，与新记忆一起提交
            if new_id:
                await conn.execute("""
                    UPDATE memories SET is_active = FALSE
                    WHERE id = ANY($1::int[])
                """, memory_ids)

        # 向量搜索：事务提交后再算并保存 embedding，外部调用不占着行锁
        if shared.MEMORY_VECTOR_ENABLED and new_id:
            try:
                embedding = await db_search.compute_embedding(new_content)
                if embedding:
                    await db_search.save_memory_embedding(conn, new_id, embedding)
            except Exception as e:
                print(f"⚠️ 合并记忆embedding计算失败（id={new_id}）: {e}")

        return new_id


async def check_duplicate_memory(new_content: str, threshold: float = 0.7) -> dict:
    """检查新记忆是否与现有记忆重复

    三层去重策略：
    1. 精确匹配：内容完全相同
    2. 包含关系：新内容包含旧内容，或旧内容包含新内容
    3. 关键词重叠度：Jaccard 相似度 > threshold

    Returns:
        {
            "is_duplicate": bool,
            "reason": str,  # "exact" / "containment" / "similarity"
            "matched_id": int or None,
            "similarity": float or None
        }
    """
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        # 获取所有活跃记忆
        rows = await conn.fetch("""
            SELECT id, content FROM memories
            WHERE is_active = TRUE
        """)

        new_content_lower = new_content.strip().lower()
        new_keywords = set(db_search.extract_search_keywords(new_content))

        for row in rows:
            old_content = row['content']
            old_content_lower = old_content.strip().lower()

            # 第一层：精确匹配
            if new_content_lower == old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "exact",
                    "matched_id": row['id'],
                    "similarity": 1.0
                }

            # 第二层：包含关系
            if new_content_lower in old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment",
                    "matched_id": row['id'],
                    "similarity": len(new_content) / len(old_content)
                }
            if old_content_lower in new_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment_update",
                    "matched_id": row['id'],
                    "similarity": len(old_content) / len(new_content)
                }

            # 第三层：关键词重叠度（Jaccard 相似度）
            old_keywords = set(db_search.extract_search_keywords(old_content))
            if new_keywords and old_keywords:
                intersection = new_keywords & old_keywords
                union = new_keywords | old_keywords
                similarity = len(intersection) / len(union) if union else 0

                if similarity > threshold:
                    return {
                        "is_duplicate": True,
                        "reason": "similarity",
                        "matched_id": row['id'],
                        "similarity": similarity
                    }

        return {
            "is_duplicate": False,
            "reason": None,
            "matched_id": None,
            "similarity": None
        }


async def update_memory_with_layer(memory_id: int, content: str = None,
                                    importance: int = None, title: str = None,
                                    layer: int = None, is_active: bool = None,
                                    is_pinned: bool = None):
    """更新记忆（支持三层架构新字段）"""
    updates = []
    params = []
    param_idx = 2  # $1 给 memory_id

    if content is not None:
        updates.append(
            f"source_content_intact = CASE "
            f"WHEN content IS DISTINCT FROM ${param_idx} THEN FALSE "
            f"ELSE source_content_intact END"
        )
        updates.append(f"content = ${param_idx}")
        params.append(content)
        param_idx += 1

    if importance is not None:
        updates.append(f"importance = ${param_idx}")
        params.append(importance)
        param_idx += 1

    if title is not None:
        updates.append(f"title = ${param_idx}")
        params.append(title)
        param_idx += 1

    if layer is not None:
        updates.append(f"layer = ${param_idx}")
        params.append(layer)
        param_idx += 1

    if is_active is not None:
        updates.append(f"is_active = ${param_idx}")
        params.append(is_active)
        param_idx += 1

    if is_pinned is not None:
        updates.append(f"is_pinned = ${param_idx}")
        params.append(is_pinned)
        param_idx += 1

    if not updates:
        return

    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if content is not None or is_active is not None or is_pinned is not None:
                await _check_pinned_budget(
                    conn,
                    memory_id,
                    content=content,
                    is_active=is_active,
                    is_pinned=is_pinned,
                )
            result = await conn.execute(
                f"UPDATE memories SET {', '.join(updates)} WHERE id = $1"
                + (" AND is_active = TRUE" if is_pinned is True else ""),
                memory_id, *params
            )
            return result == "UPDATE 1"


async def get_layer_statistics():
    """获取各层记忆的统计数据"""
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                layer,
                COUNT(*) as count,
                COUNT(*) FILTER (WHERE is_active = TRUE) as active_count
            FROM memories
            GROUP BY layer
            ORDER BY layer
        """)

        stats = {
            "layer_1": {"total": 0, "active": 0},  # 原始碎片
            "layer_2": {"total": 0, "active": 0},  # 事件记忆
            "layer_3": {"total": 0, "active": 0},  # 核心记忆
        }

        for row in rows:
            layer = row['layer'] or 1  # 默认为层级1
            key = f"layer_{layer}"
            if key in stats:
                stats[key] = {
                    "total": row['count'],
                    "active": row['active_count']
                }

        return stats


async def cleanup_old_fragments(days: int = 30):
    """清理指定天数前的归档碎片

    只清理满足以下条件的记忆：
    - layer = 1（原始碎片）
    - is_active = FALSE（已归档）
    - created_at 在 days 天之前
    - 不属于自动取代版本链

    Returns:
        {"deleted": 删除数量, "revert_disabled": 结束撤回能力的父记忆数量}
    """
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        cutoff_date = datetime.now() - timedelta(days=days)

        async with conn.transaction():
            rows = await conn.fetch("""
                SELECT memory.id
                FROM memories AS memory
                WHERE memory.layer = 1
                  AND memory.is_active = FALSE
                  AND memory.is_pinned = FALSE
                  AND memory.created_at < $1
                  AND memory.superseded_by IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM memories AS predecessor
                      WHERE predecessor.superseded_by = memory.id
                  )
                FOR UPDATE
            """, cutoff_date)
            fragment_ids = [int(row["id"]) for row in rows]
            if not fragment_ids:
                return {"deleted": 0, "revert_disabled": 0}

            result = await conn.execute("""
                UPDATE memories
                SET merged_from = NULL
                WHERE merged_from && $1::int[]
            """, fragment_ids)
            revert_disabled = int(result.split()[-1]) if result else 0

            result = await conn.execute("""
                DELETE FROM memories
                WHERE id = ANY($1::int[])
            """, fragment_ids)
            deleted = int(result.split()[-1]) if result else 0
            return {
                "deleted": deleted,
                "revert_disabled": revert_disabled,
            }


async def revert_merge(memory_id: int):
    """撤回合并操作

    恢复原始碎片（is_active = TRUE），删除合并后的事件记忆

    Args:
        memory_id: 要撤回的事件记忆ID

    Returns:
        {"status": "ok", "restored": 恢复的碎片数量}
        或 {"error": "错误信息"}
    """
    pool = await db_core.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                SELECT memory.id, memory.layer, memory.merged_from,
                       memory.superseded_by IS NOT NULL OR EXISTS (
                           SELECT 1
                           FROM memories AS predecessor
                           WHERE predecessor.superseded_by = memory.id
                       ) AS protected_by_supersession
                FROM memories AS memory
                WHERE memory.id = $1
                FOR UPDATE OF memory
            """, memory_id)

            if not row:
                return {"error": "记忆不存在"}

            if row['layer'] != 2:
                return {"error": "只能撤回事件记忆的合并"}

            if row.get('protected_by_supersession', False):
                return {"error": "版本链中的记忆不能撤回合并"}

            merged_from = row['merged_from']
            if not merged_from or len(merged_from) == 0:
                return {"error": "没有完整的合并来源，无法撤回"}

            source_rows = await conn.fetch("""
                SELECT id
                FROM memories
                WHERE id = ANY($1::int[])
                FOR UPDATE
            """, merged_from)
            source_ids = {int(source["id"]) for source in source_rows}
            expected_ids = set(merged_from)
            if source_ids != expected_ids:
                missing = sorted(expected_ids - source_ids)
                return {
                    "error": f"合并来源不完整，缺少 {len(missing)} 条，未执行撤回"
                }

            result = await conn.execute("""
                UPDATE memories SET is_active = TRUE
                WHERE id = ANY($1::int[])
            """, merged_from)
            restored = int(result.split()[-1]) if result else 0
            if restored != len(expected_ids):
                raise RuntimeError(
                    f"恢复来源数量不符: expected={len(expected_ids)}, restored={restored}"
                )

            await conn.execute("""
                DELETE FROM memories WHERE id = $1
            """, memory_id)

            return {"status": "ok", "restored": restored}
