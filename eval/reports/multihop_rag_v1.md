# MultiHop-RAG 评测结果

- 数据：MultiHop-RAG（ODC-BY），2556 题 / 609 篇新闻，top_k=10
- 模式：本地 hash 向量 + BM25 + 文档级两阶段；无 LLM（抽取式/拒答判定）
- 复现：`backend/.venv/bin/python eval/prepare.py && backend/.venv/bin/python eval/evaluate.py --all`

| 指标 | 全部（可答题） | inference_query | comparison_query | null_query | temporal_query |
|---|---:|---:|---:|---:|---:|
| questions | 2556 | 816 | 856 | 301 | 583 |
| doc_recall@k | 0.472 | 0.322 | 0.557 | — | 0.555 |
| complete@5 | 0.123 | 0.017 | 0.183 | — | 0.183 |
| mrr@k | 0.549 | 0.435 | 0.615 | — | 0.612 |
| hit@k | 0.778 | 0.670 | 0.844 | — | 0.832 |
| fact_recall@k | 0.162 | 0.131 | 0.214 | — | 0.130 |
| gap_false_positive | 0.365 | 0.540 | 0.254 | — | 0.283 |
| null_abstain | 0.705 | 1.000 | — | 0.704 | — |

说明：检索指标只在可答题上聚合；`gap_false_positive` 越低越好，`null_abstain` 越高越好；「—」表示该组没有对应样本。
这是检索/拒答评测，不代表答案事实性；引用精确率由 `scripts/check_api.py` 与单测覆盖。
