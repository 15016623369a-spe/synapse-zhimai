"""pytest 公共夹具与测试工具。

每个测试使用独立的临时数据目录（``SYNAPSE_DATA_DIR``），互不污染；
测试不访问真实外网：URL 抓取与 LLM 调用均通过 httpx.MockTransport 注入。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

# ---------------------------------------------------------------------------
# 演示语料（覆盖多主题、概念互相重叠，便于验证检索与图谱）
# ---------------------------------------------------------------------------

DOC_RAG = """# RAG 检索增强生成实践笔记

## 为什么要做 RAG
大模型的知识有截止时间，也访问不到我们的私有文档。检索增强生成（RAG）的思路是「先检索、再生成」：把文档切分成 450 到 650 字的片段，做向量化后存入索引；用户提问时先检索出最相关的片段，再把片段作为上下文交给大模型，让它基于资料回答，并用 [1][2] 标注引用来源。

## 关键参数
分块大小 450 到 650 字，块间重叠 80 字；本地向量维度 384；检索同时跑 BM25 和向量余弦，再用 RRF 融合排序，公式是 1/(60+rank)。实测在 200 篇个人笔记上，回答命中率从 68% 提升到 86%，引用错误率从 21% 降到 6%。

## 常见坑
一是分块太大，上下文噪声多；二是引用编号和资料顺序不一致；三是模型服务挂掉后整个问答不可用。所以本系统做了本地检索模式兜底：没有配置大模型时，直接从检索结果里抽取关键句子作答，保证完全离线也能用。

## 引用规范
引用编号从 1 开始，必须和传给大模型的资料顺序一致。每段资料截断到 1200 字，避免上下文过长。前端在答案下方展示参考来源卡片，点击可以预览片段。
"""

DOC_VECTOR = """# 向量检索与混合检索笔记

## 向量检索
把文本转成向量之后，可以用余弦相似度衡量语义接近程度。本地实现用 384 维哈希向量：先分词，再把词和中文 bigram 用 blake2b 哈希到固定维度，最后做 L2 归一化。优点是零依赖、离线可用；缺点是语义精度不如真正的 embedding 模型。

## 混合检索
只用向量检索，关键词精确匹配会吃亏；只用 BM25，语义泛化又会吃亏。所以把两路排序结果用 RRF 融合：分数等于 1/(60+BM25 排名) 加 1/(60+向量排名)，命中查询原句时再加 0.05 到 0.15 的加成。同一篇文档最多返回 2 个分块，避免引用集中。

## 分块经验
分块 450 到 650 字比较均衡，太短上下文不足，太长会稀释相关性。重叠 80 字可以缓解边界信息丢失。分块位置要记录 char_start 和 char_end，方便前端高亮原文。
"""

DOC_KNOWLEDGE = """# 卡片盒笔记法与知识管理

## 为什么要做知识管理
看了很多书、存了很多资料，等到要用的时候却找不到，这是知识管理最典型的失败。卡片盒笔记法的核心是：每张卡片只写一个想法，用自己的话重写，并在卡片之间建立连接。

## 我的工作流
收集阶段用剪藏工具把网页和 PDF 放进收件箱；整理阶段把长文拆成卡片，给卡片打标签；连接阶段每周把新卡片和旧卡片做关联，形成知识网络。检索的时候按标签和关键词查找，比翻文件夹快得多，检索命中率明显提升。

## 复习与输出
每周末挑 5 张卡片复述一遍，用费曼学习法检查自己有没有真正理解；每月把相关卡片整理成一篇文章。知识管理不是收藏，而是让笔记之间发生连接。好的知识网络会自己长出来。
"""

DOC_COFFEE = """# 手冲咖啡参数记录

## 基础参数
豆子 15 克，水温 92 度，研磨度中细（磨豆机刻度 6.5），粉水比 1:15，总注水 225 毫升。闷蒸 30 秒，之后分三段注水，总时长控制在 2 分 15 秒左右。

## 风味调整
如果喝起来太酸，先把水温提高 1 到 2 度，或者把研磨调细一点；如果太苦太涩，就降低水温、调粗研磨、缩短总时长。深烘豆用 88 到 90 度，浅烘豆用 92 到 94 度。

## 记录习惯
每次冲煮记录豆子、日期、水温、研磨度、时长和主观风味评分，连续记录两周就能找到适合自己的参数。咖啡豆开封后尽量在两周内喝完，存放要避光、密封、常温。
"""

DOC_SLEEP = """# 睡眠与恢复笔记

## 睡眠周期
一个完整睡眠周期大约 90 分钟，成年人每晚需要 4 到 5 个周期。入睡时间尽量固定，周末也不要晚睡超过一小时，否则周一会有明显的时差感。

## 恢复指标
早上看静息心率，比平时高 5 次以上说明恢复不足；再看主观精力评分（1 到 5 分），连续两天低于 3 分就应该降低训练强度。深睡比例比总时长更重要，睡前两小时不喝咖啡、不刷短视频。

## 训练安排
高强度间歇训练之后安排一天轻松跑，心率控制在最大心率的 60% 到 70%。恢复也是训练的一部分，睡眠是最好的恢复手段。
"""

CORPUS: dict[str, str] = {
    "rag-notes.md": DOC_RAG,
    "vector-search.md": DOC_VECTOR,
    "card-box-notes.md": DOC_KNOWLEDGE,
    "coffee-params.md": DOC_COFFEE,
    "sleep-recovery.md": DOC_SLEEP,
}

#: 上传时使用的一致 frontmatter
DEMO_MARKDOWN = """---
title: {title}
tags: {tags}
summary: {summary}
---

{body}
"""


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的数据目录，并清空可能影响测试的环境变量。"""
    path = tmp_path / "data"
    (path / "demo_docs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SYNAPSE_DATA_DIR", str(path))
    # 测试环境不启动知识巡检后台调度器（SPEC-V2-RERANK-PATROL 的约定）
    monkeypatch.setenv("SYNAPSE_PATROL_AUTOSTART", "0")
    for key in (
        "SYNAPSE_LLM_BASE_URL",
        "SYNAPSE_LLM_API_KEY",
        "SYNAPSE_LLM_MODEL",
        "SYNAPSE_LLM_TEMPERATURE",
        "SYNAPSE_EMBEDDINGS_BASE_URL",
        "SYNAPSE_EMBEDDINGS_API_KEY",
        "SYNAPSE_EMBEDDINGS_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    return path


@pytest.fixture()
def demo_files(data_dir: Path) -> dict[str, Path]:
    """把 CORPUS 写进临时演示文档目录。"""
    written: dict[str, Path] = {}
    for name, body in CORPUS.items():
        path = data_dir / "demo_docs" / name
        title = body.split("\n", 1)[0].lstrip("# ").strip()
        content = DEMO_MARKDOWN.format(
            title=title,
            tags=json.dumps(["AI", "知识管理"], ensure_ascii=False),
            summary=f"{title} 的演示摘要。",
            body=body,
        )
        path.write_text(content, encoding="utf-8")
        written[name] = path
    return written


@pytest.fixture()
def client(data_dir: Path) -> Iterator[TestClient]:
    """未注入演示文档的干净客户端（启动生命周期会初始化 DB）。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded_client(demo_files: dict[str, Path]) -> Iterator[TestClient]:
    """启动时自动载入演示文档的客户端。"""
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 测试辅助函数
# ---------------------------------------------------------------------------


def add_note(
    client: TestClient,
    title: str,
    content: str,
    tags: list[str] | None = None,
) -> dict:
    response = client.post(
        "/api/documents/text",
        json={"title": title, "content": content, "tags": tags or []},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    return payload["document"]


def seed_corpus(client: TestClient) -> dict[str, dict]:
    """通过 API 建 4 篇文档，返回 {key: document}。"""
    documents = {
        "rag": add_note(client, "RAG 检索增强生成实践笔记", DOC_RAG, ["AI", "RAG"]),
        "vector": add_note(client, "向量检索与混合检索笔记", DOC_VECTOR, ["AI", "检索"]),
        "knowledge": add_note(client, "卡片盒笔记法与知识管理", DOC_KNOWLEDGE, ["知识管理", "笔记"]),
        "coffee": add_note(client, "手冲咖啡参数记录", DOC_COFFEE, ["生活", "咖啡"]),
    }
    return documents


def upload_file(
    client: TestClient,
    filename: str,
    content: bytes,
    *,
    tags: str | None = None,
) -> dict:
    data: dict[str, object] = {"files": (filename, content)}
    if tags is not None:
        data["tags"] = tags
    response = client.post("/api/documents/upload", files=data)
    assert response.status_code == 200, response.text
    return response.json()


def parse_sse(raw: str) -> list[dict]:
    """把 SSE 文本解析成 ``[{event, data}]``。"""
    events: list[dict] = []
    event_name: str | None = None
    data_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
        elif not line.strip():
            if event_name is not None:
                payload = json.loads("\n".join(data_lines)) if data_lines else {}
                events.append({"event": event_name, "data": payload})
            event_name, data_lines = None, []
    if event_name is not None:
        payload = json.loads("\n".join(data_lines)) if data_lines else {}
        events.append({"event": event_name, "data": payload})
    return events


def chat_events(
    client: TestClient,
    message: str,
    conversation_id: str | None = None,
    top_k: int = 6,
    doc_ids: list[str] | None = None,
) -> tuple[list[dict], str]:
    """发送聊天请求，返回 (events, 原始响应文本)。"""
    response = client.post(
        "/api/chat",
        json={
            "message": message,
            "conversation_id": conversation_id,
            "doc_ids": doc_ids,
            "top_k": top_k,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text), response.text


def events_by_name(events: list[dict], name: str) -> list[dict]:
    return [event["data"] for event in events if event["event"] == name]


def joined_deltas(events: list[dict]) -> str:
    return "".join(str(data.get("text", "")) for data in events_by_name(events, "delta"))
