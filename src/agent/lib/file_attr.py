from __future__ import annotations

# Lower 16 bits: classic Unix mode bits (default rw-rw-r--).
FA_UNIX_MODE_DEFAULT = 0o664

# High/internal bits (runtime flags).
FA_CODE_FILE = 1 << 16

