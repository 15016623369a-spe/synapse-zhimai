"""联网搜索 Provider：Bing（默认）/ SearXNG / mock + 学术来源（arXiv / OpenAlex）。

- ``search_web(query, max_results)``：网页搜索；
- ``arxiv_by_id`` / ``arxiv_search`` / ``openalex_by_doi``：学术来源（无需 Key）；
- ``detect_academic_intent`` / ``build_academic_context``：学术意图与 ID 提取；
- ``score_candidate`` / ``rank_and_filter_candidates``：候选相关度 + 来源质量打分过滤；
- ``plan_queries``：带上下文的确定性查询规划 + LLM 校验回退。

配置（环境变量，运行时可调）：
``SYNAPSE_SEARCH_PROVIDER=bing|searxng|mock``、``SYNAPSE_SEARCH_MAX_RESULTS=6``、
``SYNAPSE_SEARCH_TIMEOUT=20``、``SYNAPSE_SEARCH_ENABLED=1``、``SYNAPSE_SEARXNG_URL``、
``SYNAPSE_SEARCH_API_KEY``、``SYNAPSE_SEARCH_MOCK_RESULTS``（JSON 数组）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ..utils import is_stopword, truncate
from . import corpus, doc_ref, embedding, llm

logger = logging.getLogger("synapse.search")

#: 测试注入点：httpx 同步传输层（None 表示真实网络）。
HTTP_TRANSPORT: httpx.BaseTransport | None = None

PROVIDER_LABELS = {
    "bing": "Bing 网页搜索",
    "searxng": "SearXNG 网页搜索",
    "mock": "离线模拟搜索",
}
DEFAULT_PROVIDER = "bing"
DEFAULT_MAX_RESULTS = 6
DEFAULT_TIMEOUT = 20.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX_WORK_URL = "https://api.openalex.org/works"
OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
ARXIV_PDF_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"
ARXIV_ABS_TEMPLATE = "https://arxiv.org/abs/{arxiv_id}"

#: 学术意图下 Bing 只保留这些学术域名
ACADEMIC_DOMAINS: tuple[str, ...] = (
    "arxiv.org",
    "openreview.net",
    "aclanthology.org",
    "paperswithcode.com",
    "doi.org",
    "ieeexplore.ieee.org",
    "dl.acm.org",
    "link.springer.com",
    "sciencedirect.com",
    "nature.com",
    "science.org",
    "researchgate.net",
    "semanticscholar.org",
    "openalex.org",
    "pubmed.ncbi.nlm.nih.gov",
    "biorxiv.org",
    "ssrn.com",
    "github.com",
    "distill.pub",
)
WIKI_DOMAINS: tuple[str, ...] = ("wikipedia.org", "baike.baidu.com", "wikiwand.com", "wiki")
MEDIA_DOMAINS: tuple[str, ...] = (
    "36kr.com", "ifeng.com", "sina.com.cn", "qq.com", "cnbeta.com",
    "thepaper.cn", "news.qq.com", "techcrunch.com", "theverge.com",
    "wired.com", "medium.com", "sohu.com", "163.com",
)
COMMUNITY_DOMAINS: tuple[str, ...] = (
    "zhihu.com", "zhuanlan.zhihu.com", "baijiahao.baidu.com", "jingyan.baidu.com",
    "csdn.net", "cnblogs.com", "weibo.com", "douyin.com", "xiaohongshu.com",
    "jianshu.com", "segmentfault.com", "v2ex.com", "tieba.baidu.com", "toutiao.com",
    "bilibili.com", "weixin.qq.com", "mp.weixin.qq.com",
)
OFFICIAL_SUFFIXES: tuple[str, ...] = (".gov", ".gov.cn", ".edu", ".edu.cn", ".mil")
OFFICIAL_DOMAINS: tuple[str, ...] = (
    "openai.com", "deepseek.com", "anthropic.com", "python.org", "developer.mozilla.org",
    "w3.org", "ietf.org", "rfc-editor.org", "microsoft.com", "google.com",
)

AUTHORITY_SCORES = {
    "academic": 0.95,
    "official": 0.9,
    "wiki": 0.7,
    "media": 0.65,
    "community": 0.25,
    "unknown": 0.5,
}
AUTHORITY_LABELS = {
    "academic": "学术",
    "official": "官方",
    "wiki": "百科",
    "media": "媒体",
    "community": "社区",
    "unknown": "未知",
}

#: 指令 / 泛词：不能出现在检索词里（plan_queries 会清洗）
QUERY_FORBIDDEN_WORDS: tuple[str, ...] = (
    "总结", "请", "帮我", "帮忙", "核心内容", "主要内容", "主要贡献",
    "资料", "一下", "论文内容", "摘要", "告诉我", "summarize", "summary",
)
QUERY_FORBIDDEN_RE = re.compile(
    "|".join(re.escape(word) for word in QUERY_FORBIDDEN_WORDS), re.IGNORECASE
)

ACADEMIC_INTENT_PATTERN = re.compile(
    r"arxiv|doi|论文|paper|文献|研究|实验|摘要|abstract|preprint|会议|期刊|引用|citation",
    re.IGNORECASE,
)
SUMMARY_PATTERN = re.compile(
    r"总结|概括|摘要|核心内容|主要贡献|主要内容|讲了什么|论文内容|summarize|summary",
    re.IGNORECASE,
)

MOCK_FIXTURES: list[dict[str, str]] = [
    {
        "url": "https://example.com/rag-evaluation-guide",
        "title": "RAG 系统评估指标与实践指南",
        "snippet": "RAG 常用评估指标包括忠实度 faithfulness、答案相关性、上下文召回率与引用准确率，"
        "工程上通常配合人工抽检和问题集回归。",
    },
    {
        "url": "https://example.com/rag-chunking.md",
        "title": "检索增强生成的分块与召回优化",
        "snippet": "分块大小、重叠长度与混合检索权重会直接影响召回质量，建议用固定问题集做 A/B 调参。",
    },
    {
        "url": "https://example.com/vector-search-basics",
        "title": "向量检索与混合检索基础",
        "snippet": "BM25 与向量检索各有优势，RRF 融合可以在关键词精确匹配和语义泛化之间取得平衡。",
    },
]


class SearchUnavailable(RuntimeError):
    """搜索不可用（网络错误 / 未启用 / 空结果），message 为中文。"""


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    source: str
    authority: str = "unknown"
    relevance: float = 0.0
    pdf_url: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "source": self.source,
            "authority": self.authority,
            "relevance": round(float(self.relevance), 4),
            "pdf_url": self.pdf_url,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def provider_name() -> str:
    provider = _env("SYNAPSE_SEARCH_PROVIDER", DEFAULT_PROVIDER).lower()
    return provider if provider in PROVIDER_LABELS else DEFAULT_PROVIDER


def provider_label() -> str:
    return PROVIDER_LABELS[provider_name()]


def is_enabled() -> bool:
    return _env("SYNAPSE_SEARCH_ENABLED", "1") not in {"0", "false", "False", "no"}


def _max_results() -> int:
    try:
        value = int(_env("SYNAPSE_SEARCH_MAX_RESULTS", str(DEFAULT_MAX_RESULTS)))
    except ValueError:
        value = DEFAULT_MAX_RESULTS
    return max(1, min(value, 12))


def _timeout() -> float:
    try:
        value = float(_env("SYNAPSE_SEARCH_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        value = DEFAULT_TIMEOUT
    return max(5.0, min(value, 60.0))


def search_status() -> dict[str, object]:
    return {
        "provider": provider_name(),
        "enabled": is_enabled(),
        "label": provider_label(),
    }


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _clean_text(value: str | None, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return truncate(text, limit, suffix="")


def _valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _http_client() -> httpx.Client:
    return httpx.Client(
        timeout=_timeout(),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
        transport=HTTP_TRANSPORT,
    )


# ---------------------------------------------------------------------------
# 网页搜索 Provider
# ---------------------------------------------------------------------------


def _parse_bing_html(html: str, limit: int) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in soup.select("li.b_algo"):
        anchor = item.select_one("h2 a")
        if anchor is None:
            continue
        url = str(anchor.get("href") or "").strip()
        title = _clean_text(anchor.get_text(" ", strip=True), 120)
        if not _valid_url(url) or not title or url in seen:
            continue
        caption = item.select_one(".b_caption p") or item.select_one("p")
        snippet = _clean_text(caption.get_text(" ", strip=True) if caption else "", 300)
        seen.add(url)
        results.append(
            SearchResult(
                url=url,
                title=title,
                snippet=snippet,
                source="bing",
                authority=authority_category(url),
            )
        )
        if len(results) >= limit:
            break
    return results


def _bing_search(query: str, limit: int) -> list[SearchResult]:
    params = {"q": query, "count": str(max(limit, 10)), "setlang": "zh-Hans"}
    with _http_client() as client:
        response = client.get("https://cn.bing.com/search", params=params)
        response.raise_for_status()
    return _parse_bing_html(response.text, limit)


def _searxng_search(query: str, limit: int) -> list[SearchResult]:
    base_url = _env("SYNAPSE_SEARXNG_URL").rstrip("/")
    if not base_url:
        raise SearchUnavailable("未配置 SearXNG 地址（SYNAPSE_SEARXNG_URL）")
    headers = {"Accept": "application/json"}
    api_key = _env("SYNAPSE_SEARCH_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    with _http_client() as client:
        response = client.get(
            f"{base_url}/search",
            params={"q": query, "format": "json", "language": "zh-CN"},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in payload.get("results") or []:
        url = str(item.get("url") or "").strip()
        title = _clean_text(item.get("title"), 120)
        if not _valid_url(url) or not title or url in seen:
            continue
        seen.add(url)
        results.append(
            SearchResult(
                url=url,
                title=title,
                snippet=_clean_text(item.get("content"), 300),
                source="searxng",
                authority=authority_category(url),
            )
        )
        if len(results) >= limit:
            break
    return results


def _mock_results() -> list[dict[str, str]]:
    raw = _env("SYNAPSE_SEARCH_MOCK_RESULTS")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            fixtures: list[dict[str, str]] = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "")
                if not _valid_url(url):
                    continue
                fixtures.append(
                    {
                        "url": url,
                        "title": str(item.get("title") or url),
                        "snippet": str(item.get("snippet") or ""),
                    }
                )
            if fixtures:
                return fixtures
    return MOCK_FIXTURES


def _mock_search(query: str, limit: int) -> list[SearchResult]:
    return [
        SearchResult(
            url=item["url"],
            title=item["title"],
            snippet=item["snippet"],
            source="mock",
            authority=authority_category(item["url"]),
        )
        for item in _mock_results()[:limit]
    ]


def search_web(query: str, max_results: int | None = None) -> list[SearchResult]:
    """执行一次网页搜索；不可用时抛 ``SearchUnavailable``。"""
    if not is_enabled():
        raise SearchUnavailable("联网搜索已关闭（SYNAPSE_SEARCH_ENABLED=0）")
    keyword = str(query or "").strip()
    if not keyword:
        raise SearchUnavailable("搜索词不能为空")
    limit = max(1, min(int(max_results or _max_results()), 12))
    provider = provider_name()
    try:
        if provider == "mock":
            return _mock_search(keyword, limit)
        if provider == "searxng":
            return _searxng_search(keyword, limit)
        return _bing_search(keyword, limit)
    except SearchUnavailable:
        raise
    except httpx.HTTPError as exc:
        logger.warning("联网搜索失败（%s）：%s", provider, exc)
        raise SearchUnavailable("联网搜索暂不可用，请检查网络或配置 SearXNG") from exc
    except Exception as exc:  # 解析异常等兜底，不让搜索错误冒泡成 500
        logger.warning("联网搜索解析失败（%s）：%s", provider, exc)
        raise SearchUnavailable("联网搜索暂不可用，请检查网络或配置 SearXNG") from exc


# ---------------------------------------------------------------------------
# 学术来源 Provider：arXiv / OpenAlex
# ---------------------------------------------------------------------------


def _parse_arxiv_entry(entry: ET.Element) -> SearchResult | None:
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    raw_id = (entry.findtext("atom:id", default="", namespaces=namespace) or "").strip()
    title = _clean_text(
        entry.findtext("atom:title", default="", namespaces=namespace), 300
    )
    summary = _clean_text(
        entry.findtext("atom:summary", default="", namespaces=namespace), 500
    )
    if not title:
        return None
    arxiv_id = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id.strip())
    if not arxiv_id:
        return None
    url = ARXIV_ABS_TEMPLATE.format(arxiv_id=arxiv_id)
    pdf_url = ARXIV_PDF_TEMPLATE.format(arxiv_id=arxiv_id)
    return SearchResult(
        url=url,
        title=title,
        snippet=summary,
        source="arxiv",
        authority="academic",
        relevance=0.9,
        pdf_url=pdf_url,
        extra={"arxiv_id": arxiv_id},
    )


def _arxiv_request(params: dict[str, str], max_results: int) -> list[SearchResult]:
    try:
        with _http_client() as client:
            response = client.get(
                ARXIV_API, params={"max_results": str(max_results), **params}
            )
            response.raise_for_status()
            xml_text = response.text
    except httpx.HTTPError as exc:  # 429 限流 / 网络异常统一降级，不让 500 冒泡
        logger.info("arXiv 请求失败：%s", exc)
        raise SearchUnavailable("arXiv 检索暂不可用（可能被限流），请稍后再试") from exc
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SearchUnavailable("arXiv 返回内容解析失败") from exc
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    results: list[SearchResult] = []
    for entry in root.findall("atom:entry", namespace):
        parsed = _parse_arxiv_entry(entry)
        if parsed is not None:
            results.append(parsed)
        if len(results) >= max_results:
            break
    return results


def arxiv_by_id(arxiv_id: str, max_results: int = 3) -> list[SearchResult]:
    """按 arXiv ID 取元数据（标题/摘要/PDF 链接）。"""
    clean_id = re.sub(r"^arxiv[:\s/]*", "", str(arxiv_id or "").strip(), flags=re.IGNORECASE)
    clean_id = re.sub(r"v\d+$", "", clean_id)
    if not re.match(r"^\d{4}\.\d{4,5}$", clean_id):
        raise SearchUnavailable("arXiv ID 不合法")
    return _arxiv_request({"id_list": clean_id}, max_results)


def arxiv_search(title_terms: str, max_results: int = 5) -> list[SearchResult]:
    """按标题/关键词搜索 arXiv。"""
    terms = _clean_text(title_terms, 200)
    if not terms:
        raise SearchUnavailable("arXiv 搜索词不能为空")
    safe_terms = terms.replace('"', " ")
    return _arxiv_request({"search_query": f'all:"{safe_terms}"'}, max_results)


def _reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted_index.items():
        for index in indexes or []:
            positions.append((int(index), word))
    positions.sort()
    return " ".join(word for _position, word in positions)


def openalex_by_doi(doi: str, max_results: int = 2) -> list[SearchResult]:
    """按 DOI 查询 OpenAlex 元数据。"""
    clean_doi = str(doi or "").strip().lower()
    clean_doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", clean_doi)
    if not clean_doi.startswith("10."):
        raise SearchUnavailable("DOI 不合法")
    try:
        with _http_client() as client:
            response = client.get(f"{OPENALEX_WORK_URL}/https://doi.org/{clean_doi}")
            if response.status_code >= 400:
                raise SearchUnavailable("OpenAlex 未找到该 DOI")
            payload = response.json()
    except SearchUnavailable:
        raise
    except httpx.HTTPError as exc:
        logger.info("OpenAlex DOI 查询失败：%s", exc)
        raise SearchUnavailable("OpenAlex 暂不可用") from exc
    if not isinstance(payload, dict):
        return []
    title = _clean_text(payload.get("title"), 300)
    if not title:
        return []
    doi_url = f"https://doi.org/{clean_doi}"
    snippet = _clean_text(_reconstruct_abstract(payload.get("abstract_inverted_index")), 500)
    return [
        SearchResult(
            url=doi_url,
            title=title,
            snippet=snippet,
            source="openalex",
            authority="academic",
            relevance=0.85,
            extra={"doi": clean_doi},
        )
    ][:max_results]


def openalex_search(query: str, max_results: int = 5) -> list[SearchResult]:
    """按标题/关键词搜索 OpenAlex（无需 Key），返回学术候选。

    用于「多轮追问 + 引用文档」场景：用上下文里的英文术语检索相关论文，
    避免只依赖网页搜索得到无关的百科/词典结果。
    """
    terms = _clean_text(query, 200)
    if not terms:
        raise SearchUnavailable("OpenAlex 搜索词不能为空")
    limit = max(1, min(int(max_results or 5), 10))
    params = {
        "search": terms,
        "per-page": str(limit),
        "select": "id,title,doi,publication_year,abstract_inverted_index",
    }
    try:
        with _http_client() as client:
            response = client.get(OPENALEX_SEARCH_URL, params=params)
            if response.status_code >= 400:
                raise SearchUnavailable("OpenAlex 搜索暂不可用")
            payload = response.json()
    except SearchUnavailable:
        raise
    except httpx.HTTPError as exc:
        logger.info("OpenAlex 搜索失败：%s", exc)
        raise SearchUnavailable("OpenAlex 搜索暂不可用") from exc

    rows = payload.get("results") if isinstance(payload, dict) else None
    results: list[SearchResult] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"), 300)
        if not title:
            continue
        doi = re.sub(
            r"^https?://(dx\.)?doi\.org/", "", str(item.get("doi") or "")
        ).lower()
        if doi.startswith("10."):
            url = f"https://doi.org/{doi}"
        else:
            url = str(item.get("id") or "")
        if not _valid_url(url):
            continue
        results.append(
            SearchResult(
                url=url,
                title=title,
                snippet=_clean_text(
                    _reconstruct_abstract(item.get("abstract_inverted_index")), 500
                ),
                source="openalex",
                authority="academic",
                relevance=0.8,
                extra={"doi": doi} if doi.startswith("10.") else {},
            )
        )
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# 学术意图 / 上下文
# ---------------------------------------------------------------------------


#: 学术上下文检索词里应过滤的通用/噪声英文词
ACADEMIC_CONTEXT_STOP = frozenset(
    {
        "paper", "papers", "method", "methods", "result", "results", "figure",
        "figures", "table", "tables", "reference", "references", "et", "al",
        "using", "based", "data", "model", "models", "system", "systems", "show",
        "shows", "also", "such", "each", "more", "most", "than", "then", "when",
        "with", "without", "from", "into", "over", "under", "this", "that",
        "have", "has", "been", "being", "will", "would", "can", "could",
        "should", "may", "might", "must", "not", "any", "both", "new", "novel",
        "good", "better", "best", "high", "low", "large", "small", "first",
        "second", "third", "one", "two", "three", "approach", "approaches",
        "proposed", "propose", "experiment", "experiments", "analysis",
        "performance", "framework", "section", "sections", "introduction",
        "conclusion", "conclusions", "keywords", "index", "terms", "page",
        "pages", "volume", "issue", "conference", "workshop", "journal",
        "proceedings", "international", "national", "challenge", "pdf",
        "science", "temporal", "spatial", "attention", "feature", "features",
        "learning", "train", "training", "query", "queries", "observation",
        "update", "latent", "representation", "probabilistic", "probability",
        "perform", "structured", "inference", "layer", "softmax", "message",
        "passing", "local", "global", "explicit", "detection", "target",
        "associated", "measurement", "otherwise", "marginal", "potential",
        "kalman", "learn", "learned", "different", "however", "therefore",
        "cross", "aij", "qij", "sij", "exp", "pij", "tij", "zij", "ij",
        "these", "those", "their", "there", "which", "where",
    }
)


def context_search_terms(
    question: str,
    reference_doc: dict[str, object] | None = None,
    limit: int = 10,
) -> list[str]:
    """从问题 + 引用文档（标题/文件名/正文）抽取英文学术检索词。

    只取前若干个高信息量英文词，供 OpenAlex / arXiv 按标题检索；
    不含模型调用，保持确定性。
    """
    parts = [str(question or "")]
    if reference_doc:
        parts.append(str(reference_doc.get("title") or ""))
        parts.append(str(reference_doc.get("filename") or ""))
        parts.append(str(reference_doc.get("content") or "")[:4000])
    terms: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for match in re.finditer(r"[A-Za-z][A-Za-z\-]{2,}", part):
            token = match.group(0).lower()
            # 连字符词取首段（transformer-bp → transformer、transformer-based → transformer）
            if "-" in token:
                token = token.split("-", 1)[0]
            if (
                len(token) < 3
                or token in ACADEMIC_CONTEXT_STOP
                or token in seen
                or is_stopword(token)
            ):
                continue
            seen.add(token)
            terms.append(token)
            if len(terms) >= limit:
                return terms
    return terms


def detect_academic_intent(message: str, reference_doc: dict[str, object] | None = None) -> bool:
    text = str(message or "")
    if ACADEMIC_INTENT_PATTERN.search(text):
        return True
    ids = doc_ref.extract_ids(text)
    if ids["arxiv_ids"] or ids["dois"]:
        return True
    if reference_doc:
        identity = f"{reference_doc.get('title', '')} {reference_doc.get('filename', '')}"
        if re.search(r"arxiv|doi|论文|paper|preprint", identity, re.IGNORECASE):
            return True
        if str(reference_doc.get("source_type") or "") == "pdf" and SUMMARY_PATTERN.search(text):
            return True
    return False


def _title_terms(title: str, limit: int = 8) -> list[str]:
    """从标题提取英文/数字关键词（忽略中文虚词）。"""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+\-]*|\d{4}\.\d{4,5}|\d{2,}", str(title or ""))
    stop = {"the", "a", "an", "of", "for", "and", "in", "on", "to", "with", "by", "is"}
    result: list[str] = []
    for token in tokens:
        if len(token) < 2 or token.lower() in stop:
            continue
        if token not in result:
            result.append(token)
    return result[:limit]


def build_academic_context(
    question: str, reference_doc: dict[str, object] | None = None
) -> dict[str, object]:
    texts = [str(question or "")]
    if reference_doc:
        texts.append(
            f"{reference_doc.get('title', '')} {reference_doc.get('filename', '')}"
        )
    arxiv_ids: list[str] = []
    dois: list[str] = []
    for text in texts:
        ids = doc_ref.extract_ids(text)
        arxiv_ids.extend(id_ for id_ in ids["arxiv_ids"] if id_ not in arxiv_ids)
        dois.extend(doi for doi in ids["dois"] if doi not in dois)
    reference_title = str(reference_doc.get("title") or "") if reference_doc else ""
    title_terms = _title_terms(reference_title) or _title_terms(question)
    return {
        "intent": detect_academic_intent(question, reference_doc),
        "arxiv_ids": arxiv_ids,
        "dois": dois,
        "reference_title": reference_title,
        "title_terms": title_terms,
        # 多轮追问 / 引用文档场景：从上下文抽取英文学术检索词（OpenAlex 兜底用）
        "search_terms": context_search_terms(question, reference_doc),
    }


# ---------------------------------------------------------------------------
# 来源权威度 & 候选打分 / 过滤
# ---------------------------------------------------------------------------


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def authority_category(url: str) -> str:
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    if _host_matches(host, ACADEMIC_DOMAINS):
        return "academic"
    if host.endswith(OFFICIAL_SUFFIXES) or _host_matches(host, OFFICIAL_DOMAINS):
        return "official"
    if _host_matches(host, WIKI_DOMAINS) or host.startswith("wiki"):
        return "wiki"
    if _host_matches(host, COMMUNITY_DOMAINS):
        return "community"
    if _host_matches(host, MEDIA_DOMAINS):
        return "media"
    return "unknown"


def authority_score(category: str) -> float:
    return AUTHORITY_SCORES.get(category, AUTHORITY_SCORES["unknown"])


def authority_label(category: str) -> str:
    return AUTHORITY_LABELS.get(category, AUTHORITY_LABELS["unknown"])


def _context_tokens(question: str, reference_doc: dict[str, object] | None) -> list[str]:
    context = str(question or "")
    if reference_doc:
        context += " " + str(reference_doc.get("title") or "")
        context += " " + str(reference_doc.get("content") or "")[:600]
    return embedding.tokenize_terms(context)


def score_candidate(
    question: str,
    reference_doc: dict[str, object] | None,
    candidate: dict[str, object],
) -> tuple[float, str]:
    """返回 ``(relevance 0–1, authority 类别)``。"""
    context_tokens = set(_context_tokens(question, reference_doc))
    candidate_text = f"{candidate.get('title', '')} {candidate.get('snippet', '')}"
    candidate_tokens = set(embedding.tokenize_terms(candidate_text))
    df, corpus_size = corpus.corpus_stats()

    relevance = 0.0
    if context_tokens and candidate_tokens:
        total_idf = sum(corpus.idf(token, df, corpus_size) for token in context_tokens) or 1.0
        matched_idf = sum(
            corpus.idf(token, df, corpus_size)
            for token in context_tokens
            if token in candidate_tokens
        )
        coverage = matched_idf / total_idf
        intersection = len(context_tokens & candidate_tokens)
        jaccard = intersection / max(1, len(context_tokens | candidate_tokens))
        relevance = 0.7 * coverage + 0.3 * jaccard

    # arXiv ID / DOI / 英文标题短语精确命中加成
    identity_text = f"{question} " + (
        f"{reference_doc.get('title', '')} {reference_doc.get('filename', '')}"
        if reference_doc
        else ""
    )
    ids = doc_ref.extract_ids(identity_text)
    lowered_candidate = candidate_text.lower()
    candidate_url = str(candidate.get("url") or "").lower()
    id_terms = [item.lower() for item in ids["arxiv_ids"] + ids["dois"]]
    if any(id_ and (id_ in candidate_url or id_ in lowered_candidate) for id_ in id_terms):
        # arXiv ID / DOI 精确命中（如 https://arxiv.org/abs/2005.11401）→ 高相关
        relevance = max(relevance + 0.3, 0.95)
    elif any(token.lower() in lowered_candidate for token in _title_terms(identity_text)):
        relevance += 0.15
    elif str(candidate.get("source") or "") == "arxiv" and _title_terms(identity_text):
        # 标题关键词与 arXiv 结果标题重合
        title_lower = str(candidate.get("title") or "").lower()
        if any(token.lower() in title_lower for token in _title_terms(identity_text)):
            relevance = max(relevance, 0.8)
    relevance = max(0.0, min(1.0, relevance))
    return round(relevance, 2), authority_category(str(candidate.get("url") or ""))


def rank_and_filter_candidates(
    question: str,
    reference_doc: dict[str, object] | None,
    candidates: list[dict[str, object]],
    academic_intent: bool,
) -> tuple[list[dict[str, object]], int]:
    """打分、过滤、排序外部候选；返回 ``(候选列表, 被过滤数量)``。"""
    scored: list[dict[str, object]] = []
    for candidate in candidates:
        relevance, authority = score_candidate(question, reference_doc, candidate)
        item = dict(candidate)
        item["relevance"] = round(relevance, 2)
        # 契约：authority 为类别字符串（academic/official/…），另附中文 label 与数值分
        item["authority"] = authority
        item["authority_label"] = authority_label(authority)
        item["authority_category"] = authority
        item["authority_score"] = round(authority_score(authority), 2)
        scored.append(item)

    if not scored:
        return [], 0

    if academic_intent:
        kept = [
            item
            for item in scored
            if item["authority_category"] == "academic"
            or (
                float(item["relevance"]) >= 0.5
                and float(item["authority_score"]) >= 0.65
            )
        ]
        kept = [
            item for item in kept if item["authority_category"] != "community"
        ]
        kept.sort(
            key=lambda item: (
                -0.7 * float(item["relevance"]) - 0.3 * float(item["authority_score"]),
                str(item.get("url")),
            )
        )
        filtered = len(scored) - len(kept)
        return kept[:12], filtered

    top_relevance = max(float(item["relevance"]) for item in scored)
    threshold = max(0.12, top_relevance * 0.45)
    context_tokens = set(_context_tokens(question, reference_doc))
    distinctive = {
        token
        for token in context_tokens
        if len(token) >= 2 and not re.fullmatch(r"\d+", token)
    }
    kept = []
    for item in scored:
        relevance = float(item["relevance"])
        if relevance < threshold:
            continue
        if item["authority_category"] == "community" and relevance < 0.25:
            continue
        # 与问题没有任何「非数字实体词」重合的候选视为无关（避免年份/通用词刷分）
        if distinctive and relevance < 0.5:
            candidate_tokens = set(
                embedding.tokenize_terms(
                    f"{item.get('title', '')} {item.get('snippet', '')}"
                )
            )
            if not (distinctive & candidate_tokens):
                continue
        kept.append(item)
    kept.sort(
        key=lambda item: (
            -0.7 * float(item["relevance"]) - 0.3 * float(item["authority_score"]),
            str(item.get("url")),
        )
    )
    filtered = len(scored) - len(kept)
    return kept[:12], filtered


# ---------------------------------------------------------------------------
# 查询规划
# ---------------------------------------------------------------------------


def validate_query(query: object) -> bool:
    """校验检索词：2–120 字，且不能只是指令词 / 泛词。"""
    text = str(query or "").strip()
    if not (2 <= len(text) <= 120):
        return False
    cleaned = QUERY_FORBIDDEN_RE.sub(" ", text)
    cleaned = re.sub(r"[\s\"'“”]+", " ", cleaned).strip()
    if len(cleaned) < 2:
        return False
    if re.fullmatch(r"[\d\W_]+", cleaned) and not doc_ref.extract_ids(cleaned)["arxiv_ids"]:
        return False
    return True


def clean_question_for_query(question: str) -> str:
    """清洗问题中的指令词，保留实体/关键词（保留原始大小写）。"""
    text = str(question or "").strip()
    text = re.sub(r"[？?！!。；;，,、]+", " ", text)
    text = QUERY_FORBIDDEN_RE.sub(" ", text)
    text = re.sub(r"^\s*(的|了|是)\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def deterministic_queries(
    question: str, context: dict[str, object] | None = None
) -> list[str]:
    """确定性检索词规划（不依赖 LLM）。"""
    context = context or {}
    reference_doc = context.get("reference_doc") or {}
    ids = doc_ref.extract_ids(
        f"{question} {reference_doc.get('title', '')} {reference_doc.get('filename', '')}"
    )
    arxiv_ids = list(ids["arxiv_ids"])
    dois = list(ids["dois"])
    if reference_doc.get("arxiv_id") and str(reference_doc["arxiv_id"]) not in arxiv_ids:
        arxiv_ids.insert(0, str(reference_doc["arxiv_id"]))
    if reference_doc.get("doi") and str(reference_doc["doi"]) not in dois:
        dois.insert(0, str(reference_doc["doi"]))
    title = str(reference_doc.get("title") or "").strip()
    title_terms = _title_terms(title) or _title_terms(str(question or ""))

    queries: list[str] = []
    if arxiv_ids:
        arxiv_id = arxiv_ids[0]
        queries.append(arxiv_id)
        if title_terms:
            queries.append(f'"{title_terms[0]}" {" ".join(title_terms[1:4])}'.strip())
            queries.append(" ".join(title_terms[:6]))
        elif title:
            queries.append(title)
    elif dois:
        queries.append(dois[0])
    elif title:
        keywords = " ".join(title_terms[:6]) or title
        queries.extend(
            [f'"{keywords}"', f"arXiv {keywords}", f"{keywords} pdf"]
        )
    else:
        cleaned = clean_question_for_query(question)
        if cleaned:
            queries.append(cleaned)
            # 英文/数字实体优先的第二个 query
            english = _title_terms(cleaned)
            if english:
                queries.append(" ".join(english[:5]))
            else:
                terms = embedding.tokenize_terms(cleaned)
                if len(terms) >= 3:
                    queries.append(" ".join(terms[:5]))

    result: list[str] = []
    seen: set[str] = set()
    for query in queries:
        candidate = truncate(query.strip(), 120, suffix="").strip()
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        if validate_query(candidate):
            result.append(candidate)
        if len(result) >= 3:
            break
    return result


PLAN_QUERIES_PROMPT = (
    "你是学术/网页检索词规划助手。根据用户问题（以及用户引用的知识库文档标题/arXiv ID），"
    "生成 2–3 条精确检索词：优先使用 arXiv ID、英文标题短语、专有名词；"
    "禁止包含「总结/请/帮我/核心内容/资料/一下」等指令词。只输出 JSON 字符串数组。"
)


def _heuristic_queries(question: str, context: dict[str, object] | None = None) -> list[str]:
    return deterministic_queries(question, context)


def _llm_plan_queries(
    question: str, llm_config: dict[str, object], context: dict[str, object] | None
) -> list[str]:
    reference_doc = (context or {}).get("reference_doc") or {}
    context_lines = [f"用户问题：{question}"]
    if reference_doc:
        context_lines.append(
            f"用户引用的知识库文档标题：{reference_doc.get('title', '')}"
        )
        if reference_doc.get("filename"):
            context_lines.append(f"文件名：{reference_doc.get('filename')}")
        if reference_doc.get("arxiv_id"):
            context_lines.append(f"arXiv ID：{reference_doc.get('arxiv_id')}")
    messages = [
        {"role": "system", "content": PLAN_QUERIES_PROMPT},
        {"role": "user", "content": "\n".join(context_lines)},
    ]
    payload = llm.chat_completion(
        llm_config, messages, max_tokens=200, temperature=0.2, timeout=30.0
    )
    content = llm.extract_message_text(payload)
    match = re.search(r"\[[\s\S]*?\]", content or "")
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    queries: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        candidate = truncate(item.strip(), 120, suffix="").strip()
        if validate_query(candidate):
            queries.append(candidate)
    return list(dict.fromkeys(queries))[:3]


def plan_queries(
    question: str,
    llm_config: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
) -> list[str]:
    """生成 1–3 条检索词；确定性规划优先，LLM 结果需通过校验，否则回退。"""
    if not str(question or "").strip() and not (context or {}).get("reference_doc"):
        return []
    deterministic = deterministic_queries(question, context)
    if llm_config is not None and llm.is_configured(llm_config):
        try:
            queries = _llm_plan_queries(question, llm_config, context)
            if queries:
                return queries[:3]
        except llm.LLMError as exc:
            logger.info("LLM 生成检索词失败，回退确定性规划：%s", exc)
        except Exception:  # pragma: no cover - 兜底
            logger.exception("LLM 生成检索词出现异常，回退确定性规划")
    if deterministic:
        return deterministic[:3]
    return _heuristic_queries(question, context)
