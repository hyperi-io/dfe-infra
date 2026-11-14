#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_schema_lock_remedy.py
#  Purpose:      Pin the remedy dfe-ops upgrade preflight prints for an
#                engine schema lock whose quorum part is lost.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The printed DROP names the database it was found in, and replicates with SYNC.

python3 -m pytest scripts/tests/test_schema_lock_remedy.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schema_lock_remedy


def test_the_drop_names_the_database_and_waits_for_every_replica() -> None:
    assert (
        schema_lock_remedy.schema_lock_drop("dfe") == "DROP TABLE dfe.schema_lock SYNC"
    )
    assert (
        schema_lock_remedy.schema_lock_drop("acme_data")
        == "DROP TABLE acme_data.schema_lock SYNC"
    )
