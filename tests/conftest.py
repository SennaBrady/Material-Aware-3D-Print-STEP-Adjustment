import shutil
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(autouse=True)
def isolated_calibration(tmp_path, monkeypatch):
    """Never touch the user's real ~/.matprint/calibration.json."""
    monkeypatch.setenv("MATPRINT_HOME", str(tmp_path / "matprint_home"))


@pytest.fixture
def workdir(tmp_path):
    """A copy of the samples folder to run jobs in."""
    dst = tmp_path / "samples"
    shutil.copytree(SAMPLES, dst)
    return dst
