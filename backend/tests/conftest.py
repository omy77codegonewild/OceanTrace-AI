import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# isolated data dir per test session
_tmp = Path(os.environ.get("OT_TEST_TMP", "/tmp/oceantrace-test"))
_tmp.mkdir(parents=True, exist_ok=True)
os.environ["OT_DATA_DIR"] = str(_tmp)
os.environ["OT_DB_PATH"] = str(_tmp / "test.sqlite")


@pytest.fixture(scope="session")
def tmp_data_dir() -> Path:
    return _tmp
