"""Fetch pinned public HF snapshots, validating mirror bytes against official metadata.

Obtain the manifest from the official Hugging Face /api/models?blobs=true endpoint.
Only JSON/tokenizer/Python/safetensors files are retained; pickle weights are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


def verify(path: Path, item: dict) -> bool:
    if not path.is_file() or path.stat().st_size != item.get("size"):
        return False
    if item.get("lfs"):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == item["lfs"]["sha256"]
    digest = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest() == item["blobId"]


def verify_modelscope(path: Path, item: dict) -> bool:
    if not path.is_file() or path.stat().st_size != item.get("Size"):
        return False
    expected = item.get("Sha256") or ""
    if not expected:
        return True
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == expected


def download_modelscope(manifest: dict, destination: Path) -> None:
    files = manifest["Data"]["Files"]
    revision = files[0]["Revision"]
    for item in files:
        if item.get("Type") != "blob":
            continue
        name = item["Path"]
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe model file path")
        if relative.suffix not in {".json", ".safetensors", ".py", ".txt", ".jinja", ".model", ".tiktoken"}:
            continue
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if verify_modelscope(path, item):
            continue
        url = (f"https://modelscope.cn/api/v1/models/{manifest['repo']}/repo"
               f"?Revision={revision}&FilePath={urllib.parse.quote(name)}")
        partial = path.with_name(path.name + ".part")
        for attempt in range(5):
            try:
                offset = partial.stat().st_size if partial.exists() else 0
                request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
                with urllib.request.urlopen(request, timeout=120) as response:
                    if offset and response.status != 206:
                        offset = 0
                    if offset and not (response.headers.get("Content-Range") or "").startswith(f"bytes {offset}-"):
                        raise ValueError("Invalid resumed model response")
                    with partial.open("ab" if offset else "wb") as stream:
                        for data in iter(lambda: response.read(1024 * 1024), b""):
                            stream.write(data)
                if not verify_modelscope(partial, item):
                    partial.unlink(missing_ok=True)
                    raise ValueError(f"Official hash mismatch: {name}")
                partial.replace(path)
                print(f"Verified {name} ({path.stat().st_size} bytes)", flush=True)
                break
            except Exception as exc:
                print(f"Retry {attempt + 1} {name}: {type(exc).__name__}: {exc}", flush=True)
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 16))
    (destination / "official-snapshot.json").write_text(json.dumps(manifest, indent=2))


def download(manifest: dict, destination: Path, endpoint: str) -> None:
    revision = manifest["sha"]
    for item in manifest["siblings"]:
        name = item["rfilename"]
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe model file path")
        if relative.suffix not in {".json", ".safetensors", ".py", ".txt", ".jinja", ".model"}:
            continue
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if verify(path, item):
            continue
        url = f"{endpoint.rstrip('/')}/{manifest['id']}/resolve/{revision}/{urllib.parse.quote(name)}"
        partial = path.with_name(path.name + ".part")
        for attempt in range(5):
            try:
                offset = partial.stat().st_size if partial.exists() else 0
                request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
                with urllib.request.urlopen(request, timeout=60) as response:
                    if offset and response.status != 206:
                        offset = 0
                    if offset and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("Invalid resumed model response")
                    with partial.open("ab" if offset else "wb") as stream:
                        for data in iter(lambda: response.read(1024 * 1024), b""):
                            stream.write(data)
                if not verify(partial, item):
                    partial.unlink(missing_ok=True)
                    raise ValueError(f"Official hash mismatch: {name}")
                partial.replace(path)
                print(f"Verified {name} ({path.stat().st_size} bytes)", flush=True)
                break
            except Exception as exc:
                print(f"Retry {attempt + 1} {name}: {type(exc).__name__}: {exc}", flush=True)
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 16))
    (destination / "official-snapshot.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--endpoint", default="https://huggingface.co")
    parser.add_argument("--repo", default="", help="ModelScope repo id; selects the ModelScope manifest format")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.repo:
        manifest["repo"] = args.repo
        download_modelscope(manifest, args.destination)
    else:
        download(manifest, args.destination, args.endpoint)
