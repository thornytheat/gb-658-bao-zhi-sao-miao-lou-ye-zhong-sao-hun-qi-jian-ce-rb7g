import os

os.environ["TZ"] = "Asia/Shanghai"

import pytest


@pytest.fixture()
def db(monkeypatch, tmp_path):
    import app.store as store

    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "sync.sqlite3")
    store.init_db()
    return tmp_path / "sync.sqlite3"
