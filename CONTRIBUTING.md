# Contributing to Synapse · 知脉

Thanks for helping! This is a self-hosted personal knowledge assistant. Please
keep changes focused, tested, and respectful of the local-first design.

## Development Setup

Requirements: Python 3.12, Node.js 20+, npm. Optional: uv (faster setup).

```bash
cd synapse
./start.sh            # build frontend + run at http://127.0.0.1:7800
./dev.sh              # backend :7800 --reload + Vite :5173
```

## Tests

```bash
cd backend
.venv/bin/python -m pytest -q          # backend
cd ../frontend
npm run typecheck
npm run build
```

- Do not require a real LLM key or network in unit tests; mock external calls.
- Do not commit personal data, `data/synapse.db`, `.env`, API keys, or backups.
- For retrieval/eval changes, include before/after metrics from
  `data/demo_questions.md` and note that its labels may be stale.

## Docker

```bash
docker compose up --build
```

## Pull Requests

- Keep PRs small and describe the user-visible effect.
- Add/update tests for behavior changes.
- Update README / REPORT when adding a feature or limitation.
- Do not add a new runtime dependency without discussing it first.
- Follow the existing style: Python type hints, FastAPI services, React +
  TypeScript strict, hand-written CSS.
- By contributing, you agree your contributions are licensed under the MIT
  License.

## Security

Never report vulnerabilities in public issues; see [SECURITY.md](SECURITY.md).
