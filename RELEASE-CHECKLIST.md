# Open-source Release Checklist

Target: a self-hosted, single-user release that others can run safely on their
own machine. This is **not** a multi-user SaaS product.

## Already present

- [x] Functional app: RAG chat, citations, 3D graph, zones, plans/check-ins,
  web knowledge extension with approval, PDF/OCR + AI enrichment,
  fastembed embeddings, feedback reranker, MCP tools + AI add-tool wizard,
  scheduled knowledge patrol.
- [x] Backend tests (342 passed / 1 skipped at v2.3) and API/3D/Playwright checks.
- [x] Dockerfile + docker-compose (localhost-only by default).
- [x] MIT LICENSE, SECURITY.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md.
- [x] CI workflow: backend pytest, frontend typecheck/build, Docker build.
- [x] Local one-command start and development scripts.
- [x] Demo documents and demo questions.
- [x] SQLite migrations/backups and `data/` gitignore rules.

## Before publishing (P0)

- [x] **Remove personal data from the repository**: personal logs/screenshots
  moved to `private/` (gitignored); README now uses sanitized `docs/screenshots/`
  generated from demo data; `SPEC*.md` internal notes moved to `private/specs/`.
- [ ] Confirm `git status` contains no `data/`, `.env`, `data/backups/`,
  `.cache/`, `private/`, personal screenshots, or API keys.
- [ ] Decide/replace LICENSE copyright holder (currently "Synapse contributors").
- [ ] Fresh-machine test:
  - [ ] `docker compose up --build` on a clean checkout works and shows demo data.
  - [ ] `./start.sh` on macOS/Linux with Python 3.12 + Node 20 works.
- [ ] Verify Docker data volume persistence + documented backup/restore.
- [ ] Add repository URL to README/package metadata and CI badges.
- [ ] Run `python scripts/reset_demo.py --yes` on a copy and document it.

## Repository

- [synapse-zhimai](https://github.com/15016623369a-spe/synapse-zhimai)

## Contact

- X: [@JaceHuang123](https://x.com/JaceHuang123)
- Email: 15016623369a@gmail.com
- GitHub repository: [synapse-zhimai](https://github.com/15016623369a-spe/synapse-zhimai)

## Before publishing (P1)

- [ ] Pin dependencies or add lockfiles (`requirements` ranges + frontend lock
  exist; consider `uv.lock`).
- [ ] Add a screenshot of the login-free, local-only warning.
- [ ] Add issue/PR templates.
- [ ] Add changelog/release notes for v1.0.0.
- [ ] Consider GitHub secret scanning / Dependabot.
- [ ] Review third-party licenses and search/LLM provider terms.

## Explicit non-goals of this release

- No authentication, multi-tenancy, or team permissions.
- No public-internet hardening, rate limiting, or compliance certification.
- No arbitrary MCP server sandboxing; custom MCP commands are user-controlled.
- No OCR for existing PDFs (re-upload needed) and no OCR of image-only files
  beyond PDF pages.
- No guaranteed accuracy: RAG retrieval/answer quality must be evaluated against
  your own labelled questions.

## Publishing steps (after checklist)

```bash
git init
git add .
git status                 # inspect! no data/secrets/screenshots with personal data
git commit -m "chore: initial open-source release"
git remote add origin <your-repo-url>
git push -u origin main
```

Create a GitHub release/tag `v1.0.0` after CI is green and a clean Docker run
has been verified.
