# MultiHop-RAG 评测结果

- 数据：MultiHop-RAG（ODC-BY），2556 题 / 609 篇新闻，top_k=10
- 模式：本地 hash 向量 + BM25 + 文档级两阶段；无 LLM（抽取式/拒答判定）
- 复现：`backend/.venv/bin/python eval/prepare.py && backend/.venv/bin/python eval/evaluate.py`

| 指标 | 全部 | inference_query | comparison_query | null_query | temporal_query |
|---|---:|---:|---:|---:|---:|
| questions | 2556 | 816 | 856 | 301 | 583 |
| doc_recall@k | 0.544 | 0.378 | 0.645 | — | 0.628 |
| complete@5 | 0.176 | 0.029 | 0.285 | — | 0.221 |
| complete@10 | 0.275 | 0.071 | 0.415 | — | 0.353 |
| mrr@k | 0.584 | 0.483 | 0.634 | — | 0.652 |
| hit@k | 0.827 | 0.745 | 0.877 | — | 0.868 |
| fact_recall@k | 0.210 | 0.178 | 0.278 | — | 0.156 |
| gap_false_positive | 0.097 | 0.173 | 0.041 | — | 0.074 |
| weak_false_positive | 0.227 | 0.281 | 0.207 | — | 0.180 |
| null_abstain | 0.315 | 0.000 | — | 0.316 | — |
| null_flagged | 0.861 | 1.000 | — | 0.861 | — |

说明：`gap_false_positive` 只在可答题上统计（越低越好），`null_abstain` 只在 null_query 上统计（越高越好）。
这是检索/拒答评测，不代表答案事实性；引用精确率由 `scripts/check_api.py` 与单测覆盖。
