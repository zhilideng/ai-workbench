import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离的数据目录 + 一个含两个项目的项目根目录。"""
    from workbench.core import settings
    monkeypatch.delenv("WB_PROJECTS_ROOT", raising=False)
    root = tmp_path / "projects"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    s = settings.configure(tmp_path / "data")
    s.update(projects_root=str(root))
    return s


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from workbench.api import create_app
    with TestClient(create_app()) as c:
        yield c
