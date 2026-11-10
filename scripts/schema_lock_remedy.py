#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         schema_lock_remedy.py
#  Purpose:      The one ClickHouse statement dfe-ops prints, never runs: the
#                operator's DROP that clears a wedged engine schema lock.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The remedy text for a wedged schema_lock, kept apart so no other deploy file carries DDL."""


def schema_lock_drop(database: str) -> str:
    """The statement to run once while no engine holds the lease. The engine recreates the table at boot."""
    return f"DROP TABLE {database}.schema_lock SYNC"
