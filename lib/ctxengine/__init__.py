"""Context engine for tmux-pi workers (design: campaigns/qwen-trial/context-engine-design.md).

CX1 `ctx-store` owns this package marker and: store (note format, validation, files), search (scope globs, tags, BM25),
freshness (version, path and age flags), calls (the fail-closed call log), redact (the existing screens, wrapped) and
`bin/tp-ctx`. The other lanes add their own modules beside these; none of them may create this file.
"""
import sys
from pathlib import Path

# The engine reuses lib/ modules (safe_write, work_find, work_promote, sensitive_read) that are plain top-level modules.
_LIB = str(Path(__file__).resolve().parent.parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
