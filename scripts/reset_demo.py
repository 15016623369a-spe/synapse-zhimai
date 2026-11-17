#!/usr/bin/env python3
"""Reset the local Synapse database to the bundled demo corpus.

This is intentionally conservative:
- requires --yes (otherwise prints a warning and exits);
- backs up the current DB next to it before deleting;
- only removes the app DB files, never documents/uploads/backups.

The server must be restarted after this command so the app re-seeds demo docs.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm destructive reset")
    parser.add_argument("--no-backup", action="store_true", help="skip backup (not recommended)")
    args = parser.parse_args()
    data_dir = config.data_dir().resolve()
    db = data_dir / "synapse.db"
    print(f"data dir: {data_dir}")
    if not db.exists():
        print("database does not exist; nothing to reset. Restart the app to seed demo data.")
        return 0
    if not args.yes:
        print("Refusing to reset without --yes.")
        print("Run: python scripts/reset_demo.py --yes")
        return 2
    if not args.no_backup:
        backups = data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = backups / f"synapse-pre-reset-{stamp}.db"
        shutil.copy2(db, target)
        print(f"backup: {target}")
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if path.exists():
            path.unlink()
            print(f"removed: {path}")
    print("done. Restart the app so demo documents are re-indexed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
