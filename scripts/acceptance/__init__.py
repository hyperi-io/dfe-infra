#  Project:      dfe-infra
#  File:         acceptance/__init__.py
#  Purpose:      Package marker for the live acceptance suites this repo owns
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Acceptance suites that need more than a pytest process.

The data-path suites live in dfe-engine and run from a checkout of it. What sits
here is the half that cannot: driving a real browser through the console.
"""
