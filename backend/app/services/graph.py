"""概念抽取与知识图谱构建 / 查询。

图节点 id：``doc:{doc_id}``、``concept:{concept_id}``、``tag:{tag}``。
边类型：mentions / tagged / related / cooccur（见 SPEC 5.4）。
每次文档增删后全量重建 ``links``（数据规模小，允许）。
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable

import jieba
import jieba.analyse

from .. import db
from ..utils import is_stopword, new_id, now_iso, parse_tags_json, round_score
from . import zones as zones_service

logger = logging.getLogger("synapse.graph")

jieba.setLogLevel(logging.WARNING)

#: 概念抽取版本：升级抽取规则后启动时会自动重建已入库文档的概念与图谱
CONCEPT_EXTRACTION_VERSION = "4"
MAX_CONCEPTS_PER_DOC = 8  # 契约允许 8–12，取 8 让图谱概念更「知识化」、噪声更少
MIN_CONCEPTS_PER_DOC = 8
CONCEPT_MAX_CHARS_CJK = 12
#: 只保留名词 / 动名词 / 英文术语 / 习语 / 形容词性名词，过滤时间量词、副词、动词等
CONCEPT_ALLOWED_POS = ("n", "nr", "ns", "nt", "nz", "vn", "eng", "j", "l", "an")
RELATED_MIN_JACCARD = 0.06
RELATED_TAG_MIN_WEIGHT = 0.18
RELATED_MAX_PER_DOC = 3
COOCCUR_MIN_DOCS = 2
COOCCUR_MIN_WEIGHT = 0.2
COOCCUR_MAX_PER_CONCEPT = 5

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LABEL_ALLOWED_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9+#.\-_ ]+$")
#: 序数词（第一轮 / 第二块…）与「数量 + 单位」（15分钟 / 3公里…）不作为概念
_ORDINAL_RE = re.compile(r"^第[一二三四五六七八九十百千0-9]+")
_MEASURE_RE = re.compile(
    r"^[0-9一二三四五六七八九十百千]+"
    r"(分钟|小时|天|周|月|年|块|次|个|元|公里|米|克|毫升|升|度)$"
)


# ---------------------------------------------------------------------------
# 概念抽取
# ---------------------------------------------------------------------------


def _concept_norm(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip()).lower()


def _valid_concept(label: str) -> bool:
    text = label.strip()
    if not text or len(text) > 24:
        return False
    if not _LABEL_ALLOWED_RE.match(text):
        return False
    if not any(ch.isalnum() for ch in text):
        return False
    if text.isdigit():
        return False
    if _CJK_RE.search(text):
        if not (2 <= len(text) <= CONCEPT_MAX_CHARS_CJK):
            return False
    elif len(text) < 2:
        return False
    if _ORDINAL_RE.match(text) or _MEASURE_RE.match(text):
        return False
    if is_stopword(text):
        return False
    # 分词边界错误产生的倒序词（如「题问」←「问题」）不是概念
    if len(text) == 2 and is_stopword(text[::-1]):
        return False
    return True


def extract_concepts(text: str, top_k: int = MAX_CONCEPTS_PER_DOC) -> list[dict[str, object]]:
    """从文档正文抽取 8–12 个概念，返回 ``[{label, norm, weight}]``。

    weight 已归一化到 0.2–1（同文档内最大者为 1），供 doc_concepts / mentions 使用。
    """
    if not text or not text.strip():
        return []
    candidate_weights: dict[str, float] = {}
    labels: dict[str, str] = {}

    try:
        tagged = jieba.analyse.extract_tags(
            text, topK=max(30, top_k * 3), withWeight=True, allowPOS=CONCEPT_ALLOWED_POS
        )
    except Exception:  # pragma: no cover - jieba 极端输入兜底
        logger.exception("jieba 关键词抽取失败")
        tagged = []

    for word, weight in tagged:
        label = str(word).strip()
        norm = _concept_norm(label)
        if not _valid_concept(label):
            continue
        if norm not in candidate_weights or weight > candidate_weights[norm]:
            candidate_weights[norm] = float(weight)
            labels[norm] = label

    # 不足 8 个时用词频补齐（仍然过滤停用词 / 单字）
    if len(candidate_weights) < MIN_CONCEPTS_PER_DOC:
        counts = Counter(
            word.strip()
            for word in jieba.lcut(text)
            if word.strip() and len(word.strip()) >= 2
        )
        for word, count in counts.most_common(200):
            if len(candidate_weights) >= MIN_CONCEPTS_PER_DOC:
                break
            norm = _concept_norm(word)
            if norm in candidate_weights or not _valid_concept(word):
                continue
            candidate_weights[norm] = float(count)
            labels[norm] = word

    ranked = sorted(candidate_weights.items(), key=lambda item: (-item[1], item[0]))[:top_k]
    if not ranked:
        return []
    max_weight = max(weight for _, weight in ranked) or 1.0
    return [
        {
            "label": labels[norm],
            "norm": norm,
            "weight": round_score(0.2 + 0.8 * (weight / max_weight)),
        }
        for norm, weight in ranked
    ]


# ---------------------------------------------------------------------------
# 数据读取辅助
# ---------------------------------------------------------------------------


def _doc_rows(conn) -> list:
    return db.fetch_all(
        conn,
        "SELECT d.id, d.title, d.tags, d.summary, d.created_at, d.zone_id, "
        "(SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunk_count "
        "FROM documents d ORDER BY d.created_at ASC",
    )


def _concept_pairs(conn) -> list:
    return db.fetch_all(
        conn,
        "SELECT dc.doc_id, dc.concept_id, dc.weight, c.label "
        "FROM doc_concepts dc JOIN concepts c ON c.id = dc.concept_id",
    )


# ---------------------------------------------------------------------------
# 图谱重建
# ---------------------------------------------------------------------------


def _related_links(
    doc_ids: list[str],
    doc_concepts: dict[str, dict[str, float]],
    doc_tags: dict[str, set[str]],
) -> list[tuple[str, str, str, str, float]]:
    """文档间 related 边：概念 Jaccard 相似 + 共享标签的低权重连接。"""
    pair_weights: dict[tuple[str, str], float] = {}
    for left in range(len(doc_ids)):
        for right in range(left + 1, len(doc_ids)):
            first, second = doc_ids[left], doc_ids[right]
            set_first = set(doc_concepts.get(first, {}))
            set_second = set(doc_concepts.get(second, {}))
            if not set_first or not set_second:
                continue
            intersection = len(set_first & set_second)
            if not intersection:
                continue
            jaccard = intersection / len(set_first | set_second)
            if jaccard >= RELATED_MIN_JACCARD:
                # 权重下限 0.16，保证默认 min_link_weight=0.15 时可见
                pair_weights[(first, second)] = round_score(max(jaccard, 0.16))
    # 共享标签也算一种连接（低权重），不覆盖已有的概念相似连接
    for left in range(len(doc_ids)):
        for right in range(left + 1, len(doc_ids)):
            first, second = doc_ids[left], doc_ids[right]
            if (first, second) in pair_weights or (second, first) in pair_weights:
                continue
            shared = len(doc_tags.get(first, set()) & doc_tags.get(second, set()))
            if shared <= 0:
                continue
            pair_weights[(first, second)] = round_score(
                min(0.4, RELATED_TAG_MIN_WEIGHT + 0.05 * (shared - 1))
            )

    ranked = sorted(
        ((weight, first, second) for (first, second), weight in pair_weights.items()),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    per_doc: Counter[str] = Counter()
    rows: list[tuple[str, str, str, str, float]] = []
    for weight, first, second in ranked:
        if per_doc[first] >= RELATED_MAX_PER_DOC or per_doc[second] >= RELATED_MAX_PER_DOC:
            continue
        source, target = (first, second) if first < second else (second, first)
        rows.append(
            (new_id("l"), f"doc:{source}", f"doc:{target}", "related", round_score(weight))
        )
        per_doc[first] += 1
        per_doc[second] += 1
    return rows


def _cooccur_links(
    doc_concepts: dict[str, dict[str, float]],
) -> list[tuple[str, str, str, str, float]]:
    concept_docs: dict[str, set[str]] = defaultdict(set)
    for doc_id, concept_weights in doc_concepts.items():
        for concept_id in concept_weights:
            concept_docs[concept_id].add(doc_id)

    # 统计「概念对共同出现在多少个文档中」
    concept_ids = sorted(concept_docs)
    pair_counts: Counter[tuple[str, str]] = Counter()
    for left in range(len(concept_ids)):
        for right in range(left + 1, len(concept_ids)):
            first, second = concept_ids[left], concept_ids[right]
            shared_docs = len(concept_docs[first] & concept_docs[second])
            if shared_docs >= COOCCUR_MIN_DOCS:
                pair_counts[(first, second)] = shared_docs

    if not pair_counts:
        return []
    max_count = max(pair_counts.values())
    candidates = [
        (count / max_count, first, second)
        for (first, second), count in pair_counts.items()
        if count / max_count >= COOCCUR_MIN_WEIGHT
    ]
    per_concept: Counter[str] = Counter()
    rows: list[tuple[str, str, str, str, float]] = []
    for weight, first, second in sorted(candidates, key=lambda item: -item[0]):
        if (
            per_concept[first] >= COOCCUR_MAX_PER_CONCEPT
            or per_concept[second] >= COOCCUR_MAX_PER_CONCEPT
        ):
            continue
        source, target = sorted((f"concept:{first}", f"concept:{second}"))
        rows.append((new_id("l"), source, target, "cooccur", round_score(weight)))
        per_concept[first] += 1
        per_concept[second] += 1
    return rows


def rebuild(conn) -> None:
    """全量重建 links，并清理无引用概念。"""
    conn.execute("DELETE FROM links")
    documents = _doc_rows(conn)
    pairs = _concept_pairs(conn)

    doc_concepts: dict[str, dict[str, float]] = defaultdict(dict)
    link_rows: list[tuple[str, str, str, str, float]] = []

    for pair in pairs:
        doc_id = str(pair["doc_id"])
        concept_id = str(pair["concept_id"])
        weight = min(1.0, max(0.2, float(pair["weight"])))
        doc_concepts[doc_id][concept_id] = weight
        link_rows.append(
            (
                new_id("l"),
                f"doc:{doc_id}",
                f"concept:{concept_id}",
                "mentions",
                round_score(weight),
            )
        )

    doc_tags: dict[str, set[str]] = {}
    for document in documents:
        doc_id = str(document["id"])
        tags = set(parse_tags_json(document["tags"]))
        doc_tags[doc_id] = tags
        for tag in tags:
            link_rows.append((new_id("l"), f"doc:{doc_id}", f"tag:{tag}", "tagged", 2.0))

    doc_ids = [str(document["id"]) for document in documents]
    link_rows.extend(_related_links(doc_ids, doc_concepts, doc_tags))
    link_rows.extend(_cooccur_links(doc_concepts))

    if link_rows:
        conn.executemany(
            "INSERT INTO links(id, source, target, type, weight) VALUES(?, ?, ?, ?, ?)",
            link_rows,
        )
    conn.execute(
        "DELETE FROM concepts WHERE id NOT IN (SELECT concept_id FROM doc_concepts)"
    )
    logger.info("图谱已重建：文档 %d 篇，命中 %d 条边", len(documents), len(link_rows))


# ---------------------------------------------------------------------------
# 图查询
# ---------------------------------------------------------------------------


def _degree_map(links: Iterable) -> Counter[str]:
    degrees: Counter[str] = Counter()
    for link in links:
        degrees[str(link["source"])] += 1
        degrees[str(link["target"])] += 1
    return degrees


def _doc_node(document, degree: int) -> dict[str, object]:
    return {
        "id": f"doc:{document['id']}",
        "type": "document",
        "label": str(document["title"]),
        "weight": int(document["chunk_count"]),
        "degree": int(degree),
        "doc_id": str(document["id"]),
        "tags": parse_tags_json(document["tags"]),
        "summary": document["summary"],
    }


def _concept_node(concept_id: str, label: str, weight: float, degree: int) -> dict[str, object]:
    return {
        "id": f"concept:{concept_id}",
        "type": "concept",
        "label": label,
        "weight": round_score(weight, 3),
        "degree": int(degree),
    }


def _tag_node(tag: str, weight: int, degree: int) -> dict[str, object]:
    return {
        "id": f"tag:{tag}",
        "type": "tag",
        "label": tag,
        "weight": int(weight),
        "degree": int(degree),
    }


def _node_degree(conn, node_id: str) -> int:
    value = db.fetch_value(
        conn,
        "SELECT COUNT(*) FROM links WHERE source = ? OR target = ?",
        (node_id, node_id),
        default=0,
    )
    return int(value or 0)


def build_graph(
    conn,
    max_concepts: int = 120,
    min_link_weight: float = 0.15,
    min_concept_degree: int = 0,
) -> dict[str, object]:
    """正在线图：全部文档 + 全部标签 + degree×weight 前 N 概念。

    ``min_concept_degree`` 为可选过滤（默认 0 = 保持原行为）；传 2 可让前端只展示
    至少连接 2 个节点的概念，图谱更干净。
    """
    documents = _doc_rows(conn)
    all_links = db.fetch_all(conn, "SELECT id, source, target, type, weight FROM links")
    degrees_full = _degree_map(all_links)

    concept_rows = db.fetch_all(
        conn,
        "SELECT c.id AS concept_id, c.label AS label, "
        "COALESCE(SUM(dc.weight), 0) AS weight "
        "FROM concepts c LEFT JOIN doc_concepts dc ON dc.concept_id = c.id "
        "GROUP BY c.id",
    )
    ranked: list[tuple[float, str, str, float]] = []
    for row in concept_rows:
        concept_id = str(row["concept_id"])
        weight = float(row["weight"] or 0.0)
        score = degrees_full.get(f"concept:{concept_id}", 0) * weight
        ranked.append((score, concept_id, str(row["label"]), weight))
    ranked.sort(key=lambda item: (-item[0], item[2]))
    limit_concepts = max(1, min(int(max_concepts), 500))
    ranked = ranked[:limit_concepts]
    ranked_by_id = {concept_id: (label, weight) for _s, concept_id, label, weight in ranked}

    tag_docs: Counter[str] = Counter()
    base_visible: set[str] = set()
    for document in documents:
        base_visible.add(f"doc:{document['id']}")
        for tag in parse_tags_json(document["tags"]):
            tag_docs[tag] += 1
    base_visible.update(f"tag:{tag}" for tag in tag_docs)

    concept_node_ids = {concept_id: f"concept:{concept_id}" for concept_id in ranked_by_id}
    visible = set(base_visible) | set(concept_node_ids.values())

    def filter_links(visible_ids: set[str]) -> list:
        return [
            link
            for link in all_links
            if float(link["weight"]) >= min_link_weight
            and str(link["source"]) in visible_ids
            and str(link["target"]) in visible_ids
        ]

    filtered_links = filter_links(visible)
    degrees = _degree_map(filtered_links)

    min_degree = max(0, int(min_concept_degree))
    if min_degree >= 1:
        for _iteration in range(3):
            kept = {
                concept_id
                for concept_id, node_id in concept_node_ids.items()
                if degrees.get(node_id, 0) >= min_degree
            }
            new_visible = set(base_visible) | {
                concept_node_ids[concept_id] for concept_id in kept
            }
            if new_visible == visible:
                break
            visible = new_visible
            filtered_links = filter_links(visible)
            degrees = _degree_map(filtered_links)

    nodes: list[dict[str, object]] = [
        _doc_node(document, degrees.get(f"doc:{document['id']}", 0))
        for document in documents
    ]
    for tag, count in sorted(tag_docs.items(), key=lambda item: (-item[1], item[0])):
        nodes.append(_tag_node(tag, count, degrees.get(f"tag:{tag}", 0)))
    for concept_id, (label, weight) in ranked_by_id.items():
        node_id = concept_node_ids[concept_id]
        if node_id not in visible:
            continue
        nodes.append(_concept_node(concept_id, label, weight, degrees.get(node_id, 0)))

    links = [
        {
            "id": str(link["id"]),
            "source": str(link["source"]),
            "target": str(link["target"]),
            "type": str(link["type"]),
            "weight": round_score(float(link["weight"])),
        }
        for link in filtered_links
    ]
    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "node_count": len(nodes),
            "link_count": len(links),
            "document_count": len(documents),
            "generated_at": now_iso(),
        },
    }


# ---------------------------------------------------------------------------
# 分区图谱（mode=zones）：每个区域独立子图，组与组之间零连线
# ---------------------------------------------------------------------------


def _zone_fields(zone_meta: dict[str, object]) -> dict[str, object]:
    return {
        "zone_id": zone_meta["id"],
        "zone_name": zone_meta["name"],
        "zone_color": zone_meta["color"],
    }


def _suffix_node_ref(ref: str, suffix: str) -> str:
    """文档节点 id 保持不变；概念 / 标签节点 id 追加区域后缀。"""
    if ref.startswith("doc:"):
        return ref
    return f"{ref}{suffix}"


def _map_zone_endpoint(
    ref: str,
    doc_ids: set[str],
    concept_ids: set[str],
    tags: set[str],
) -> str | None:
    """把全局边端点映射到组内节点；不属于本组则返回 None。"""
    if ref.startswith("doc:"):
        return ref if ref[4:] in doc_ids else None
    if ref.startswith("concept:"):
        return ref if ref[8:] in concept_ids else None
    if ref.startswith("tag:"):
        return ref if ref[4:] in tags else None
    return None


def _build_zone_subgraph(
    zone_meta: dict[str, object],
    group_documents: list,
    all_links: list,
    concept_labels: dict[str, str],
    pairs_by_doc: dict[str, dict[str, float]],
    max_concepts: int,
    min_link_weight: float,
    min_concept_degree: int,
) -> dict[str, list[dict[str, object]]]:
    """为单个区域构建隔离子图（节点 id 带 ``@zone`` 后缀，degree 组内重算）。"""
    suffix = zones_service.zone_suffix(zone_meta["id"])  # type: ignore[arg-type]
    doc_ids = {str(document["id"]) for document in group_documents}

    tags: Counter[str] = Counter()
    for document in group_documents:
        for tag in parse_tags_json(document["tags"]):
            tags[tag] += 1
    tag_set = set(tags)

    concept_weights: dict[str, float] = {}
    for document in group_documents:
        for concept_id, weight in pairs_by_doc.get(str(document["id"]), {}).items():
            concept_weights[concept_id] = concept_weights.get(concept_id, 0.0) + float(weight)
    concept_ids = set(concept_weights)

    internal_links: list[dict[str, object]] = []
    for link in all_links:
        if float(link["weight"]) < min_link_weight:
            continue
        source = _map_zone_endpoint(str(link["source"]), doc_ids, concept_ids, tag_set)
        target = _map_zone_endpoint(str(link["target"]), doc_ids, concept_ids, tag_set)
        if source is None or target is None:
            continue
        internal_links.append(
            {
                "id": str(link["id"]),
                "source": source,
                "target": target,
                "type": str(link["type"]),
                "weight": float(link["weight"]),
            }
        )

    degrees_all = _degree_map(internal_links)
    ranked = sorted(
        (
            (
                degrees_all.get(f"concept:{concept_id}", 0) * weight,
                concept_id,
                weight,
            )
            for concept_id, weight in concept_weights.items()
        ),
        key=lambda item: (-item[0], concept_labels.get(item[1], item[1])),
    )
    limit_concepts = max(1, min(int(max_concepts), 500))
    ranked = ranked[:limit_concepts]
    ranked_concepts: dict[str, float] = {
        concept_id: weight for _score, concept_id, weight in ranked
    }
    concept_node_ids = {
        concept_id: f"concept:{concept_id}" for concept_id in ranked_concepts
    }
    base_visible = {f"doc:{doc_id}" for doc_id in doc_ids} | {
        f"tag:{tag}" for tag in tag_set
    }
    visible = set(base_visible) | set(concept_node_ids.values())

    def filter_links(visible_ids: set[str]) -> list[dict[str, object]]:
        return [
            link
            for link in internal_links
            if str(link["source"]) in visible_ids and str(link["target"]) in visible_ids
        ]

    filtered_links = filter_links(visible)
    degrees = _degree_map(filtered_links)

    min_degree = max(0, int(min_concept_degree))
    if min_degree >= 1:
        for _iteration in range(3):
            kept = {
                concept_id
                for concept_id, node_id in concept_node_ids.items()
                if degrees.get(node_id, 0) >= min_degree
            }
            new_visible = set(base_visible) | {
                concept_node_ids[concept_id] for concept_id in kept
            }
            if new_visible == visible:
                break
            visible = new_visible
            filtered_links = filter_links(visible)
            degrees = _degree_map(filtered_links)

    nodes: list[dict[str, object]] = []
    for document in group_documents:
        node = _doc_node(document, degrees.get(f"doc:{document['id']}", 0))
        node.update(_zone_fields(zone_meta))
        nodes.append(node)
    for tag, count in sorted(tags.items(), key=lambda item: (-item[1], item[0])):
        node = _tag_node(tag, count, degrees.get(f"tag:{tag}", 0))
        node["id"] = f"tag:{tag}{suffix}"
        node.update(_zone_fields(zone_meta))
        nodes.append(node)
    for concept_id, weight in ranked_concepts.items():
        node_id = concept_node_ids[concept_id]
        if node_id not in visible:
            continue
        node = _concept_node(
            concept_id,
            concept_labels.get(concept_id, concept_id),
            weight,
            degrees.get(node_id, 0),
        )
        node["id"] = f"concept:{concept_id}{suffix}"
        node.update(_zone_fields(zone_meta))
        nodes.append(node)

    links = [
        {
            "id": f"{link['id']}{suffix}",
            "source": _suffix_node_ref(str(link["source"]), suffix),
            "target": _suffix_node_ref(str(link["target"]), suffix),
            "type": str(link["type"]),
            "weight": round_score(float(link["weight"])),
        }
        for link in filtered_links
    ]
    return {"nodes": nodes, "links": links}


def build_zones_graph(
    conn,
    max_concepts: int = 120,
    min_link_weight: float = 0.15,
    min_concept_degree: int = 0,
) -> dict[str, object]:
    """分区模式：按文档 zone_id 分组，每组独立构建子图，组间零连线。"""
    documents = _doc_rows(conn)
    zone_rows = zones_service.list_zone_rows(conn)
    known_zone_ids = {str(row["id"]) for row in zone_rows}
    all_links = db.fetch_all(conn, "SELECT id, source, target, type, weight FROM links")
    concept_labels = {
        str(row["id"]): str(row["label"])
        for row in db.fetch_all(conn, "SELECT id, label FROM concepts")
    }
    pairs_by_doc: dict[str, dict[str, float]] = defaultdict(dict)
    for pair in _concept_pairs(conn):
        pairs_by_doc[str(pair["doc_id"])][str(pair["concept_id"])] = float(pair["weight"])

    docs_by_zone: dict[str | None, list] = defaultdict(list)
    for document in documents:
        raw_zone = document["zone_id"]
        zone_id = str(raw_zone) if raw_zone is not None and str(raw_zone) in known_zone_ids else None
        docs_by_zone[zone_id].append(document)

    groups: list[tuple[dict[str, object], list]] = []
    for row in zone_rows:
        zone_id = str(row["id"])
        groups.append(
            (
                {"id": zone_id, "name": str(row["name"]), "color": str(row["color"])},
                docs_by_zone.get(zone_id, []),
            )
        )
    unassigned_documents = docs_by_zone.get(None, [])
    if unassigned_documents:
        groups.append((zones_service.unassigned_zone_meta(), unassigned_documents))

    nodes: list[dict[str, object]] = []
    links: list[dict[str, object]] = []
    zones_meta: list[dict[str, object]] = []
    for zone_meta, group_documents in groups:
        subgraph = _build_zone_subgraph(
            zone_meta,
            group_documents,
            all_links,
            concept_labels,
            pairs_by_doc,
            max_concepts,
            min_link_weight,
            min_concept_degree,
        )
        nodes.extend(subgraph["nodes"])
        links.extend(subgraph["links"])
        zones_meta.append(
            {
                "id": zone_meta["id"],
                "name": zone_meta["name"],
                "color": zone_meta["color"],
                "document_count": len(group_documents),
                "node_count": len(subgraph["nodes"]),
                "link_count": len(subgraph["links"]),
            }
        )
    return {
        "nodes": nodes,
        "links": links,
        "zones": zones_meta,
        "meta": {
            "node_count": len(nodes),
            "link_count": len(links),
            "document_count": len(documents),
            "zone_count": len(zones_meta),
            "generated_at": now_iso(),
        },
    }


def _resolve_labels(conn, entries: Iterable[dict[str, object]]) -> None:
    """把 related 条目的 label 从 id 后半段替换为真实名称。"""
    entry_list = list(entries)
    doc_ids = [
        str(entry["ref"]) for entry in entry_list if entry["type"] == "doc"
    ]
    concept_ids = [
        str(entry["ref"]) for entry in entry_list if entry["type"] == "concept"
    ]
    doc_titles: dict[str, str] = {}
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        for row in db.fetch_all(
            conn, f"SELECT id, title FROM documents WHERE id IN ({placeholders})", doc_ids
        ):
            doc_titles[str(row["id"])] = str(row["title"])
    concept_labels: dict[str, str] = {}
    if concept_ids:
        placeholders = ",".join("?" for _ in concept_ids)
        for row in db.fetch_all(
            conn, f"SELECT id, label FROM concepts WHERE id IN ({placeholders})", concept_ids
        ):
            concept_labels[str(row["id"])] = str(row["label"])
    for entry in entry_list:
        ref = str(entry.pop("ref"))
        if entry["type"] == "doc":
            entry["label"] = doc_titles.get(ref, ref)
        elif entry["type"] == "concept":
            entry["label"] = concept_labels.get(ref, ref)


def _link_related(conn, node_id: str) -> list[dict[str, object]]:
    rows = db.fetch_all(
        conn,
        "SELECT source, target, weight FROM links WHERE source = ? OR target = ?",
        (node_id, node_id),
    )
    related: dict[str, dict[str, object]] = {}
    for row in rows:
        other = str(row["target"]) if str(row["source"]) == node_id else str(row["source"])
        kind, ref = other.split(":", 1)
        weight = round_score(float(row["weight"]))
        if other in related:
            if weight > float(related[other]["weight"]):
                related[other]["weight"] = weight
            continue
        related[other] = {
            "id": other,
            "type": kind,
            "label": ref,
            "ref": ref,
            "weight": weight,
        }
    _resolve_labels(conn, related.values())
    ordered = sorted(
        related.values(), key=lambda item: (-float(item["weight"]), str(item["id"]))
    )
    return ordered[:12]


def _document_brief(document) -> dict[str, object]:
    return {
        "id": str(document["id"]),
        "title": str(document["title"]),
        "tags": parse_tags_json(document["tags"]),
        "summary": document["summary"],
    }


def _chunks_for_docs(conn, doc_ids: list[str], limit: int = 8) -> list[dict[str, object]]:
    if not doc_ids:
        return []
    placeholders = ",".join("?" for _ in doc_ids)
    rows = db.fetch_all(
        conn,
        f"SELECT c.id, c.doc_id, c.text, d.title AS doc_title "
        f"FROM chunks c JOIN documents d ON d.id = c.doc_id "
        f"WHERE c.doc_id IN ({placeholders}) ORDER BY c.doc_id, c.idx LIMIT ?",
        [*doc_ids, limit],
    )
    return [
        {
            "id": str(row["id"]),
            "doc_id": str(row["doc_id"]),
            "doc_title": str(row["doc_title"]),
            "text": str(row["text"]),
        }
        for row in rows
    ]


def _get_document_node(conn, doc_id: str) -> dict[str, object] | None:
    document = db.fetch_one(conn, "SELECT * FROM documents WHERE id = ?", (doc_id,))
    if document is None:
        return None
    chunk_count = int(
        db.fetch_value(conn, "SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,), 0) or 0
    )
    node = _doc_node(
        {
            "id": document["id"],
            "title": document["title"],
            "tags": document["tags"],
            "summary": document["summary"],
            "chunk_count": chunk_count,
        },
        _node_degree(conn, f"doc:{doc_id}"),
    )
    chunks = db.fetch_all(
        conn,
        "SELECT id, doc_id, text FROM chunks WHERE doc_id = ? ORDER BY idx LIMIT 12",
        (doc_id,),
    )
    return {
        "node": node,
        "docs": [_document_brief(document)],
        "chunks": [
            {
                "id": str(row["id"]),
                "doc_id": str(row["doc_id"]),
                "doc_title": str(document["title"]),
                "text": str(row["text"]),
            }
            for row in chunks
        ],
        "related": _link_related(conn, f"doc:{doc_id}"),
    }


def _get_concept_node(conn, concept_id: str) -> dict[str, object] | None:
    concept = db.fetch_one(conn, "SELECT * FROM concepts WHERE id = ?", (concept_id,))
    if concept is None:
        return None
    node_id = f"concept:{concept_id}"
    weight = float(
        db.fetch_value(
            conn,
            "SELECT COALESCE(SUM(weight), 0) FROM doc_concepts WHERE concept_id = ?",
            (concept_id,),
            default=0,
        )
        or 0
    )
    doc_rows = db.fetch_all(
        conn,
        "SELECT d.id, d.title, d.tags, d.summary, dc.weight AS link_weight "
        "FROM doc_concepts dc JOIN documents d ON d.id = dc.doc_id "
        "WHERE dc.concept_id = ? ORDER BY dc.weight DESC LIMIT 12",
        (concept_id,),
    )
    doc_ids = [str(row["id"]) for row in doc_rows]
    return {
        "node": _concept_node(
            concept_id, str(concept["label"]), weight, _node_degree(conn, node_id)
        ),
        "docs": [_document_brief(row) for row in doc_rows],
        "chunks": _chunks_for_docs(conn, doc_ids, limit=8),
        "related": _link_related(conn, node_id),
    }


def _get_tag_node(conn, tag: str) -> dict[str, object] | None:
    documents = [row for row in _doc_rows(conn) if tag in parse_tags_json(row["tags"])]
    if not documents:
        return None
    node_id = f"tag:{tag}"
    doc_ids = [str(row["id"]) for row in documents]
    return {
        "node": _tag_node(tag, len(documents), _node_degree(conn, node_id)),
        "docs": [_document_brief(row) for row in documents],
        "chunks": _chunks_for_docs(conn, doc_ids, limit=8),
        "related": _link_related(conn, node_id),
    }


# ---------------------------------------------------------------------------
# 节点详情（分区作用域，支持 @zone 后缀）
# ---------------------------------------------------------------------------


def _zone_context(conn, zone_id: str | None) -> dict[str, object]:
    """收集某区域的文档 / 概念 / 标签，用于隔离的节点详情查询。

    ``zone_id=None`` 表示未分区组；命名区域不存在时抛 ``ZoneNotFound``。
    """
    if zone_id is None:
        zone_meta: dict[str, object] = zones_service.unassigned_zone_meta()
        documents = [document for document in _doc_rows(conn) if document["zone_id"] is None]
    else:
        row = zones_service.get_zone_or_404(conn, zone_id)
        zone_meta = {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "color": str(row["color"]),
        }
        documents = [
            document
            for document in _doc_rows(conn)
            if document["zone_id"] is not None and str(document["zone_id"]) == zone_id
        ]

    doc_ids = {str(document["id"]) for document in documents}
    pairs_by_doc: dict[str, dict[str, float]] = defaultdict(dict)
    concept_docs: dict[str, dict[str, float]] = defaultdict(dict)
    for pair in _concept_pairs(conn):
        doc_id = str(pair["doc_id"])
        if doc_id not in doc_ids:
            continue
        concept_id = str(pair["concept_id"])
        weight = float(pair["weight"])
        pairs_by_doc[doc_id][concept_id] = weight
        concept_docs[concept_id][doc_id] = weight

    concept_labels: dict[str, str] = {}
    if concept_docs:
        placeholders = ",".join("?" for _ in concept_docs)
        for row in db.fetch_all(
            conn,
            f"SELECT id, label FROM concepts WHERE id IN ({placeholders})",
            list(concept_docs),
        ):
            concept_labels[str(row["id"])] = str(row["label"])

    tags: set[str] = set()
    for document in documents:
        tags.update(parse_tags_json(document["tags"]))
    return {
        "zone": zone_meta,
        "documents": documents,
        "document_by_id": {str(document["id"]): document for document in documents},
        "doc_ids": doc_ids,
        "concept_docs": concept_docs,
        "concept_labels": concept_labels,
        "tags": tags,
    }


def _zone_visible_endpoint(ref: str, ctx: dict[str, object]) -> str | None:
    return _map_zone_endpoint(
        ref,
        ctx["doc_ids"],  # type: ignore[arg-type]
        set(ctx["concept_docs"]),  # type: ignore[arg-type]
        ctx["tags"],  # type: ignore[arg-type]
    )


def _zone_node_degree(conn, node_ref: str, ctx: dict[str, object]) -> int:
    rows = db.fetch_all(
        conn,
        "SELECT source, target FROM links WHERE source = ? OR target = ?",
        (node_ref, node_ref),
    )
    degree = 0
    for row in rows:
        other = str(row["target"]) if str(row["source"]) == node_ref else str(row["source"])
        if _zone_visible_endpoint(other, ctx) is not None:
            degree += 1
    return degree


def _zone_related(
    conn, node_ref: str, ctx: dict[str, object], limit: int = 12
) -> list[dict[str, object]]:
    """只返回本区域内的关联节点；概念 / 标签 id 带对应 ``@zone`` 后缀。"""
    suffix = zones_service.zone_suffix(ctx["zone"]["id"])  # type: ignore[arg-type]
    document_by_id = ctx["document_by_id"]  # type: ignore[assignment]
    concept_labels = ctx["concept_labels"]  # type: ignore[assignment]
    rows = db.fetch_all(
        conn,
        "SELECT source, target, weight FROM links WHERE source = ? OR target = ?",
        (node_ref, node_ref),
    )
    entries: dict[str, dict[str, object]] = {}
    for row in rows:
        other = str(row["target"]) if str(row["source"]) == node_ref else str(row["source"])
        mapped = _zone_visible_endpoint(other, ctx)
        if mapped is None:
            continue
        kind, ref = mapped.split(":", 1)
        display_id = _suffix_node_ref(mapped, suffix)
        if kind == "doc":
            document = document_by_id.get(ref)
            label = str(document["title"]) if document is not None else ref
        elif kind == "concept":
            label = str(concept_labels.get(ref, ref))
        else:
            label = ref
        weight = round_score(float(row["weight"]))
        existing = entries.get(display_id)
        if existing is None or weight > float(existing["weight"]):
            entries[display_id] = {
                "id": display_id,
                "type": kind,
                "label": label,
                "weight": weight,
            }
    ordered = sorted(
        entries.values(), key=lambda item: (-float(item["weight"]), str(item["id"]))
    )
    return ordered[:limit]


def _get_zoned_document_node(
    conn, ctx: dict[str, object], doc_id: str
) -> dict[str, object] | None:
    document_by_id = ctx["document_by_id"]  # type: ignore[assignment]
    document = document_by_id.get(doc_id)
    if document is None:
        return None
    node = _doc_node(document, _zone_node_degree(conn, f"doc:{doc_id}", ctx))
    node.update(_zone_fields(ctx["zone"]))  # type: ignore[arg-type]
    chunks = db.fetch_all(
        conn,
        "SELECT id, doc_id, text FROM chunks WHERE doc_id = ? ORDER BY idx LIMIT 12",
        (doc_id,),
    )
    return {
        "node": node,
        "docs": [_document_brief(document)],
        "chunks": [
            {
                "id": str(row["id"]),
                "doc_id": str(row["doc_id"]),
                "doc_title": str(document["title"]),
                "text": str(row["text"]),
            }
            for row in chunks
        ],
        "related": _zone_related(conn, f"doc:{doc_id}", ctx),
    }


def _get_zoned_concept_node(
    conn, ctx: dict[str, object], concept_id: str
) -> dict[str, object] | None:
    concept_docs = ctx["concept_docs"]  # type: ignore[assignment]
    document_weights = concept_docs.get(concept_id)
    if not document_weights:
        return None
    suffix = zones_service.zone_suffix(ctx["zone"]["id"])  # type: ignore[arg-type]
    node_ref = f"concept:{concept_id}"
    node: dict[str, object] = {
        "id": f"{node_ref}{suffix}",
        "type": "concept",
        "label": str(ctx["concept_labels"].get(concept_id, concept_id)),  # type: ignore[attr-defined]
        "weight": round_score(sum(document_weights.values()), 3),
        "degree": _zone_node_degree(conn, node_ref, ctx),
    }
    node.update(_zone_fields(ctx["zone"]))  # type: ignore[arg-type]
    ordered_doc_ids = [
        doc_id
        for doc_id, _weight in sorted(
            document_weights.items(), key=lambda item: (-item[1], item[0])
        )
    ][:12]
    document_by_id = ctx["document_by_id"]  # type: ignore[assignment]
    document_rows = [
        document_by_id[doc_id] for doc_id in ordered_doc_ids if doc_id in document_by_id
    ]
    return {
        "node": node,
        "docs": [_document_brief(document) for document in document_rows],
        "chunks": _chunks_for_docs(conn, ordered_doc_ids, limit=8),
        "related": _zone_related(conn, node_ref, ctx),
    }


def _get_zoned_tag_node(
    conn, ctx: dict[str, object], tag: str
) -> dict[str, object] | None:
    documents = [
        document
        for document in ctx["documents"]  # type: ignore[union-attr]
        if tag in parse_tags_json(document["tags"])
    ]
    if not documents:
        return None
    suffix = zones_service.zone_suffix(ctx["zone"]["id"])  # type: ignore[arg-type]
    node_ref = f"tag:{tag}"
    node: dict[str, object] = {
        "id": f"{node_ref}{suffix}",
        "type": "tag",
        "label": tag,
        "weight": len(documents),
        "degree": _zone_node_degree(conn, node_ref, ctx),
    }
    node.update(_zone_fields(ctx["zone"]))  # type: ignore[arg-type]
    return {
        "node": node,
        "docs": [_document_brief(document) for document in documents],
        "chunks": _chunks_for_docs(
            conn, [str(document["id"]) for document in documents], limit=8
        ),
        "related": _zone_related(conn, node_ref, ctx),
    }


def get_node(
    conn, node_id: str, zone_id: str | None = None
) -> dict[str, object] | None:
    """节点详情；非法 / 不存在返回 None（由路由层转 404）。

    - 无 ``zone_id`` 且 id 不带 ``@zone`` 后缀：旧版全局行为；
    - ``zone_id`` 或 id 后缀 ``@z_x`` / ``@unassigned``：只在本区域内查询。
    """
    if not node_id or ":" not in node_id:
        return None

    has_suffix = False
    suffix_zone_id: str | None = None
    if "@" in node_id:
        base, raw_suffix = node_id.rsplit("@", 1)
        if base and raw_suffix and (
            raw_suffix == "unassigned" or zones_service.zone_exists(conn, raw_suffix)
        ):
            has_suffix = True
            suffix_zone_id = None if raw_suffix == "unassigned" else raw_suffix
            node_id = base

    scoped = has_suffix or zone_id is not None
    scoped_zone_id: str | None = None
    if zone_id is not None:
        requested_zone_id = None if zone_id == "unassigned" else zone_id
        if has_suffix and requested_zone_id != suffix_zone_id:
            return None  # 后缀与显式 zone_id 不匹配
        scoped_zone_id = requested_zone_id
    elif has_suffix:
        scoped_zone_id = suffix_zone_id

    kind, raw = node_id.split(":", 1)
    raw = raw.strip()
    if not raw:
        return None

    if scoped:
        ctx = _zone_context(conn, scoped_zone_id)
        if kind == "doc":
            return _get_zoned_document_node(conn, ctx, raw)
        if kind == "concept":
            return _get_zoned_concept_node(conn, ctx, raw)
        if kind == "tag":
            return _get_zoned_tag_node(conn, ctx, raw)
        return None

    if kind == "doc":
        return _get_document_node(conn, raw)
    if kind == "concept":
        return _get_concept_node(conn, raw)
    if kind == "tag":
        return _get_tag_node(conn, raw)
    return None
