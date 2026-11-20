# Evaluation Protocol

> 状态（2026-11-20）：**已实现**公开基准评测与闭环收敛率评测。
> 原来的 10 题 demo 集降级为「中文冒烟/回归」，不再作为效果证据。

---

## 1. 现在能跑的两套评测

### A. MultiHop-RAG（主评测，公开基准）

```bash
backend/.venv/bin/python eval/prepare.py      # 下载 609 篇 + 导入隔离库
backend/.venv/bin/python eval/evaluate.py --all
```

- 数据：[MultiHop-RAG](https://huggingface.co/datasets/yixuantt/MultiHopRAG)（ODC-BY），609 篇英文新闻、2556 题；
- 题型：comparison 856 / inference 816 / temporal 583 / null 301；
- 每题的 gold 文章标题与证据句都有，标题与语料 100% 对齐；
- 全程离线、确定性；不调用 LLM。

指标定义见 [`eval/README.md`](../eval/README.md)。

### B. 10 题 demo 集（中文冒烟）

```bash
backend/.venv/bin/python scripts/eval_retrieval.py http://127.0.0.1:7800
```

只用于中文小语料回归，**不要对外引用为准确率**：16 篇文档取 top-8，随机命中率约 70%。

---

## 2. 结果（2026-11-20）

全量 2556 题，hash 向量 + BM25 + 文档级两阶段，top_k=10：

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

怎么读：

- **随机基线**：609 篇取 10 篇，doc recall 约 `10/609 ≈ 1.6%`；0.472 约为随机的 29 倍。
- **comparison / temporal 明显好于 inference**：2 篇证据的题 hit@10 84%，4 篇证据的推断题只有 67%；`complete@5` 只有 12.3%，说明「把多篇证据一次凑齐」仍是短板。
- **fact_recall@10 只有 0.162**：命中文档不等于命中证据句，chunk 级精度是下一步重点。
- **gap 误报 36.5%、null 拒答 70.5%**：这是同一枚硬币的两面，见第 3 节。

原始报告：[`eval/reports/multihop_rag_v1.json`](../eval/reports/multihop_rag_v1.json)。
复现耗时：937s（单进程、无 GPU）。

---

## 3. 闭环收敛率（LCR）

```bash
backend/.venv/bin/python eval/loop_closure.py --questions 20
```

定义：

```
LCR = 采纳外部知识后由「答不上」变为「答得上」的题数
      ────────────────────────────────────────
      触发 gap 且完成采纳的题数
```

流程：16 篇中文 demo 语料 → 选 20 道 MultiHop-RAG 题（证据互不重叠）→ 先问确认 gap →
通过真实 `POST /api/extend/commit` 把证据文章入库（抓取用 MockTransport 模拟）→ 再问。

结果：**15/20 = 75%**；复问时引用到新文档 **17/20 = 85%**。

5 道失败题里：

- 2 道复问仍 gap、也没引用新文档 → 新知识没被检索到；
- 3 道**已经引用了新文档，但 gap 仍然触发** → gap 判据对长英文问题偏严。

这正是下一步「语言自适应阈值」的直接依据。

原始报告：[`eval/reports/loop_closure.json`](../eval/reports/loop_closure.json)。

---

## 4. 为什么 gap 指标是这个水平

在 130 道抽样题上统计「登录词 IDF 覆盖率」：

| 分位 | 可答题（n=90） | null 题（n=40） |
|---|---:|---:|
| p25 | 0.24 | 0.16 |
| p50 | 0.31 | 0.19 |
| p75 | 0.38 | 0.24 |

两组分布重叠明显：阈值 0.25 时，约 1/4 可答题被误判为缺口；降到 0.15 时 null 拒答会掉到 50% 左右。
**覆盖率单指标无法同时做好这两件事**——需要语言自适应阈值 + 证据句级信号（下一步）。

---

## 5. 已知限制

- **语言错配**：语料是英文新闻，产品主打中文笔记；tokenizer / 分块 / 阈值都按中文调过。这里的数字是压力测试，不等于中文场景表现。
- **只测检索与拒答**：不测答案忠实度、引用精确率（后者由 `check_api.py` 与单测覆盖）。
- **hash 向量不是语义向量**：默认模式是词法哈希；`fastembed` 需要 4–5 分钟重建 1.4 万 chunk，评测里没有作为默认。
- **null 题是构造出来的**：它们引用真实文章但问一个不存在的关联；现实中的「资料不足」更容易被识别。
- **没有调参**：没有针对 MultiHop-RAG 的题型改权重；`per_doc=1` 的文档多样性策略是通用的多跳设计。

---

## 6. 下一步

1. 语言自适应 gap 阈值（中文 0.25 / 英文 0.15），并加「证据句命中」作为第二信号。
2. 多证据题的 `complete@5`：按子问题拆检索（英文 `and` / 分号切分），而不是整句一个大 query。
3. fact-level 指标进 CI：`fact_recall@10` 低于阈值直接失败。
4. 引用精确率评测：生成模式下对 `[n]` 与证据句做 NLI/蕴含校验。
