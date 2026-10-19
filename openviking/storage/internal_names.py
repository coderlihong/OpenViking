"""Shared internal file-name constants for Python storage code."""

from __future__ import annotations

MULTIWRITE_PATH_LOCK_FILE = ".path.ovlock"
MULTIWRITE_EXACT_LOCK_FILE_PREFIX = ".exact.ovlock."
MULTIWRITE_REDIRECT_FILE = ".redirect.json"
MULTIWRITE_SYNC_LOG_FILE = ".sync_log.json"

MULTIWRITE_INTERNAL_FILE_NAMES = frozenset(
    {
        MULTIWRITE_PATH_LOCK_FILE,
        MULTIWRITE_REDIRECT_FILE,
        MULTIWRITE_SYNC_LOG_FILE,
    }
)

STORAGE_INTERNAL_ENTRY_NAMES = frozenset(
    {
        "_system",
        "tasks",
        *MULTIWRITE_INTERNAL_FILE_NAMES,
    }
)

WEBDAV_RESERVED_FILENAMES = frozenset(
    {
        ".abstract.md",
        ".overview.md",
        ".relations.json",
        *MULTIWRITE_INTERNAL_FILE_NAMES,
    }
)

# Parser control records remain readable but are not source knowledge.
# ``toc.md`` is the parser's human-readable navigation index (a flat list of
# every chapter title and path); indexing it would let the table of contents
# outrank the chapters it points at.
#
# ``index.json`` is the same record in machine-readable form -- the importer
# writes one per corpus root as document_id -> files[{chapter_uri, path, pages}].
# Live, the export refused a whole tree because that one file was expected to
# carry an L2 row while the ingestion path never gave it one: asking for it
# directly walks it as a directory and dies with AGFSNotADirectoryError. Keeping
# it here holds ingestion, the pack contents and the export expectation in sync.
CHAPTER_METADATA_FILENAMES = frozenset(
    {"toc.md", "_toc.json", "_manifest.json", "index.json"}
)
