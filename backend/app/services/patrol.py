"""定时知识巡检与自动整理。

职责：
- 配置（settings 表）：``patrol_enabled``（默认关闭）、``patrol_interval_hours``（24，1–168）、
  ``patrol_auto_enrich``（默认开启）、``patrol_max_enrich``（3，1–12）、
  ``patrol_last_run`` / ``patrol_next_run`` / ``patrol_last_result``；
- ``run_patrol(trigger)``：自动整理 ``enrich_version=0`` 的待整理文档（≤ max_enrich，
  单篇失败不中断，批量后统一重建图谱一次），并生成知识库健康报告
  （待整理 / 重复标题 / 过时 >30 天 / 未分区 / 过短 <800 字 / 无标签）；
  只做元数据与派生整理，**不修改原文、不联网、不新增外部文档**；
- 后台调度：``start_scheduler()`` / ``stop_scheduler()`` 单例 asyncio task，
  每 60 秒根据 ``next_run`` 判断是否执行；``SYNAPSE_PATROL_AUTOSTART=0`` 时禁用。
  *main.py 由 Lead 在 lifespan 中集成，本模块不修改 main.py。*
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import db
from ..utils import now_iso, truncate
from . import enrich, graph, llm

logger = logging.getLogger("synapse.patrol")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_ENABLED = False
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_AUTO_ENRICH = True
DEFAULT_MAX_ENRICH = 3
INTERVAL_MIN, INTERVAL_MAX = 1, 168
MAX_ENRICH_MIN, MAX_ENRICH_MAX = 1, 12
STALE_DAYS = 30
SMALL_DOC_CHARS = 800
DUPLICATE_THRESHOLD = 0.9
DUPLICATE_PAIR_LIMIT = 10
SAMPLE_LIMIT = 5
TICK_SECONDS = 60

SETTING_ENABLED = "patrol_enabled"
SETTING_INTERVAL_HOURS = "patrol_interval_hours"
SETTING_AUTO_ENRICH = "patrol_auto_enrich"
SETTING_MAX_ENRICH = "patrol_max_enrich"
SETTING_LAST_RUN = "patrol_last_run"
SETTING_NEXT_RUN = "patrol_next_run"
SETTING_LAST_RESULT = "patrol_last_result"

#: 巡检运行锁（threading）——API 与调度线程共用，防重入
_RUN_LOCK = threading.Lock()
#: 调度器单例 task / 异步锁（lazy，避免在模块导入时绑定事件循环）
_scheduler_task: asyncio.Task | None = None
_scheduler_lock: asyncio.Lock | None = None


class PatrolConfigError(ValueError):
    """巡检配置校验失败（路由层转 400，message 为中文）。"""


# ---------------------------------------------------------------------------
# 时间 / 解析工具
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            parsed = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_due(now: datetime, next_run_at: str | None) -> bool:
    """``now >= next_run_at`` 即到期；next_run 为空或无法解析时视为到期（自愈）。"""
    if not next_run_at:
        return True
    target = _parse_iso(next_run_at)
    if target is None:
        return True
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= target


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


def _parse_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _parse_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def load_config(conn) -> dict[str, object]:
    """读取巡检配置（缺失/非法值回退默认值）。"""
    stored = db.settings_all(conn)
    last_run_at = str(stored.get(SETTING_LAST_RUN) or "").strip() or None
    next_run_at = str(stored.get(SETTING_NEXT_RUN) or "").strip() or None
    last_result_raw = stored.get(SETTING_LAST_RESULT)
    last_result: dict[str, object] | None = None
    if last_result_raw:
        try:
            parsed = json.loads(last_result_raw)
            if isinstance(parsed, dict):
                last_result = parsed
        except (TypeError, ValueError):
            last_result = None
    return {
        "enabled": _parse_bool(stored.get(SETTING_ENABLED), DEFAULT_ENABLED),
        "interval_hours": _parse_int(
            stored.get(SETTING_INTERVAL_HOURS),
            DEFAULT_INTERVAL_HOURS,
            INTERVAL_MIN,
            INTERVAL_MAX,
        ),
        "auto_enrich": _parse_bool(stored.get(SETTING_AUTO_ENRICH), DEFAULT_AUTO_ENRICH),
        "max_enrich": _parse_int(
            stored.get(SETTING_MAX_ENRICH),
            DEFAULT_MAX_ENRICH,
            MAX_ENRICH_MIN,
            MAX_ENRICH_MAX,
        ),
        "last_run_at": last_run_at,
        "next_run_at": next_run_at,
        "last_result": last_result,
    }


def update_config(conn, updates: dict[str, object]) -> dict[str, object]:
    """部分更新巡检配置（校验后写库）；返回最新配置。

    首个 ``enabled=True`` 且尚无 ``next_run`` 时，把 next_run 设为当前时间，
    调度器下一次 tick 会立即执行一次巡检。
    """
    known = {"enabled", "interval_hours", "auto_enrich", "max_enrich"}
    unknown = {key for key in updates if key not in known and updates[key] is not None}
    if unknown:
        raise PatrolConfigError(f"不支持的配置项：{', '.join(sorted(unknown))}")

    current = load_config(conn)
    if updates.get("enabled") is not None:
        db.setting_set(conn, SETTING_ENABLED, "true" if bool(updates["enabled"]) else "false")
    if updates.get("auto_enrich") is not None:
        db.setting_set(
            conn, SETTING_AUTO_ENRICH, "true" if bool(updates["auto_enrich"]) else "false"
        )
    if updates.get("interval_hours") is not None:
        try:
            interval = int(str(updates["interval_hours"]).strip())
        except (TypeError, ValueError) as exc:
            raise PatrolConfigError("巡检周期必须是整数小时") from exc
        if interval < INTERVAL_MIN or interval > INTERVAL_MAX:
            raise PatrolConfigError(f"巡检周期需在 {INTERVAL_MIN}–{INTERVAL_MAX} 小时之间")
        db.setting_set(conn, SETTING_INTERVAL_HOURS, str(interval))
    if updates.get("max_enrich") is not None:
        try:
            max_enrich = int(str(updates["max_enrich"]).strip())
        except (TypeError, ValueError) as exc:
            raise PatrolConfigError("每次自动整理数量必须是整数") from exc
        if max_enrich < MAX_ENRICH_MIN or max_enrich > MAX_ENRICH_MAX:
            raise PatrolConfigError(
                f"每次自动整理数量需在 {MAX_ENRICH_MIN}–{MAX_ENRICH_MAX} 篇之间"
            )
        db.setting_set(conn, SETTING_MAX_ENRICH, str(max_enrich))

    enabled_now = _parse_bool(
        db.setting_get(conn, SETTING_ENABLED), DEFAULT_ENABLED
    )
    next_run_at = db.setting_get(conn, SETTING_NEXT_RUN)
    if enabled_now and not str(next_run_at or "").strip():
        db.setting_set(conn, SETTING_NEXT_RUN, _format_iso(_now_utc()))
    _ = current  # 保留读取顺序语义：校验基于更新前的配置
    return load_config(conn)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def is_running() -> bool:
    """是否有巡检正在执行（线程安全）。"""
    return _RUN_LOCK.locked()


def status(conn) -> dict[str, object]:
    """巡检状态（供 GET /api/patrol/status 与前端卡片）。"""
    cfg = load_config(conn)
    total = db.table_count(conn, "documents")
    enriched = int(
        db.fetch_value(
            conn, "SELECT COUNT(*) FROM documents WHERE enrich_version > 0", default=0
        )
        or 0
    )
    pending = int(
        db.fetch_value(
            conn, "SELECT COUNT(*) FROM documents WHERE enrich_version = 0", default=0
        )
        or 0
    )
    return {
        "enabled": bool(cfg["enabled"]),
        "interval_hours": int(cfg["interval_hours"]),
        "auto_enrich": bool(cfg["auto_enrich"]),
        "max_enrich": int(cfg["max_enrich"]),
        "running": is_running(),
        "last_run_at": cfg["last_run_at"],
        "next_run_at": cfg["next_run_at"],
        "last_result": cfg["last_result"],
        "counts": {
            "pending_docs": pending,
            "total_documents": total,
            "enriched_documents": enriched,
        },
    }


# ---------------------------------------------------------------------------
# 巡检执行
# ---------------------------------------------------------------------------


def _pending_documents(conn) -> list[dict[str, str]]:
    rows = db.fetch_all(
        conn,
        "SELECT id, title FROM documents WHERE enrich_version = 0 "
        "ORDER BY created_at ASC, id ASC",
    )
    return [{"id": str(row["id"]), "title": str(row["title"] or "")} for row in rows]


def _auto_enrich(
    targets: list[dict[str, str]], llm_config: dict[str, object]
) -> tuple[list[dict[str, object]], bool]:
    """逐篇整理（每篇独立事务，失败不中断），成功后统一重建图谱一次。"""
    enriched: list[dict[str, object]] = []
    for target in targets:
        doc_id = target["id"]
        title = target["title"]
        try:
            with db.get_db() as conn:
                outcome = enrich.enrich_document(conn, doc_id, llm_config=llm_config)
        except Exception as exc:  # 单篇失败不影响后续
            logger.exception("知识巡检自动整理失败：%s", doc_id)
            enriched.append(
                {
                    "id": doc_id,
                    "title": title,
                    "ok": False,
                    "error": str(exc) or "整理失败",
                }
            )
            continue
        if outcome.get("skipped"):
            enriched.append(
                {
                    "id": doc_id,
                    "title": title,
                    "ok": False,
                    "error": str(outcome.get("reason") or "已整理"),
                }
            )
            continue
        enriched.append(
            {
                "id": doc_id,
                "title": str(outcome.get("title") or title),
                "ok": True,
                "error": None,
            }
        )

    graph_rebuilt = False
    if any(item["ok"] for item in enriched):
        try:
            with db.get_db() as conn:
                graph.rebuild(conn)
            graph_rebuilt = True
        except Exception:  # 图谱失败不影响巡检结果返回
            logger.exception("知识巡检后重建图谱失败")
    return enriched, graph_rebuilt


_TITLE_NOISE_RE = re.compile(
    r"[\s\u3000_\-—–·:：,，.。!！?？\"'“”‘’()（）\[\]【】<>《》/\\|]+"
)


def _normalize_title(title: object) -> str:
    return _TITLE_NOISE_RE.sub("", str(title or "").strip().lower())


def _sample(rows: list, limit: int = SAMPLE_LIMIT) -> list[dict[str, object]]:
    return [
        {"id": str(row["id"]), "title": str(row["title"] or "")} for row in rows[:limit]
    ]


def _duplicate_pairs(rows: list) -> list[dict[str, object]]:
    """标题归一化后相同或相似度 ≥0.9 的文档对。"""
    pairs: list[dict[str, object]] = []
    prepared = [(row, _normalize_title(row["title"])) for row in rows]
    for index, (left_row, left_title) in enumerate(prepared):
        if not left_title:
            continue
        for right_row, right_title in prepared[index + 1 :]:
            if not right_title:
                continue
            shorter, longer = sorted((len(left_title), len(right_title)))
            if longer == 0 or shorter / longer < 0.5:
                continue
            if left_title == right_title:
                similarity = 1.0
            else:
                similarity = difflib.SequenceMatcher(
                    None, left_title, right_title
                ).ratio()
            if similarity < DUPLICATE_THRESHOLD:
                continue
            pairs.append(
                {
                    "a": {"id": str(left_row["id"]), "title": str(left_row["title"] or "")},
                    "b": {
                        "id": str(right_row["id"]),
                        "title": str(right_row["title"] or ""),
                    },
                    "similarity": round(float(similarity), 2),
                }
            )
    pairs.sort(key=lambda item: (-float(item["similarity"]), str(item["a"]["id"])))
    return pairs


def _collect_issues(conn) -> dict[str, object]:
    """知识库健康报告：待整理 / 重复 / 过时 / 未分区 / 过短 / 无标签。"""
    rows = db.fetch_all(
        conn,
        "SELECT id, title, content, tags, zone_id, updated_at, enrich_version "
        "FROM documents ORDER BY created_at ASC, id ASC",
    )
    pending_rows = [row for row in rows if int(row["enrich_version"] or 0) == 0]

    cutoff = _now_utc() - timedelta(days=STALE_DAYS)
    stale_rows: list[dict[str, object]] = []
    for row in rows:
        updated = _parse_iso(row["updated_at"])
        if updated is not None and updated < cutoff:
            stale_rows.append(
                {
                    "id": str(row["id"]),
                    "title": str(row["title"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                }
            )
    orphan_rows = [
        row for row in rows if not str(row["zone_id"] or "").strip()
    ]
    small_rows: list[dict[str, object]] = []
    for row in rows:
        chars = len(str(row["content"] or ""))
        if chars < SMALL_DOC_CHARS:
            small_rows.append(
                {
                    "id": str(row["id"]),
                    "title": str(row["title"] or ""),
                    "chars": chars,
                }
            )
    no_tag_rows: list[dict[str, object]] = []
    for row in rows:
        try:
            parsed_tags = json.loads(str(row["tags"] or "[]"))
        except (TypeError, ValueError):
            parsed_tags = []
        if not isinstance(parsed_tags, list) or not [
            str(tag).strip() for tag in parsed_tags if str(tag).strip()
        ]:
            no_tag_rows.append(
                {"id": str(row["id"]), "title": str(row["title"] or "")}
            )

    pairs = _duplicate_pairs(rows)
    return {
        "pending_docs": {
            "count": len(pending_rows),
            "sample": _sample(pending_rows),
        },
        "duplicate_docs": {
            "count": len(pairs),
            "pairs": pairs[:DUPLICATE_PAIR_LIMIT],
        },
        "stale_docs": {
            "count": len(stale_rows),
            "sample": stale_rows[:SAMPLE_LIMIT],
        },
        "orphan_docs": {
            "count": len(orphan_rows),
            "sample": _sample(orphan_rows),
        },
        "small_docs": {
            "count": len(small_rows),
            "sample": small_rows[:SAMPLE_LIMIT],
        },
        "missing_tags": {
            "count": len(no_tag_rows),
            "sample": no_tag_rows[:SAMPLE_LIMIT],
        },
    }


ISSUE_LABELS: tuple[tuple[str, str], ...] = (
    ("pending_docs", "待整理"),
    ("duplicate_docs", "重复标题"),
    ("stale_docs", "过时"),
    ("orphan_docs", "未分区"),
    ("small_docs", "过短"),
    ("missing_tags", "无标签"),
)


def _build_summary(
    enriched: list[dict[str, object]], issues: dict[str, object]
) -> str:
    ok_count = sum(1 for item in enriched if item.get("ok"))
    failed_count = len(enriched) - ok_count
    if enriched:
        enrich_text = f"自动整理 {ok_count} 篇"
        if failed_count:
            enrich_text += f"（失败 {failed_count} 篇）"
    else:
        enrich_text = "未执行自动整理"
    issue_bits: list[str] = []
    for key, label in ISSUE_LABELS:
        payload = issues.get(key) or {}
        count = int(payload.get("count") or 0)  # type: ignore[union-attr]
        if count:
            issue_bits.append(f"{label} {count}")
    issue_text = (
        "发现问题：" + "、".join(issue_bits) + "。" if issue_bits else "未发现明显问题。"
    )
    return f"{enrich_text}；{issue_text}"


def _run_patrol_locked(trigger: str) -> dict[str, object]:
    """实际巡检流程（调用方已持有运行锁）。"""
    started_at = now_iso()
    started_perf = time.perf_counter()
    with db.get_db() as conn:
        cfg = load_config(conn)
    interval_hours = int(cfg["interval_hours"])
    auto_enrich = bool(cfg["auto_enrich"])
    max_enrich = int(cfg["max_enrich"])

    ok = True
    error = ""
    enriched: list[dict[str, object]] = []
    graph_rebuilt = False
    issues: dict[str, object] = {}
    try:
        llm_config = llm.get_config()
        with db.get_db() as conn:
            pending = _pending_documents(conn)
        if auto_enrich and pending:
            enriched, graph_rebuilt = _auto_enrich(
                pending[:max_enrich], llm_config
            )
        with db.get_db() as conn:
            issues = _collect_issues(conn)
    except Exception as exc:  # 巡检绝不向上抛错，返回 failed 信息
        ok = False
        error = str(exc) or exc.__class__.__name__
        logger.exception("知识巡检执行失败：%s", error)

    finish_moment = _now_utc()
    finished_at = _format_iso(finish_moment)
    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    summary = _build_summary(enriched, issues) if ok else f"知识巡检失败：{error}"
    result: dict[str, object] = {
        "ok": ok,
        "trigger": trigger,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "enriched": enriched,
        "graph_rebuilt": graph_rebuilt,
        "issues": issues,
        "summary": summary,
    }
    if error:
        result["error"] = error
    next_run_at = _format_iso(
        finish_moment + timedelta(hours=max(INTERVAL_MIN, interval_hours))
    )
    try:
        with db.get_db() as conn:
            db.setting_set(conn, SETTING_LAST_RUN, started_at)
            db.setting_set(conn, SETTING_NEXT_RUN, next_run_at)
            db.setting_set(
                conn, SETTING_LAST_RESULT, json.dumps(result, ensure_ascii=False)
            )
            db.add_activity(
                conn,
                "patrol",
                f"知识巡检（{trigger}）：{truncate(summary, 160, suffix='')}",
            )
    except Exception as exc:  # 保存失败也返回可读信息
        logger.exception("保存知识巡检结果失败")
        result["ok"] = False
        result["error"] = f"保存巡检结果失败：{exc}"
        ok = False
    return {
        "ok": ok,
        "running": False,
        "result": result,
        "summary": summary,
        "next_run_at": next_run_at,
    }


def run_patrol(trigger: str = "manual") -> dict[str, object]:
    """执行一次知识巡检（同步，阻塞）；已有巡检在运行时返回 running。

    返回 ``{"ok", "running", "result", "summary", "next_run_at"}``；
    ``running=True`` 表示本次没有执行（调用方应提示稍后再试）。
    """
    clean_trigger = str(trigger or "manual").strip() or "manual"
    if not _RUN_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "running": True,
            "message": "知识巡检正在运行中，请稍后再试",
            "result": None,
        }
    try:
        return _run_patrol_locked(clean_trigger)
    finally:
        _RUN_LOCK.release()


# ---------------------------------------------------------------------------
# 后台调度器（main.py lifespan 由 Lead 集成；不由本模块修改 main.py）
# ---------------------------------------------------------------------------


def autostart_disabled() -> bool:
    """``SYNAPSE_PATROL_AUTOSTART=0`` 时禁用调度器（测试环境）。"""
    return os.environ.get("SYNAPSE_PATROL_AUTOSTART", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def _get_scheduler_lock() -> asyncio.Lock:
    global _scheduler_lock
    if _scheduler_lock is None:
        _scheduler_lock = asyncio.Lock()
    return _scheduler_lock


async def _tick(now: datetime | None = None) -> bool:
    """一次调度检查；返回是否触发了巡检（便于单元测试）。"""
    with db.get_db() as conn:
        cfg = load_config(conn)
    if not cfg["enabled"]:
        return False
    moment = now or _now_utc()
    if not is_due(moment, cfg["next_run_at"]):  # type: ignore[arg-type]
        return False
    lock = _get_scheduler_lock()
    if lock.locked():
        logger.info("知识巡检仍在执行，跳过本次调度")
        return False
    async with lock:
        outcome = await asyncio.to_thread(run_patrol, "scheduled")
    if outcome.get("running"):
        logger.info("知识巡检正在执行（其它入口），跳过本次调度")
        return False
    logger.info(
        "定时知识巡检完成：%s", str(outcome.get("summary") or "")
    )
    return True


async def _scheduler_loop() -> None:
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # 单次 tick 异常不能让调度器退出
            logger.exception("知识巡检调度器 tick 异常")
        await asyncio.sleep(TICK_SECONDS)


async def start_scheduler() -> bool:
    """启动单例调度器 task；已运行或 ``SYNAPSE_PATROL_AUTOSTART=0`` 时返回 False。"""
    global _scheduler_task
    if autostart_disabled():
        logger.info("知识巡检调度器已禁用（SYNAPSE_PATROL_AUTOSTART=0）")
        return False
    if _scheduler_task is not None and not _scheduler_task.done():
        return False
    _scheduler_lock = asyncio.Lock()
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(), name="synapse-patrol-scheduler"
    )
    logger.info("知识巡检调度器已启动（每 %d 秒检查一次 next_run）", TICK_SECONDS)
    return True


async def stop_scheduler() -> None:
    """停止调度器 task（幂等）；供 lifespan 关闭时调用。"""
    global _scheduler_task, _scheduler_lock
    task = _scheduler_task
    _scheduler_task = None
    _scheduler_lock = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - 关闭阶段兜底
        logger.exception("知识巡检调度器停止时出现异常")
    logger.info("知识巡检调度器已停止")
