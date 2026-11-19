"""The HTTP API against a local release: scoring, clustering, configs, conversation ownership, release swaps and
the sign-in gate."""
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app
import release
import state
from conftest import principal

ALICE, BOB = principal("alice-oid"), principal("bob-oid")


def test_health_reports_the_live_release(client, data_store):
    assert client.get("/health").json()["data_version"] == release.current_version(data_store)


def test_meta_lists_levels_from_the_manifest(client):
    m = client.get("/meta?level=boroughs").json()
    assert [lv["key"] for lv in m["levels"]] == ["h3_8", "h3_7", "msoa", "boroughs"]
    assert len(m["cells"]) == len(m["raw"]) == 33


def test_unknown_level_is_a_client_error(client):
    assert client.get("/meta?level=nope").status_code == 422
    assert client.post("/score", json={"weights": {}, "level": "nope"}).status_code == 422


def test_no_weights_scores_everyone_equally(client):
    pct = client.post("/score", json={"weights": {}, "level": "msoa"}).json()["pct"]
    assert set(pct) == {0.5}


def test_exclusions_blank_out_zones(client):
    r = client.post("/score", json={"weights": {"population": 1}, "level": "msoa", "exclusions": {"flood": 0.05}}).json()
    assert r["excluded"] > 0 and r["pct"].count(None) == r["excluded"]


def test_top_score_is_the_most_populous_zone(client):
    m = client.get("/meta?level=boroughs").json()
    pct = client.post("/score", json={"weights": {"population": 1}, "level": "boroughs"}).json()["pct"]
    pop = [row[[f["key"] for f in m["features"]].index("population")] for row in m["raw"]]
    assert int(np.argmax(pct)) == int(np.argmax(pop))


def test_clusters_are_deterministic_and_numbered_by_size(client):
    body = {"weights": {"population": 1, "cbd_distance_m": 1}, "level": "msoa", "n_clusters": 4}
    a, b = client.post("/cluster", json=body).json(), client.post("/cluster", json=body).json()
    assert a["labels"] == b["labels"]
    sizes = np.bincount([x for x in a["labels"] if x is not None])
    assert list(sizes) == sorted(sizes, reverse=True)


def test_parse_config_validates_and_clips(client):
    ds = app.DATA
    req, mode = app.parse_config(ds, {"zones": "msoa", "mode": "cluster", "n_clusters": 50,
                                      "features": {"population": {"enabled": True, "weight": 3}},
                                      "hard_exclusions": {"flood": {"enabled": True, "threshold": 0}}})
    assert (mode, req.n_clusters, req.weights["population"], req.exclusions["flood"]) == ("cluster", 10, 1.0, 0.05)
    with pytest.raises(ValueError, match="Unknown keys"):
        app.parse_config(ds, {"features": {"nope": {"enabled": True}}})


def test_configs_are_private(client, state_dir):
    name = state.save_config(state.User("alice-oid"), "zones: msoa\n")
    assert client.get("/configs", headers=ALICE).json() == [{"owner": "alice-oid", "name": name}]
    assert client.get("/configs", headers=BOB).json() == []
    assert client.get(f"/configs/alice-oid/{name}", headers=ALICE).text == "zones: msoa\n"
    assert client.get(f"/configs/alice-oid/{name}", headers=BOB).status_code == 403
    admin = principal("admin-oid", roles=["Admin"])
    assert client.get(f"/configs/alice-oid/{name}", headers=admin).status_code == 200


def test_chat_refuses_someone_elses_conversation(client, state_dir, monkeypatch):
    state.claim_conversation(state.User("alice-oid"), "conv_alice")

    async def never(*a, **k):
        raise AssertionError("the agent must not run")
        yield
    monkeypatch.setattr(app.agent, "chat", never)
    r = client.post("/chat", headers=BOB, json={"message": "hi", "session_id": "conv_alice"})
    assert json.loads(r.text.strip()) == {"type": "error", "text": "Not your conversation"}


def test_sign_in_gate(client, monkeypatch):
    monkeypatch.setattr(app, "REQUIRE_SIGN_IN", True)
    assert client.get("/meta").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/meta", headers=ALICE).status_code == 200


def test_new_release_is_swapped_in_without_restart(data_store, state_dir, small_levels, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_STORE_URL", str(data_store.root))
    monkeypatch.setattr(app, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(app, "POLL_SECONDS", 0.05)
    with TestClient(app.app) as c:
        first = c.get("/health").json()["data_version"]
        v2 = release.publish(data_store, small_levels, promote_it=True)
        for _ in range(100):
            if c.get("/health").json()["data_version"] == v2:
                break
            time.sleep(0.05)
        assert first != v2 and c.get("/health").json()["data_version"] == v2
        assert [lv["key"] for lv in c.get("/meta").json()["levels"]] == ["msoa", "boroughs"]


def test_without_a_store_it_serves_data_levels(state_dir, monkeypatch):
    monkeypatch.delenv("DATA_STORE_URL", raising=False)
    with TestClient(app.app) as c:
        assert c.get("/health").json()["data_version"] == "local"
