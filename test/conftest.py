from __future__ import annotations

import sys
import os
from pathlib import Path


os.environ.setdefault("GPU_MCP_TEST_DISABLE_POLICY_APPROVAL", "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))
