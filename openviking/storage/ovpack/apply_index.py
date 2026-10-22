"""Install a vector index shipped inside an ovpack (offline migration helper).

A pack produced with ``include_index`` carries the source engine's persisted
vector index (``_ovpack/publication/vectordb_index/...``). The payload rows are
restored by ``ov import``; this tool copies the shipped index into the target
data directory so the engine loads a search index that already covers the
restored rows (including directory L0/L1 summaries, which the restore write
path does not add to the persisted index).

Run it with the server stopped, then start the server:

    python -m openviking.storage.ovpack.apply_index <pack.ovpack> --data-dir <data>

The existing index directory is moved aside (never deleted) as a rollback.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zipfile
from typing import Optional

_INDEX_DIR = os.path.join("vectordb", "context", "index", "default")
_LEAF_PREFIX = "_ovpack/publication/vectordb_index/"


def _find_base(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if not names:
        raise SystemExit("empty ovpack")
    return names[0].split("/", 1)[0]


def apply_index(pack_path: str, data_dir: str, force: bool = False) -> dict:
    if not os.path.exists(pack_path):
        raise SystemExit(f"pack not found: {pack_path}")
    target_dir = os.path.join(data_dir, _INDEX_DIR)
    if os.path.isdir(target_dir) and not force:
        raise SystemExit(
            f"index directory already exists: {target_dir}\n"
            "Pass --force to move it aside and install the shipped index."
        )
    with zipfile.ZipFile(pack_path) as zf:
        base = _find_base(zf)
        prefix = f"{base}/{_LEAF_PREFIX}"
        members = [n for n in zf.namelist() if n.startswith(prefix) and not n.endswith("/")]
        if not members:
            raise SystemExit("pack does not contain a shipped vector index")
        backup_dir = target_dir + f".bak-{int(time.time())}"
        if os.path.isdir(target_dir):
            os.replace(target_dir, backup_dir)
        os.makedirs(target_dir, exist_ok=True)
        count = 0
        for name in members:
            rel = name[len(prefix):]
            dest = os.path.join(target_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(zf.read(name))
            count += 1
    result = {
        "installed_files": count,
        "index_dir": target_dir,
        "backup_dir": backup_dir if os.path.isdir(backup_dir) else None,
    }
    print(json.dumps(result, indent=2))
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", help="path to the .ovpack file")
    parser.add_argument("--data-dir", required=True, help="OpenViking data directory")
    parser.add_argument("--force", action="store_true", help="replace an existing index")
    args = parser.parse_args(argv)
    apply_index(args.pack, args.data_dir, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
