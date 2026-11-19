"""Shared fixtures: a data store with one live release built from data/levels, an empty state store, and the app
running against both. Nothing touches Azure."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import release
from store import LocalStore

LEVELS = Path(__file__).parent.parent / "data" / "levels"


@pytest.fixture
def data_store(tmp_path):
    """A local data store holding one live release of data/levels."""
    store = LocalStore(tmp_path / "data")
    release.publish(store, LEVELS, promote_it=True)
    return store


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """An empty local state store, set as STATE_STORE_URL."""
    folder = tmp_path / "state"
    monkeypatch.setenv("STATE_STORE_URL", str(folder))
    return folder


@pytest.fixture
def client(data_store, state_dir, tmp_path, monkeypatch):
    """The app serving the live release from ``data_store``, with its own state store and cache."""
    import app
    monkeypatch.setenv("DATA_STORE_URL", str(data_store.root))
    monkeypatch.setattr(app, "CACHE", tmp_path / "cache")
    with TestClient(app.app) as c:
        yield c


@pytest.fixture
def small_levels(tmp_path):
    """A copy of data/levels with only the two coarse levels, so a second release differs from the first."""
    folder = tmp_path / "small"
    folder.mkdir()
    for k in ("msoa", "boroughs"):
        shutil.copyfile(LEVELS / f"{k}.parquet", folder / f"{k}.parquet")
    return folder


def principal(oid, roles=()):
    """Headers Container Apps sign-in would add for a user with these app roles."""
    import base64
    import json
    claims = [{"typ": "roles", "val": r} for r in roles]
    blob = base64.b64encode(json.dumps({"claims": claims, "role_typ": "roles"}).encode()).decode()
    return {"x-ms-client-principal-id": oid, "x-ms-client-principal-name": f"{oid}@example.org",
            "x-ms-client-principal": blob}
