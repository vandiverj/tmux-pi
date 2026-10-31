"""Offline, read-only library docs for workers: api_signature and docs_search over lockfile-pinned sources.

Nothing in this package opens a socket except fetch.py, which only `tp-docs fetch` imports.
"""
