"""请求 / 响应 Pydantic 模型（响应多为 dict，此处约束请求体与公共结构）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentTextIn(BaseModel):
    """新增纯文本笔记。"""

    title: str | None = None
    content: str = Field(..., description="笔记正文")
    tags: list[str] = Field(default_factory=list)
    source_type: str = "note"


class DocumentUrlIn(BaseModel):
    """导入网页。"""

    url: str
    title: str | None = None
    tags: list[str] = Field(default_factory=list)


class SearchIn(BaseModel):
    """检索请求。"""

    query: str = ""
    top_k: int = 6
    doc_ids: list[str] | None = None


class ChatIn(BaseModel):
    """聊天请求（SSE）。"""

    message: str = ""
    conversation_id: str | None = None
    doc_ids: list[str] | None = None
    top_k: int = 6
    reference_doc_id: str | None = None


class LLMSettingsIn(BaseModel):
    """模型配置写入项；``None`` 表示「不修改」。"""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    temperature: float | None = None


class SettingsIn(BaseModel):
    llm: LLMSettingsIn | None = None


class SettingsTestIn(BaseModel):
    """测试连接入参；省略字段时使用已保存配置。"""

    llm: LLMSettingsIn | None = None


class ZoneCreateIn(BaseModel):
    """新建分区。"""

    name: str = ""
    color: str | None = None
    description: str | None = None


class ZoneUpdateIn(BaseModel):
    """更新分区；未提供的字段保持不变。"""

    name: str | None = None
    color: str | None = None
    description: str | None = None


class DocumentZoneIn(BaseModel):
    """批量归档文档到分区（``zone_id=None`` = 移出到未分区）。"""

    doc_ids: list[str] = Field(default_factory=list)
    zone_id: str | None = None


class PlanCreateIn(BaseModel):
    """新建计划（循环计划或单日行程）。"""

    title: str = ""
    description: str | None = None
    color: str | None = None
    frequency: str = "daily"
    weekly_target: int | None = None
    start_date: str | None = None
    group_id: str | None = None
    plan_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None


class PlanUpdateIn(BaseModel):
    """部分更新计划；未提供的字段保持不变。"""

    title: str | None = None
    description: str | None = None
    color: str | None = None
    frequency: str | None = None
    weekly_target: int | None = None
    start_date: str | None = None
    archived: bool | None = None
    sort_order: int | None = None
    group_id: str | None = None
    plan_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None


class PlanGroupCreateIn(BaseModel):
    """新建计划分组。"""

    name: str = ""
    color: str | None = None
    description: str | None = None


class PlanGroupUpdateIn(BaseModel):
    """部分更新计划分组；未提供的字段保持不变。"""

    name: str | None = None
    color: str | None = None
    description: str | None = None
    sort_order: int | None = None


class CheckinIn(BaseModel):
    """打卡入参（date 缺省今天）。"""

    date: str | None = None
    note: str | None = None


class ExtendItemIn(BaseModel):
    """待审核的外部资料条目。"""

    url: str = ""
    title: str | None = None
    snippet: str | None = None


class ExtendSearchIn(BaseModel):
    """联网搜索候选。"""

    question: str = ""
    queries: list[str] | None = None
    conversation_id: str | None = None
    max_results: int | None = None


class ExtendCommitIn(BaseModel):
    """审核入库。"""

    items: list[ExtendItemIn] = Field(default_factory=list)
    zone_id: str | None = None


class ExtendRejectIn(BaseModel):
    """忽略反馈。"""

    items: list[ExtendItemIn] = Field(default_factory=list)
    query: str | None = None


class EnrichIn(BaseModel):
    """知识库智能整理请求。"""

    scope: str = "pdf"
    doc_ids: list[str] | None = None
    force: bool = False
    limit: int = 12


class ReseedIn(BaseModel):
    force: bool = True


class Citation(BaseModel):
    index: int
    doc_id: str
    doc_title: str
    chunk_id: str
    source_type: str
    snippet: str
    score: float
