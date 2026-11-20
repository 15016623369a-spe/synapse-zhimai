# MultiHop-RAG 评测（公开基准）

> 用一个公开、可复现的多跳检索基准，替代「10 题演示集 90%」这种没有区分度的自测。

## 数据

- 来源：[MultiHop-RAG](https://huggingface.co/datasets/yixuantt/MultiHopRAG)（ODC-BY）
- 语料：609 篇英文新闻正文（`corpus.json`，6.5MB）
- 题目：2556 道（`MultiHopRAG.json`，5.1MB）
  - `comparison_query` 856：比较两篇/三篇报道
  - `inference_query` 816：跨 2–4 篇推断
  - `temporal_query` 583：按时间线比较
  - `null_query` 301：语料里**没有答案**，用于测拒答
- 每道题的 `evidence_list` 给出 gold 文章标题与证据句；标题与语料 100% 对得上。

文件 SHA256 固定在 `eval/prepare.py`，下载后校验；不把 11MB 数据提交进仓库。

## 为什么用它

原来的 `data/demo_questions.md` 只有 10 题、16 篇文档，top-8 覆盖一半语料，随机检索的期望命中率约 70%——没有区分度，也无法对外引用。

MultiHop-RAG 有 609 篇语料、2556 题、四类题型、gold evidence，且是公开数据集；指标可以被任何人复现。

## 怎么跑

```bash
# 1) 下载 + 导入到隔离数据目录 eval/state（不碰 data/synapse.db）
backend/.venv/bin/python eval/prepare.py

# 2) 跑评测（默认分层抽样 600 题；--all 跑 2556 题）
backend/.venv/bin/python eval/evaluate.py --sample 600

# 冒烟
backend/.venv/bin/python eval/evaluate.py --limit 20
```

产物：`eval/reports/multihop_rag_v1.json` 与 `.md`。

## 指标

| 指标 | 定义 |
|---|---|
| `doc_recall@k` | top-k chunk 覆盖到的期望文档比例（多证据题取平均） |
| `complete@5` | 所有期望文档都进 top-5 的题占比 |
| `mrr@k` | 第一个期望文档的倒数排名 |
| `hit@k` | top-k 至少命中一个期望文档 |
| `fact_recall@k` | 期望证据句能在 top-k chunk 原文里找到的比例 |
| `gap_false_positive` | 可答题里被判定为「知识缺口」的比例（越低越好） |
| `null_abstain` | null_query 里被判定为缺口/拒答的比例（越高越好） |

评测模式：本地 hash 向量 + BM25 + 文档级两阶段；不调用 LLM。全流程离线、确定性。

## 结果

见 [reports/multihop_rag_v1.md](reports/multihop_rag_v1.md)。摘要：

<!-- RESULTS:START -->
全量 2556 题（2026-11-20，hash 向量 + BM25 + 文档级两阶段，无 LLM）：

| 指标 | 全部（可答题） | comparison | inference | temporal |
|---|---:|---:|---:|---:|
| 题数 | 2254 | 856 | 815 | 583 |
| `doc_recall@10` | **0.472** | 0.557 | 0.322 | 0.556 |
| `hit@10` | **0.778** | 0.844 | 0.670 | 0.832 |
| `MRR@10` | **0.549** | 0.615 | 0.435 | 0.612 |
| `complete@5` | 0.123 | 0.183 | 0.017 | 0.184 |
| `fact_recall@10` | 0.162 | 0.214 | 0.131 | 0.130 |
| `gap_false_positive` | 0.365 | 0.254 | 0.540 | 0.283 |
| `null_abstain` | **0.705**（n=301） | – | – | – |

参考：609 篇语料、top-10 的**随机** doc recall 约 `10/609 ≈ 1.6%`；0.472 约为随机基线的 29 倍。
耗时 937s（单进程、无 GPU）。

闭环收敛率（LCR，20 题）：**15/20 = 75%**，复问时引用到新文档 17/20 = 85%。见 [reports/loop_closure.md](reports/loop_closure.md)。
<!-- RESULTS:END -->

## 已知限制

- **语言错配**：语料是英文新闻，产品主打中文个人笔记；tokenizer / 分块 / 阈值都是为中文设计的。这里的数字是**压力测试**，不等于中文场景表现。
- **只测检索与拒答**：不测答案忠实度、引用精确率（后者由 `check_api.py` 与单测覆盖）。
- **hash 向量不是语义向量**：默认模式是词法哈希，英文近义改写召回弱；`fastembed` 模式可单独对比。
- **gap 阈值是中文调出来的**：英文题上误报率偏高，见结果表；语言自适应阈值是后续工作。
- 数据许可 ODC-BY，引用请保留来源链接。
