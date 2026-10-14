# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Constants definition module

This module centralizes all constants used in VikingVectorIndex,
avoiding magic strings scattered throughout the code, improving code maintainability.
"""

from enum import Enum


# ==================== Table name constants ====================
class TableNames(str, Enum):
    """Storage table name enum"""

    CANDIDATES = "C"  # Candidate data table
    DELTA = "D"  # Delta data table
    TTL = "T"  # TTL expiration time table


# ==================== Special field names ====================
class SpecialFields(str, Enum):
    """Special field name enum"""

    AUTO_ID = "AUTO_ID"  # Auto-generated primary key field name


# ==================== Aggregate operation related ====================
class AggregateKeys(str, Enum):
    """Aggregate operation related key names"""

    TOTAL_COUNT_INTERNAL = "__total_count__"  # Internal total key name
    TOTAL_COUNT_EXTERNAL = "_total"  # External return total key name


# ==================== Index related constants ====================
class IndexFileMarkers(str, Enum):
    """Index file markers"""

    WRITE_DONE = ".write_done"  # Index write complete marker file suffix


# Names of the files the native index manager writes into one version snapshot
# directory.  They must stay in sync with the C++ layout
# (``src/index/detail/index_manager_impl.cpp`` kMetaFile / kVectorIndexDir /
# kScalarIndexDir, ``src/index/detail/vector/common/bruteforce.h``
# kFlatIndexFileName and ``src/index/detail/scalar/scalar_index.cpp``
# kIndexDataFile).  The Python layer only uses them to tell a *complete*
# snapshot apart from one that a crash left half-written.
INDEX_MANAGER_META_FILE = "manager_meta.json"
INDEX_VECTOR_DIR = "vector_index"
INDEX_VECTOR_FLAT_FILE = "index_flat.data"
INDEX_SCALAR_DIR = "scalar_index"
INDEX_SCALAR_DATA_FILE = "scalar_index.data"


# ==================== Scheduler related constants ====================
DEFAULT_TTL_CLEANUP_SECONDS = 0  # TTL expired data cleanup interval (seconds)
DEFAULT_INDEX_MAINTENANCE_SECONDS = 30  # Index maintenance task interval (seconds)

# Environment variable names
ENV_TTL_CLEANUP_SECONDS = "VECTORDB_TTL_CLEANUP_SECONDS"
ENV_INDEX_MAINTENANCE_SECONDS = "VECTORDB_INDEX_MAINTENANCE_SECONDS"


# ==================== Other constants ====================
DEFAULT_LIMIT = 10  # Default search return result count
STORAGE_DIR_NAME = "store"  # Storage directory name
