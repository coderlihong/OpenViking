import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# AGFS queue control operations are addressed as virtual files under the queue
# directory (e.g. /queue/Embedding/enqueue). The real Rust binding implements
# these as queue semantics rather than plain file I/O; the mock must match that
# contract or queue workers never see any pending messages.
_QUEUE_CONTROL_OPS = {
    "enqueue",
    "dequeue",
    "ack",
    "size",
    "status",
    "messages",
    "peek",
    "clear",
}


class MockLocalAGFS:
    """
    A mock implementation of the AGFS binding client that operates on a local
    directory. Useful for tests where running the real RAGFS binding is not
    feasible or desired.
    """

    def __init__(self, config=None, root_path=None):
        self.config = config
        self.root = Path(root_path) if root_path else Path("/tmp/viking_data")
        self.root.mkdir(parents=True, exist_ok=True)
        self._pathlocks_guard = threading.Lock()
        self._pathlocks = {}
        self._pathlock_leases = {}
        # Queue control state: queue_dir -> {"pending": [...], "processing": [...]}
        self._queue_guard = threading.Lock()
        self._queues = {}

    def _resolve(self, path):
        if str(path).startswith("viking://"):
            path = str(path).replace("viking://", "")
        if str(path).startswith("/"):
            path = str(path)[1:]
        return self.root / path

    @staticmethod
    def _queue_control(path):
        """Return (queue_dir, op) when path addresses an AGFS queue control file."""
        normalized = str(path).strip("/")
        if "viking://" in normalized:
            normalized = normalized.split("viking://", 1)[1]
        parts = normalized.split("/")
        if (
            len(parts) >= 2
            and "queue" in parts
            and parts[-1] in _QUEUE_CONTROL_OPS
        ):
            return "/".join(parts[:-1]), parts[-1]
        return None

    def _queue_state(self, queue_dir):
        with self._queue_guard:
            return self._queues.setdefault(
                queue_dir, {"pending": [], "processing": []}
            )

    def _queue_write(self, queue_dir, op, content):
        state = self._queue_state(queue_dir)
        with self._queue_guard:
            if op == "enqueue":
                raw = content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else str(content)
                msg_id = str(uuid.uuid4())
                # The backend stores the raw payload under a "data" envelope; the
                # dequeue handler reads ``data["data"]`` as the serialized payload.
                state["pending"].append({"id": msg_id, "data": raw})
                return msg_id
            if op == "ack":
                msg_id = content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else str(content)
                state["processing"] = [m for m in state["processing"] if m.get("id") != msg_id]
                state["pending"] = [m for m in state["pending"] if m.get("id") != msg_id]
                return msg_id
            if op == "clear":
                state["pending"].clear()
                state["processing"].clear()
                return ""
        return ""

    def _queue_read(self, queue_dir, op):
        state = self._queue_state(queue_dir)
        with self._queue_guard:
            if op == "dequeue":
                if not state["pending"]:
                    return b"{}"
                msg = state["pending"].pop(0)
                state["processing"].append(msg)
                return json.dumps(msg).encode("utf-8")
            if op == "peek":
                if not state["pending"]:
                    return b"{}"
                return json.dumps(state["pending"][0]).encode("utf-8")
            if op == "size":
                return str(len(state["pending"])).encode("utf-8")
            if op == "status":
                return json.dumps(
                    {"pending": len(state["pending"]), "processing": len(state["processing"])}
                ).encode("utf-8")
            if op == "messages":
                return json.dumps(state["pending"] + state["processing"]).encode("utf-8")
        return b"{}"

    def exists(self, path, ctx=None):
        return self._resolve(path).exists()

    def mkdir(self, path, ctx=None, parents=True, exist_ok=True):
        self._resolve(path).mkdir(parents=parents, exist_ok=exist_ok)

    def ls(self, path, ctx=None, **kwargs):
        """List test entries with the same sorting and pagination contract as RagFS."""
        p = self._resolve(path)
        if not p.exists():
            return []
        res = []
        for item in p.iterdir():
            res.append(
                {
                    "name": item.name,
                    "isDir": item.is_dir(),  # Note: JS style camelCase for some APIs
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0,
                    "mtime": item.stat().st_mtime,
                    "uri": f"viking://{path}/{item.name}".replace("//", "/"),
                }
            )
        sort_by = kwargs.get("sort_by")
        reverse = kwargs.get("sort_order", "asc") == "desc"
        if sort_by:
            key = (
                (lambda entry: (str(entry["name"]).lower(), str(entry["name"])))
                if sort_by == "name"
                else (
                    lambda entry: (
                        entry["mtime"],
                        str(entry["name"]).lower(),
                        str(entry["name"]),
                    )
                )
            )
            directories = sorted(
                (entry for entry in res if entry["isDir"]), key=key, reverse=reverse
            )
            files = sorted((entry for entry in res if not entry["isDir"]), key=key, reverse=reverse)
            res = directories + files
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit")
        return res[offset:] if limit is None else res[offset : offset + limit]

    def glob_directory(
        self,
        path,
        pattern,
        show_hidden=False,
        page_size=None,
        level_limit=None,
        continuation_token=None,
        ctx=None,
    ):
        del ctx
        root = self._resolve(path)
        matches = []
        for item in sorted(root.glob(pattern)):
            relative = item.relative_to(root)
            if not show_hidden and any(part.startswith(".") for part in relative.parts):
                continue
            if level_limit is not None and len(relative.parts) > level_limit:
                continue
            matches.append(
                {
                    "path": f"{str(path).rstrip('/')}/{relative.as_posix()}",
                    "rel_path": relative.as_posix(),
                    "name": item.name,
                    "is_dir": item.is_dir(),
                }
            )

        start = int(continuation_token or 0)
        end = len(matches) if not page_size else start + page_size
        return {
            "entries": matches[start:end],
            "next_token": str(end) if end < len(matches) else None,
        }

    def tree_directory(
        self,
        path,
        show_hidden=False,
        node_limit=None,
        level_limit=None,
        ctx=None,
        *,
        offset=0,
        sort_by=None,
        sort_order="asc",
    ):
        """Recursive directory traversal matching the RagFS tree_directory contract."""
        del ctx
        root = self._resolve(path)
        if not root.exists():
            raise FileNotFoundError(path)
        base = str(path).rstrip("/")
        entries = []
        for item in sorted(root.rglob("*")):
            relative = item.relative_to(root)
            if not show_hidden and any(part.startswith(".") for part in relative.parts):
                continue
            if level_limit is not None and len(relative.parts) > level_limit:
                continue
            stat = item.stat()
            entries.append(
                {
                    "path": f"{base}/{relative.as_posix()}",
                    "rel_path": relative.as_posix(),
                    "info": {
                        "name": item.name,
                        "isDir": item.is_dir(),
                        "size": stat.st_size if item.is_file() else 0,
                        "mode": 0o755 if item.is_dir() else 0o644,
                        "modTime": _iso(stat.st_mtime),
                    },
                    "extra": {},
                }
            )
        if sort_by == "name":
            entries.sort(
                key=lambda e: (str(e["info"]["name"]).lower(), str(e["info"]["name"])),
                reverse=sort_order == "desc",
            )
        else:
            entries.sort(
                key=lambda e: (e["info"]["modTime"], str(e["info"]["name"]).lower()),
                reverse=sort_order == "desc",
            )
        sliced = entries[offset:]
        if node_limit is not None:
            sliced = sliced[:node_limit]
        return sliced

    def writeto(self, path, content, ctx=None, **kwargs):
        control = self._queue_control(path)
        if control is not None:
            queue_dir, op = control
            return self._queue_write(queue_dir, op, content)
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
        else:
            p.write_bytes(content)
        return str(p)

    def write(self, path, content, ctx=None, **kwargs):
        return self.writeto(path, content, ctx, **kwargs)

    def write_file(self, path, content, ctx=None, **kwargs):
        return self.writeto(path, content, ctx, **kwargs)

    def read_file(self, path, ctx=None, **kwargs):
        control = self._queue_control(path)
        if control is not None:
            queue_dir, op = control
            return self._queue_read(queue_dir, op)
        p = self._resolve(path)
        if not p.exists():
            raise FileNotFoundError(path)
        return p.read_bytes()

    def read(self, path, ctx=None, **kwargs):
        return self.read_file(path, ctx, **kwargs)

    def cat(self, path, ctx=None, **kwargs):
        return self.read_file(path, ctx, **kwargs)

    def rm(self, path, recursive=False, ctx=None):
        p = self._resolve(path)
        if p.exists():
            if p.is_dir():
                if recursive:
                    shutil.rmtree(p)
                else:
                    p.rmdir()
            else:
                p.unlink()

    def delete_temp(self, path, ctx=None):
        self.rm(path, recursive=True, ctx=ctx)

    def mv(self, src, dst, ctx=None):
        s = self._resolve(src)
        d = self._resolve(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))

    def stat(self, path, ctx=None):
        p = self._resolve(path)
        if not p.exists():
            raise FileNotFoundError(path)
        s = p.stat()
        return {
            "size": s.st_size,
            "mtime": s.st_mtime,
            "isDir": p.is_dir(),
            "is_dir": p.is_dir(),
        }

    def bind_request_context(self, ctx):
        return MagicMock(__enter__=lambda x: None, __exit__=lambda x, y, z: None)

    def pathlock_acquire_tree(
        self,
        ctx,
        path,
        timeout_secs=0.0,
        owner_lease_ref=None,
    ):
        del ctx, owner_lease_ref
        with self._pathlocks_guard:
            lock = self._pathlocks.setdefault(path, threading.Lock())

        acquired = lock.acquire(timeout=timeout_secs)
        if not acquired:
            raise TimeoutError(f"timed out acquiring test path lock: {path}")

        lease_ref = str(uuid.uuid4())
        lease = {
            "lease_ref": lease_ref,
            "ownership_ref": str(uuid.uuid4()),
            "owner_id": "mock-local-agfs",
            "owned": True,
        }
        with self._pathlocks_guard:
            self._pathlock_leases[lease_ref] = lock
        return lease

    pathlock_acquire_exact = pathlock_acquire_tree

    def pathlock_release(self, ctx, owned_lease_ref):
        del ctx
        lease_ref = owned_lease_ref["lease_ref"]
        with self._pathlocks_guard:
            lock = self._pathlock_leases.pop(lease_ref)
        lock.release()

    def pathlock_as_borrowed(self, ctx, owned_lease_ref):
        del ctx
        return dict(owned_lease_ref, borrowed=True)

    def pathlock_refresh(self, ctx, owned_lease_ref):
        del ctx, owned_lease_ref
        return "refreshed"

    def pathlock_to_handoff(self, ctx, owned_lease_ref):
        del ctx
        return dict(owned_lease_ref, handoff=True)

    def pathlock_handoff(self, ctx, owned_lease_ref):
        del ctx, owned_lease_ref

    def pathlock_adopt(self, ctx, handoff_ref):
        del ctx
        return dict(handoff_ref, owned=True)

    def pathlock_release_selected(self, ctx, owned_lease_ref, lock_paths):
        del ctx, owned_lease_ref, lock_paths

    def pathlock_is_locked(self, ctx, path, ignore_stale=True):
        del ctx, path, ignore_stale
        return False

    def pathlock_observe(self, ctx):
        del ctx
        return {"locks": []}
