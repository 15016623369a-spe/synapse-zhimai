# Security Policy

## Supported Versions

Only the latest `2.x` release line is supported with security fixes.

## Reporting a Vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
for this repository. Include reproduction steps, affected version, and impact.

## Threat Model / Deployment Warning

Synapse is designed as a **single-user, self-hosted personal tool**.

- **There is no authentication or multi-user isolation.** Anyone who can reach
  the HTTP port can read/write all knowledge, conversations, plans, and settings.
- The default startup scripts bind to **127.0.0.1** and the Docker Compose file
  publishes only to `127.0.0.1`. **Do not expose it directly to the public
  internet.** If you need remote access, use a VPN or put it behind a reverse
  proxy with authentication, TLS, and network ACLs.
- **API keys are stored in the local SQLite database** (`data/synapse.db`) and
  are never returned by the API. Protect the data directory and backups with
  appropriate filesystem permissions. Do not commit `data/`, `.env`, or
  backups to git.
- **Custom MCP servers execute user-provided commands.** Only add MCP servers
  from trusted sources. Built-in servers are read-only; custom tools are
  disabled until you explicitly select `allowed_tools`. Do not expose
  user-configurable MCP server setup to untrusted users.
- Web/URL fetching has SSRF protections against loopback/private addresses.
  Both `POST /api/documents/url` and the knowledge-extension fetcher validate the
  initial URL and **every redirect hop** before requesting it. The check is
  best-effort (DNS rebinding is out of scope); set `SYNAPSE_ALLOW_PRIVATE_FETCH=1`
  only for local tests. Do not run it on a network with sensitive internal
  services unless you understand the risk.
- Tool/web content is untrusted data and may contain prompt-injection attempts.
  The app treats external content as context, not instructions, but no agent
  system is immune. Keep human approval for knowledge expansion.
- OCR / embeddings / model downloads execute locally and may consume
  significant CPU, RAM, disk, and network.

## Hardening Checklist for Shared / Remote Deployments

- Add authentication + authorization before exposing beyond localhost.
- Put behind HTTPS reverse proxy; restrict source IPs.
- Encrypt the data volume at rest; restrict filesystem permissions.
- Disable custom MCP servers and untrusted URL fetching where not needed.
- Add rate limiting, request size limits, and audit logging.
- Automate encrypted backups and test restore procedures.

These capabilities are **not** built into the current release; see
[`RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md) for the open-source release scope.
