# Synapse · 知脉 v1.0.1

This patch release fixes the published container image for Apple Silicon and other ARM64 machines.

## Fix

- **Multi-architecture GHCR image**: `linux/amd64` + `linux/arm64`.
- Previously the image was built only for `linux/amd64`; Docker on Apple Silicon returned:
  `no matching manifest for linux/arm64/v8`.

## Pull

```bash
docker pull ghcr.io/15016623369a-spe/synapse-zhimai:latest
# or pin:
docker pull ghcr.io/15016623369a-spe/synapse-zhimai:v1.0.1
```

The `latest` tag is now multi-architecture.

## Notes

- The application code is unchanged from v1.0.0; this release only fixes image publishing.
- `v1.0.0` image remains linux/amd64-only. Use `v1.0.1` or `latest` on ARM64.

See [README.md](../README.md) and [SECURITY.md](../SECURITY.md).
