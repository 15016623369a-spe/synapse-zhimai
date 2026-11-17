"""检索 V3 测试：查询意图路由 + 文档级两阶段召回 + 多查询合并。

覆盖 SPEC-RETRIEVAL-V3：
- query_intent：set/compare/multi/fact 判定，expand_queries 数量/去重/保留原问题；
- 文档级召回：多文档轮转覆盖、每文档 ≤2、top_k 契约、doc_ids 限定；
- set：引号 rare term 扩展 + rare-term 豁免（低 coverage 也入选）；
- compare / multi：实体 A/B、子问题分别被检索到；
- fact 单文档 Top-1 回归；API mode/expanded_queries/doc_count 与旧字段兼容。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services import query_intent, retrieval

from .conftest import add_note

RESULT_KEYS = {
    "chunk_id",
    "doc_id",
    "doc_title",
    "text",
    "snippet",
    "score",
    "source_type",
}


def _doc_ids(results: list[dict]) -> list[str]:
    return list(dict.fromkeys(str(item["doc_id"]) for item in results))


def _note(client: TestClient, title: str, content: str, tags: list[str] | None = None) -> dict:
    return add_note(client, title, content, tags or [])


# ---------------------------------------------------------------------------
# 查询意图路由
# ---------------------------------------------------------------------------


def test_classify_query_modes() -> None:
    assert query_intent.classify_query("哪些笔记里出现了「预算」这个概念？") == "set"
    assert query_intent.classify_query("列出所有包含 RAG 的文档") == "set"
    assert query_intent.classify_query("RAG 检索和卡片盒笔记法的区别是什么？") == "compare"
    assert query_intent.classify_query("睡眠和跑步相比哪个更重要？") == "compare"
    assert query_intent.classify_query("深度工作和间隔重复分别是怎么安排的？") == "multi"
    assert query_intent.classify_query("分块大小和重叠参数是多少？") == "fact"
    assert query_intent.classify_query("我的手冲咖啡水温是多少？") == "fact"
    assert query_intent.classify_query("") == "fact"


def test_expand_queries_compare_split_entities() -> None:
    queries = query_intent.expand_queries("RAG 检索和卡片盒笔记法的区别是什么？")
    assert 2 <= len(queries) <= 3
    assert queries[0] == "RAG 检索和卡片盒笔记法的区别是什么？"  # 原问题保留在首位
    joined = " ".join(queries[1:])
    assert "RAG" in joined and "卡片盒" in joined
    assert len(set(queries)) == len(queries)


def test_expand_queries_multi_split_clauses() -> None:
    queries = query_intent.expand_queries("深度工作和间隔重复分别是怎么安排的？")
    assert len(queries) <= 3
    assert queries[0].startswith("深度工作和间隔重复")
    joined = " ".join(queries)
    assert "深度工作" in joined and "间隔重复" in joined


def test_expand_queries_set_extracts_quoted_term() -> None:
    queries = query_intent.expand_queries("哪些笔记里出现了「预算」这个概念？")
    assert queries[0].startswith("哪些笔记")
    assert any("预算" in query for query in queries[1:])
    assert len(queries) <= 3
    assert len(set(queries)) == len(queries)
    # 无引号 set 问题也能给出扩展（或仅原问题）
    no_quote = query_intent.expand_queries("哪些概念连接了 AI 和写作？")
    assert no_quote and no_quote[0].startswith("哪些概念")
    assert len(no_quote) <= 3


def test_expand_queries_fact_only_original() -> None:
    assert query_intent.expand_queries("我的手冲咖啡水温是多少？") == [
        "我的手冲咖啡水温是多少？"
    ]


# ---------------------------------------------------------------------------
# 文档级两阶段召回
# ---------------------------------------------------------------------------


def test_multi_doc_question_covers_more_documents(client: TestClient) -> None:
    documents = []
    for index in range(5):
        documents.append(
            _note(
                client,
                f"项目进展记录 {index}",
                f"这篇笔记记录数据管道 pipeline 和检索 retrieval 的进展、指标与复盘，"
                f"编号 {index}，还有故障处理流程与经验总结。" * 4,
                ["进展", "复盘"],
            )
        )
    results = client.post(
        "/api/search", json={"query": "pipeline retrieval 的进展分别如何？", "top_k": 6}
    ).json()["results"]
    assert results
    assert len(results) <= 6
    doc_ids = _doc_ids(results)
    # 轮转配额：小 top_k 也尽量覆盖不同文档（至少 4 篇）
    assert len(doc_ids) >= 4
    per_doc: dict[str, int] = {}
    for item in results:
        per_doc[item["doc_id"]] = per_doc.get(item["doc_id"], 0) + 1
    assert all(count <= 2 for count in per_doc.values())
    assert set(doc_ids) <= {document["id"] for document in documents}


def test_set_mode_rare_term_low_coverage_still_included(client: TestClient) -> None:
    # 16 篇文档构成小语料，rare term「预算」只出现在 1 篇且整体覆盖率低于 0.2
    for index in range(15):
        _note(
            client,
            f"普通工作记录 {index}",
            f"这是一篇关于日常工作安排与例行事项的记录，编号 {index}，"
            "内容包括会议、邮件和行程整理。" * 3,
            ["工作"],
        )
    budget = _note(
        client,
        "年度预算表",
        "本文整理年度预算：预算总额、成本结构与审批流程，以及与彩票、奖券无关的说明。" * 3,
        ["财务"],
    )
    from app import db

    with db.get_db() as conn:
        results = retrieval.search_chunks(
            conn, "哪些笔记里出现过预算、彩票和奖券的概念？", top_k=6
        )
    doc_ids = _doc_ids(results)
    assert budget["id"] in doc_ids


def test_compare_mode_retrieves_both_entities(client: TestClient) -> None:
    rag = _note(
        client,
        "RAG 检索增强实践",
        "RAG 检索增强生成：先检索知识库片段，再交给大模型生成回答，强调引用溯源。" * 6,
        ["AI", "检索"],
    )
    zettel = _note(
        client,
        "卡片盒笔记法实践",
        "卡片盒笔记法：把想法写成卡片，用双链把卡片连接成知识网络，强调人工索引与回顾。" * 6,
        ["知识管理"],
    )
    results = client.post(
        "/api/search",
        json={"query": "RAG 检索和卡片盒笔记法的区别是什么？", "top_k": 6},
    ).json()["results"]
    doc_ids = _doc_ids(results)
    assert rag["id"] in doc_ids
    assert zettel["id"] in doc_ids


def test_multi_mode_retrieves_both_clauses(client: TestClient) -> None:
    deep = _note(
        client,
        "深度工作安排",
        "深度工作：每天上午和下午各安排一块 90 分钟专注时间，关闭消息通知。" * 6,
        ["效率"],
    )
    spaced = _note(
        client,
        "间隔重复安排",
        "间隔重复：每天用 25 分钟复习卡片，按 1/2/4/7 天间隔安排，配合费曼复述。" * 6,
        ["学习"],
    )
    results = client.post(
        "/api/search",
        json={"query": "深度工作和间隔重复分别是怎么安排的？", "top_k": 8},
    ).json()["results"]
    doc_ids = _doc_ids(results)
    assert deep["id"] in doc_ids
    assert spaced["id"] in doc_ids


def test_fact_query_top1_not_regressed(client: TestClient) -> None:
    _note(client, "咖啡参数记录", "手冲咖啡粉水比 1:15，水温 92 度，研磨度中细。" * 5, ["咖啡"])
    target = _note(
        client,
        "跑步心率记录",
        "跑步心率区间：最大心率 185，Z2 区间约 130 到 148，按储备心率法划分五区。" * 5,
        ["跑步"],
    )
    _note(client, "睡眠恢复记录", "睡眠周期约 90 分钟，连续两天恢复不足就降低训练强度。" * 5, ["睡眠"])
    results = client.post(
        "/api/search", json={"query": "跑步心率 Z2 区间是多少？", "top_k": 1}
    ).json()["results"]
    assert len(results) == 1
    assert results[0]["doc_id"] == target["id"]
    assert set(results[0]) == RESULT_KEYS


def test_top_k_and_per_doc_contract(client: TestClient) -> None:
    for index in range(4):
        _note(
            client,
            f"检索笔记 {index}",
            "检索检索检索：向量检索与关键词检索的混合方案，RRF 融合排序。" * 8,
            ["检索"],
        )
    for top_k in (1, 3, 6, 12):
        results = client.post(
            "/api/search", json={"query": "向量检索和关键词检索怎么融合？", "top_k": top_k}
        ).json()["results"]
        assert 1 <= len(results) <= top_k
        per_doc: dict[str, int] = {}
        for item in results:
            per_doc[item["doc_id"]] = per_doc.get(item["doc_id"], 0) + 1
        assert all(count <= 2 for count in per_doc.values())
        scores = [item["score"] for item in results]
        assert scores == sorted(scores, reverse=True)
        assert all(0 < score <= 1 for score in scores)


def test_doc_ids_scope_with_document_stage(client: TestClient) -> None:
    coffee = _note(client, "咖啡参数", "手冲咖啡水温 92 度，研磨度中细，粉水比 1:15。" * 5, ["咖啡"])
    _note(client, "检索笔记", "向量检索与关键词检索的混合方案。" * 5, ["检索"])
    results = client.post(
        "/api/search",
        json={"query": "咖啡 水温 研磨度 粉水比", "top_k": 8, "doc_ids": [coffee["id"]]},
    ).json()["results"]
    assert results
    assert all(item["doc_id"] == coffee["id"] for item in results)


def test_irrelevant_question_returns_no_results(client: TestClient) -> None:
    for index in range(4):
        _note(
            client,
            f"检索笔记 {index}",
            "向量检索、关键词检索、RRF 融合和引用溯源的实践记录。" * 8,
            ["检索"],
        )
    results = client.post(
        "/api/search", json={"query": "量子纠缠和超导量子比特的退相干时间？", "top_k": 6}
    ).json()["results"]
    assert results == []


def test_title_and_tag_bonus_prefers_tagged_document(client: TestClient) -> None:
    plain = _note(
        client,
        "普通工程记录",
        "向量检索与混合检索的工程实现，记录参数配置与回归测试结果。" * 6,
        ["工程"],
    )
    tagged = _note(
        client,
        "混合检索专题：向量检索与关键词检索",
        "向量检索与混合检索的工程实现，记录参数配置与回归测试结果。" * 6,
        ["向量检索", "混合检索", "检索"],
    )
    results = client.post(
        "/api/search", json={"query": "混合检索 向量检索 参数配置", "top_k": 6}
    ).json()["results"]
    doc_ids = _doc_ids(results)
    assert tagged["id"] in doc_ids
    # 标题/标签命中应排在同内容无标题命中的文档之前
    assert doc_ids.index(tagged["id"]) <= doc_ids.index(plain["id"])


# ---------------------------------------------------------------------------
# API 兼容
# ---------------------------------------------------------------------------


def test_search_api_adds_mode_fields_and_keeps_old_contract(client: TestClient) -> None:
    _note(
        client,
        "预算记录",
        "年度预算、成本结构与审批流程的整理记录。" * 5,
        ["财务"],
    )
    payload = client.post(
        "/api/search", json={"query": "哪些笔记里出现过「预算」的概念？", "top_k": 6}
    ).json()
    assert payload["query"] == "哪些笔记里出现过「预算」的概念？"
    assert payload["mode"] == "set"
    assert isinstance(payload["expanded_queries"], list)
    assert payload["expanded_queries"][0] == payload["query"]
    assert any("预算" in query for query in payload["expanded_queries"][1:])
    assert isinstance(payload["doc_count"], int)
    assert payload["doc_count"] == len(_doc_ids(payload["results"]))
    for result in payload["results"]:
        assert set(result) == RESULT_KEYS


def test_search_api_fact_mode_keeps_old_fields(client: TestClient) -> None:
    target = _note(
        client,
        "咖啡参数",
        "手冲咖啡粉水比 1:15，水温 92 度，研磨度中细，总时长 2 分 15 秒。" * 5,
        ["咖啡"],
    )
    payload = client.post(
        "/api/search", json={"query": "我的手冲咖啡水温是多少？", "top_k": 6}
    ).json()
    assert payload["mode"] == "fact"
    assert payload["expanded_queries"] == ["我的手冲咖啡水温是多少？"]
    assert payload["doc_count"] >= 1
    assert payload["results"][0]["doc_id"] == target["id"]


def test_per_doc_limit_enforced_with_few_documents(client: TestClient) -> None:
    """候选文档很少时，补足阶段也不能突破「每文档 ≤2」。"""
    target = _note(
        client,
        "检索专题长文",
        "向量检索与关键词检索的混合方案，RRF 融合排序与引用溯源。" * 30,  # 多 chunk
        ["检索"],
    )
    results = client.post(
        "/api/search",
        json={"query": "向量检索 关键词检索 混合方案", "top_k": 8, "doc_ids": [target["id"]]},
    ).json()["results"]
    assert results
    assert all(item["doc_id"] == target["id"] for item in results)
    assert len(results) <= 2
