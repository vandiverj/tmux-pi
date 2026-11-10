"""tmux-pi product design workflow: the durable package contract and its deterministic helpers.

Standard library only (Python 3.9+). Modules:
  schema   a JSON Schema subset interpreter with the package's id/reference/bookkeeping extensions
  package  load, validate, fingerprint, and evaluate slice readiness for a package directory
  store    the writer lock, the journaled atomic commit and its recovery, the commit record, external-edit detection
  changes  changesets (apply), impact, feedback ingestion
  review   the offline review build: prototype adapter, note target registry, the page and its record
  cli      `bin/tp-product`: init, status, validate, apply, build, import-feedback
The contract these implement is described in docs/product-design.md.
"""
