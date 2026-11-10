"""tmux-pi product design workflow: the durable package contract and its deterministic helpers.

Standard library only (Python 3.9+). Modules:
  schema   a JSON Schema subset interpreter with the package's id/reference/bookkeeping extensions
  package  load, validate, fingerprint, and evaluate slice readiness for a package directory
The contract these implement is described in docs/product-design.md.
"""
