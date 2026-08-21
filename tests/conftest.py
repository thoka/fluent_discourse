"""
Make the test suite runnable with a plain `pytest`.

Without this, the tests need both a PYTHONPATH covering `src/` and `tests/`
and three DISCOURSE_* environment variables - the unit tests never talk to a
server, so requiring real ones only kept them from being run at all.
The integration tests read the same variables and are skipped separately.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("DISCOURSE_URL", "http://localhost:3000")
os.environ.setdefault("DISCOURSE_USERNAME", "test_user")
os.environ.setdefault("DISCOURSE_API_KEY", "not-a-real-key")
