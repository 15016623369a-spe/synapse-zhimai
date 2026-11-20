"""通用工具：ID / 时间 / 文本规范化 / frontmatter / 停用词。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# ID 与时间
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """当前 UTC 时间，格式 ``2026-11-16T08:00:00Z``。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    """生成带前缀的短 ID，如 ``d_ab12cd34ef56``。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def round_score(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")
_HSPACE_RE = re.compile(r"[ \t\f\v\u00a0\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """统一换行、压缩空白、去零宽字符；多次调用结果一致。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_RE.sub("", text)
    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.replace("\t", " ")
        line = _HSPACE_RE.sub(" ", line).strip()
        lines.append(line)
    text = "\n".join(lines)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def collapse_whitespace(text: str) -> str:
    return _HSPACE_RE.sub(" ", text.replace("\n", " ")).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """截断到 ``limit`` 字符（含省略号）。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + suffix


def make_summary(content: str, limit: int = 120) -> str:
    """无 frontmatter summary 时，取正文前 120 字并去掉 Markdown 标记。"""
    cleaned = re.sub(r"^#{1,6}\s*", "", content, flags=re.MULTILINE)
    cleaned = re.sub(r"^[\s>*+-]+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[*_`~#]+", "", cleaned)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", cleaned)
    cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cleaned)
    cleaned = collapse_whitespace(cleaned)
    return truncate(cleaned, limit)


def make_snippet(text: str, limit: int = 280) -> str:
    """生成约 ``limit`` 字的摘要片段，尽量在句末截断。"""
    compact = collapse_whitespace(text)
    if len(compact) <= limit:
        return compact
    head = compact[:limit]
    for mark in ("。", "！", "？", "；", ". ", "! ", "? ", "，", ", "):
        pos = head.rfind(mark)
        if pos >= limit * 0.6:
            return head[: pos + len(mark)].strip() + "…"
    return head.rstrip() + "…"


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点与换行切句。"""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text)]
    return [part for part in parts if part]


def estimate_tokens(text: str) -> int:
    """粗估 token 数：每汉字计 1，每个英文/数字词计 1。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    return cjk + words


# ---------------------------------------------------------------------------
# frontmatter（严格格式，不引入 PyYAML）
# ---------------------------------------------------------------------------

_FRONTMATTER_KEYS = ("title", "tags", "summary")


class FrontmatterError(ValueError):
    """frontmatter 解析失败。"""


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown frontmatter。

    返回 ``(meta, body)``；格式不合法时整体忽略 frontmatter，返回 ``({}, 原文)``。
    """
    source = text.lstrip("\ufeff")
    lines = source.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, source
    end = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end < 0:
        return {}, source

    meta: dict[str, Any] = {}
    try:
        for line in lines[1:end]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                raise FrontmatterError("缺少冒号")
            key, raw_value = stripped.split(":", 1)
            key = key.strip().lower()
            if key not in _FRONTMATTER_KEYS:
                continue
            raw_value = raw_value.strip()
            if key == "tags":
                parsed = json.loads(raw_value)
                if not isinstance(parsed, list) or not all(
                    isinstance(item, str) for item in parsed
                ):
                    raise FrontmatterError("tags 必须是 JSON 字符串数组")
                meta["tags"] = [item.strip() for item in parsed if item.strip()]
            else:
                meta[key] = _strip_quotes(raw_value)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}, source

    title = meta.get("title")
    if title is not None and not str(title):
        meta.pop("title", None)
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def looks_like_filename_title(title: object) -> bool:
    """判断标题是否像文件名 / 占位符（用于 enrich 时替换为正式标题）。"""
    text = str(title or "").strip()
    if not text:
        return True
    lower = text.lower()
    if lower.endswith((".pdf", ".md", ".txt", ".docx", ".html", ".htm")):
        return True
    if "_" in text:
        return True
    if re.match(r"^(untitled|unknown|document|file|扫描件|未命名)", lower):
        return True
    if re.search(r"(19|20)\d{2}[.\-][^ ]*_", text):
        return True
    return False


def looks_like_academic_text(text: object) -> bool:
    """粗判是否为英文学术正文（用于关键词提取策略）。"""
    sample = str(text or "")[:4000]
    if not sample:
        return False
    english = len(re.findall(r"[A-Za-z]{3,}", sample))
    chinese = len(re.findall(r"[\u4e00-\u9fff]", sample))
    return english >= 50 and english > chinese


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------


def normalize_tag(tag: str) -> str:
    """标签清洗：去首尾空白与引号，压缩空白，限制长度。"""
    cleaned = collapse_whitespace(str(tag)).strip().strip("\"'")
    cleaned = cleaned.replace('"', "").replace("\\", "")
    return cleaned[:32]


def normalize_tags(tags: Any) -> list[str]:
    """去重并保持顺序。"""
    if not tags:
        return []
    if isinstance(tags, str):
        candidates: list[Any] = tags.split(",")
    else:
        candidates = list(tags)
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        tag = normalize_tag(item)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= 8:
            break
    return result


def parse_tags_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


# ---------------------------------------------------------------------------
# 停用词（概念抽取 / 检索词过滤，约 100 个中文常用虚词 + 英文常见词）
# ---------------------------------------------------------------------------

STOPWORDS: frozenset[str] = frozenset(
    {
        # 代词 / 指代
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
        "这", "那", "这个", "那个", "这些", "那些", "这里", "那里", "哪里",
        "这样", "那样", "那么", "这么", "什么", "怎么", "怎样", "如何", "哪个",
        "自己", "大家", "别人", "咱们", "其", "其中", "其他", "其它", "另外",
        "多少", "为什么", "哪些", "哪种", "是否", "有没有", "是不是", "能不能",
        "怎么样", "几个", "几种", "关于", "如果", "这么", "那么",
        # 助词 / 语气
        "的", "了", "着", "过", "地", "得", "之", "所", "也", "都", "就",
        "又", "再", "才", "只", "很", "太", "更", "最", "还", "却", "并",
        "而", "且", "则", "乃", "吧", "呢", "啊", "呀", "吗", "嘛", "哦",
        # 连词 / 介词
        "和", "与", "及", "或", "以及", "并且", "而且", "但是", "然而", "不过",
        "因为", "所以", "由于", "因此", "如果", "虽然", "即使", "除了", "只要",
        "无论", "不管", "然后", "于是", "对于", "关于", "根据", "按照", "通过",
        "为了", "随着", "作为", "来自", "针对", "基于", "属于", "包括",
        # 动词 / 判断
        "是", "有", "没有", "没", "不", "不是", "不能", "不会", "不要", "别",
        "可以", "可能", "应该", "需要", "必须", "能够", "会", "要", "想", "让",
        "把", "被", "给", "从", "向", "往", "到", "于", "为", "对", "在",
        "做", "用", "使", "进行", "使用", "成为", "觉得", "认为", "知道",
        "出现", "提到", "写过", "写了", "说过", "聊聊", "讲讲", "说说", "看看",
        "ms", "cm", "km", "kg", "ml", "hz", "kb", "mb", "gb",
        # 泛化词 / 工作流口头禅（图谱概念去噪）
        "半年", "每周", "每月", "每天", "每年", "本周", "上周", "下周", "这周",
        "复盘", "格式", "模板", "任务", "工作", "记录", "结论", "提前", "人多",
        "阶段", "过程", "思路", "办法", "做法", "效果", "这次", "那次", "上次",
        "下次", "人们", "部分", "整体", "情况", "内容", "方式", "方法", "结果",
        "开始", "结束", "继续", "变成", "发现", "选择", "决定", "安排", "计划",
        "目标", "问题", "东西",
        # 时间 / 数量 / 泛指
        "时候", "时间", "现在", "今天", "昨天", "明天", "最近", "以后", "之前",
        "之后", "当时", "目前", "已经", "一直", "一起", "经常", "有时", "一次",
        "一些", "一个", "一种", "一直", "很多", "更多", "很少", "每个", "各种",
        "所有", "全部", "部分", "东西", "事情", "方面", "问题", "情况", "地方",
        "非常", "特别", "比较", "更加", "还是", "就是", "只是", "只有", "其实",
        # 时间量词 / 序数 / 计量单位（概念抽取会误抽，作为低价值词过滤）
        "分钟", "小时", "秒钟", "小时侯", "上午", "下午", "晚上", "早上", "中午",
        "左右", "大约", "大概", "第一", "第二", "第三", "第四", "第五",
        "公里", "毫升", "千克", "公斤", "摄氏", "一点", "一下", "一块",
        # 英文
        "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in",
        "on", "at", "for", "with", "by", "is", "are", "was", "were", "be",
        "been", "being", "this", "that", "these", "those", "it", "its", "as",
        "from", "we", "you", "they", "he", "she", "i", "our", "your", "their",
        "not", "no", "can", "will", "would", "should", "could", "have", "has",
        "had", "do", "does", "did", "about", "into", "over", "more", "most",
        "other", "some", "such", "than", "too", "very", "just", "also",
        # 英文疑问词 / 引述词（英文问题里几乎必出现，作为检索信号是噪声）
        "who", "whom", "whose", "what", "which", "when", "where", "why", "how",
        "whether", "according", "versus", "vs",
    }
)


def is_stopword(token: str) -> bool:
    return token.strip().lower() in STOPWORDS
