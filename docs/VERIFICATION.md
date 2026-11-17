# Verification Summary

This is a sanitized summary; detailed logs and personal screenshots are kept
locally under `private/` and are not part of the open-source repository.

| Check | Result |
|---|---|
| Backend pytest | 342 passed / 1 skipped |
| API contract | 52 / 52 |
| 3D graph E2E | 16 / 16 |
| RAG complete recall @8 | 90.0% on the 10-question demo set |
| RAG complete recall @6 | 85.6% |
| Single-document Top-1 | 3/3 (no regression) |
| Frontend typecheck / build | 0 errors / success |

Reproduce locally:

```bash
cd backend && .venv/bin/python -m pytest -q
cd ../frontend && npm run typecheck && npm run build
backend/.venv/bin/python scripts/check_api.py http://127.0.0.1:7800
backend/.venv/bin/python scripts/eval_retrieval.py http://127.0.0.1:7800
```

The demo evaluation set has stale labels after the knowledge base grew; use it
as a regression/trend check, not as a universal accuracy number. See README's
limitations section.
