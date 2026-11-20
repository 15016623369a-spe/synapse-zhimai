# Verification Summary

This is a sanitized summary; detailed logs and personal screenshots are kept
locally under `private/` and are not part of the open-source repository.

## v2.5（2026-11-20）

| Check | Result |
|---|---|
| Backend pytest（断网） | **361 passed / 1 skipped** |
| MultiHop-RAG 全量 2556 题（可答题 2254） | doc hit@10 **0.778** / doc recall@10 **0.472** / MRR@10 **0.549** |
| MultiHop-RAG null_query（301 题） | 拒答 **0.705**；可答题 gap 误报 **0.365** |
| 闭环收敛率 LCR（20 题） | **15/20 = 75%**；复问引用新文档 17/20 |
| 前端 typecheck / build | 0 errors / 构建成功 |

复现：

```bash
backend/.venv/bin/python eval/prepare.py
backend/.venv/bin/python eval/evaluate.py --all
backend/.venv/bin/python eval/loop_closure.py --questions 20
```

详细口径、逐题型拆分与失败分析见 [EVAL.md](EVAL.md)。

## v2.4（2026-11-20）

| Check | Result |
|---|---|
| Backend pytest（conftest 阻断外网 socket） | **359 passed / 1 skipped** |
| 缺口回归（10 题 demo + 3 道无答案） | 9 道可答题 **0 误报**；3 道无答案 **全部命中** |
| 引用一致性 | `done.citations` 只含回答里出现的 `[n]`；幻觉编号进 `context_diagnostics.invalid_citations` |
| 文档级引用 | `kind=document`、`score=null`，不再伪造 0.99 |
| URL 抓取 SSRF | 首跳与每次 302 都校验，内网/环回被拒 |
| 前端 typecheck / build | 0 errors / 构建成功 |
| 离线可运行 | 无 LLM、无外网完成「问答 → 引用 → 缺口」 |

复现：

```bash
cd backend && .venv/bin/python -m pytest -q        # 不联网也应全绿
cd ../frontend && npm run typecheck && npm run build
backend/.venv/bin/python scripts/eval_retrieval.py http://127.0.0.1:7800
```

## 历史验收（v1.x–v2.3）

| Check | Result |
|---|---|
| Backend pytest（v2.3） | 342 passed / 1 skipped |
| API contract | 52 / 52 |
| 3D graph E2E | 16 / 16 |
| RAG complete recall @8 | 90.0% on the 10-question demo set |
| RAG complete recall @6 | 85.6% |
| Single-document Top-1 | 3/3 (no regression) |
| Frontend typecheck / build | 0 errors / success |

## 指标口径（重要）

- 「complete recall @k」= **10 题演示集上平均每题的期望文档召回率**（n=10、16 篇文档），
  来自 v2.3 的内部验收记录，不是通用准确率，也没有置信区间。
- 仓库里的 `scripts/eval_retrieval.py` 现在输出 **doc hit-rate / 平均期望文档召回 / MRR**
  三个数；2026-11-20 在 16 篇 demo 库上复跑为 recall **0.922**、MRR **1.000**（n=9），
  与 v2.3 的 90.0% / 0.944 不一致：v2.3 用的是 34 篇 live 库，且 Q9 标签已过期。
  口径一致了，但 before 基线脚本不在仓库里，仍无法一键复现「77% → 90%」。
- 16 篇文档取 top-8 时，随机检索的期望命中率约为 **70%**：
  Q1–Q3 各 50%、Q4 90%、Q5 96%、Q6 77%、Q7 90%、Q8–Q9 99%、Q10 0%。
  因此 90% 只能当回归趋势，不能当效果证明。
- Q9（预算）的旧标注已过期：当前库实际 13 篇含「预算」，旧期望只列 5 篇；
  「5/5」是相对旧标签的结果。
- 下一步的评测协议（60 题、chunk 级指标、闭环收敛率）见 [EVAL.md](EVAL.md)。
