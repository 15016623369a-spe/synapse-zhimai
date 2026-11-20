# 闭环 demo（自包含，不联网、不花 LLM 额度）

演示「答不上 → 你审核后入库 → 再问答上」三幕：

1. 16 篇 demo 文档作为基础语料；
2. 问 `SEA 2026 年市场进入策略是什么？` —— 语料里没有 SEA，触发缺口；
3. 「搜索补充资料」→ 候选只作为待确认资料展示；
4. 勾选并「加入知识库」→ 本地 fixture 页面被抓取、入库；
5. 再问同一问题 —— 命中新文档，回答带引用，可回原文。

## 运行

```bash
# 1) 起 demo（含 fixture 静态服务 :7900 + Synapse :7909）
backend/.venv/bin/python demo/run_demo.py --reset

# 2) 浏览器打开
open http://127.0.0.1:7909
```

`run_demo.py` 用 `PYTHONPATH` 指向本仓库后端、`SYNAPSE_DATA_DIR=demo/state`，
不会读写你的 `data/synapse.db`。

## 重新录制视频

需要 Playwright chromium（本机在仓库上一级的 `.playwright`）：

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/../.playwright" \
  ../.venv-tools/bin/python demo/record_demo.py
```

产物：

- `docs/media/demo-loop.webm`（约 31 秒，VP9）
- `docs/media/demo-loop-poster.png`（README 首图）

录制脚本会在页面上叠三幕标题；如果复问仍出现新的缺口卡，脚本直接失败。
