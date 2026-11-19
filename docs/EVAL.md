# Evaluation Protocol

> 状态：v2.4 已修掉「引用/缺口」层面的错误信号；**下面第 3 节的 60 题评测还是计划**，
> 当前仓库里能一键复现的仍然只有 10 题 demo 集。不要把计划当结果。

---

## 1. 现在能跑的评测

```bash
# 启动服务后
backend/.venv/bin/python scripts/eval_retrieval.py http://127.0.0.1:7800
```

输出四个数：

- `document hit-rate@8`：top-8 里是否出现了任一「期望文档」；
- `expected-doc recall (avg)`：平均每题期望文档被召回的比例（n=9，无答案题不计）；
- `MRR@8`：第一个期望文档的倒数排名均值；
- `keyword coverage`：期望关键词在召回文本里的子串覆盖率。

### 这个数为什么不能当效果证明

- 语料只有 16 篇，top-8 已经覆盖一半语料；随机检索的期望命中率约 70%；
- 10 道题里 1 道就是 10 个百分点；
- 只到文档级，不区分「命中了文档但片段完全无关」；
- 无置信区间、无多次运行、无 CI 门禁。

它适合做**回归趋势**：改动前后跑同一条命令，看有没有明显退化。

---

## 2. 当前已知的评测坑

| 坑 | 说明 | 处理 |
|---|---|---|
| 指标名混用 | README 的「完整召回@8」是平均每题期望文档召回率，脚本曾只输出 doc hit-rate | v2.4 起脚本补上 recall/MRR，并在 README / VERIFICATION 里写明口径 |
| Q9 标签过期 | 当前库 13 篇含「预算」，旧期望只列 5 篇 | 需要重标；在重标前不要引用 Q9 的 5/5 |
| 语料太小 | 16 篇文档 | 计划换 200–300 篇 arXiv cs.CL 摘要 |
| 无答案题太少 | 只有 Q10 | 计划补到 10 道 |
| 无 chunk 级指标 | 只看文档命中 | 计划加 Recall@k / MRR / nDCG |

---

## 3. 计划：60 题评测协议（待实现）

### 3.1 语料

`eval/corpus/arxiv_cs_cl.json`：200–300 篇 arXiv cs.CL 摘要（脚本抓一次、缓存提交）。
理由：语料足够大时 top-k 才是真约束；16 篇文档的 top-8 没有区分度。

### 3.2 题目

`eval/questions.jsonl`，60 题：

| 类型 | 数量 | 目的 |
|---|---:|---|
| 单跳事实 | 30 | chunk 级召回精度 |
| 多跳/跨文档 | 20 | 文档级两阶段是否有效 |
| 无答案 | 10 | 拒答准确率 |

```json
{"id":"q001","type":"single","question":"...","gold_docs":["d_x"],"gold_spans":["..."],"answerable":true}
{"id":"q047","type":"multi","question":"...","gold_docs":["d_a","d_c"],"answerable":true}
{"id":"q055","type":"unanswerable","question":"...","gold_docs":[],"answerable":false}
```

### 3.3 指标

| 指标 | 定义 | 目标 |
|---|---|---|
| chunk Recall@5 / @10 | 命中 gold chunk 的比例 | ≥ 0.75 |
| MRR@10 | 第一个命中 chunk 的倒数排名 | ≥ 0.65 |
| nDCG@10 | 排序质量 | 记录趋势 |
| doc Recall@5 | 文档级召回 | 记录趋势 |
| 引用精确率 | 回答 `[n]` 都能映射到真实 chunk | ≥ 0.95 |
| 拒答准确率 | 无答案题正确说「没有」 | ≥ 0.9 |
| **闭环收敛率 LCR** | 采纳外部知识后由「答不上」变「答得上」的比例 | ≥ 0.6 |

### 3.4 闭环收敛率（LCR）

```
LCR = 采纳后 answerable 翻转的题数 / 触发 gap 且完成采纳的题数
```

做法：

1. 准备 20 道知识库确实没有的题；
2. 跑一遍，记录 `gap=true`；
3. 对每题走 `/api/extend/commit` 采纳 1–2 条候选；
4. 复跑同一题，记录是否不再 gap 且引用到新文档；
5. 统计翻转比例。

这是「知识闭环」最直接的度量，也是本项目区别于普通 RAG demo 的地方。

### 3.5 消融

为了回答「两阶段召回到底贡献了多少」，需要可切换的检索模式：

```
SYNAPSE_RETRIEVAL_MODE=legacy   # 只 BM25 + dense + RRF
SYNAPSE_RETRIEVAL_MODE=full     # 默认，含文档级两阶段与意图路由
```

输出一张表（BM25 / +dense / +RRF / +two-stage / +intent），每一项都有 Recall@5 与 MRR。

### 3.6 CI 门禁

- `eval` job 在固定语料 + 固定题目上跑；
- `chunk Recall@5 < 0.75` 或 `LCR < 0.6` 直接失败；
- 报告作为 artifact 上传，PR 里贴对比表。

---

## 4. 评测之外的诚实边界

即使 60 题全绿，也只能说明「在这个语料、这批题上有效」，不能说明：

- 对任意领域的文档都有效；
- 引用没有幻觉（当前只做 `[n]` 映射，不做 NLI）；
- 多用户/高并发可用（单进程 SQLite）。

写简历/README 时把语料规模、题数、指标定义一起写出来，比单独写一个 90% 更有说服力。
