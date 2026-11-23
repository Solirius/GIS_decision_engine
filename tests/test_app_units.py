"""The app's building blocks, called directly: loading levels and releases, scoring, clustering, describing data
and checking configs for the agent. The endpoints themselves are in test_app.py."""
import asyncio
import json
import logging
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
from fastapi import HTTPException

import app
import release
from conftest import LEVELS, principal
from store import LocalStore


@pytest.fixture(scope="module")
def ds():
    """The tables in data/levels, loaded once for the module."""
    return app.Dataset(*release.local())


def req(weights=None, level="msoa", exclusions=None, n_clusters=5):
    return app.ClusterRequest(weights=weights or {}, level=level, exclusions=exclusions or {}, n_clusters=n_clusters)


# --- Loading -----------------------------------------------------------------------------------------------------

def test_ring_rounds_to_five_decimals():
    assert app.ring([(0.123456789, 51.987654321), (1, 2)]) == [[0.12346, 51.98765], [1, 2]]
    assert app.ring([]) == []


def test_load_level_reads_features_shapes_percentiles_and_coverage():
    feats, L = app.load_level(LEVELS / "boroughs.parquet")
    assert "zone_id" not in feats and not any(f.startswith("cov_") for f in feats) and "population" in feats
    assert len(L["cells"]) == len(L["raw"]) == len(L["norm"]) == 33
    assert all(len(row) == len(feats) for row in L["raw"])
    assert all(v == round(v, 1) for row in L["raw"] for v in row)
    assert L["norm"].shape == (33, len(feats)) and L["norm"].min() > 0 and L["norm"].max() == 1
    assert set(L["cov"]) == {"flood", "habitats", "monuments", "brownfield"}
    assert all(0 <= c.min() and c.max() <= 1 for c in L["cov"].values())
    cell = L["cells"][0]
    assert isinstance(cell["id"], str) and cell["polys"] and all(len(p) >= 4 and p[0] == p[-1] for p in cell["polys"])


def test_load_level_keeps_every_polygon_of_a_multipolygon_zone(tmp_path):
    g = gpd.read_parquet(LEVELS / "boroughs.parquet")
    parts = [len(geom.geoms) if geom.geom_type == "MultiPolygon" else 1 for geom in g.geometry]
    _, L = app.load_level(LEVELS / "boroughs.parquet")
    assert [len(c["polys"]) for c in L["cells"]] == parts


def test_dataset_reads_the_manifest(ds):
    assert ds.version == "local" and ds.base_level == "h3_8"
    assert ds.labels == {"h3_8": "H3 8", "h3_7": "H3 7", "msoa": "MSOA", "boroughs": "Boroughs"}
    assert ds.features == [c for c in ds.idx] and ds.idx["population"] == ds.features.index("population")
    assert ds.agg["population"] == ds.agg["brownfield_ha"] == ds.agg["poi_health_care"] == "sum"
    assert ds.agg["cbd_distance_m"] == ds.agg["green_belt_pct"] == "mean"


def test_dataset_refuses_levels_with_different_features(tmp_path):
    levels = tmp_path / "levels"
    levels.mkdir()
    gpd.read_parquet(LEVELS / "msoa.parquet").to_parquet(levels / "msoa.parquet")
    g = gpd.read_parquet(LEVELS / "boroughs.parquet")
    g["extra"] = 1.0
    g.to_parquet(levels / "boroughs.parquet")
    with pytest.raises(ValueError, match="Level boroughs has different features"):
        app.Dataset(tmp_path, release.build_manifest(levels, "v"))


def test_dataset_level(ds):
    assert ds.level(None) is ds.levels["h3_8"]
    assert ds.level("msoa") is ds.levels["msoa"]
    with pytest.raises(HTTPException) as e:
        ds.level("nope")
    assert e.value.status_code == 422


def test_load_live_without_a_store_serves_data_levels(monkeypatch):
    monkeypatch.delenv("DATA_STORE_URL", raising=False)
    assert app.load_live().version == "local"


def test_load_live_with_nothing_promoted_serves_data_levels(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATA_STORE_URL", str(tmp_path / "empty"))
    monkeypatch.setattr(app, "CACHE", tmp_path / "cache")
    with caplog.at_level(logging.WARNING, "uvicorn.error"):
        assert app.load_live().version == "local"
    assert "No live release" in caplog.text


def test_load_live_serves_the_live_release(data_store, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_STORE_URL", str(data_store.root))
    monkeypatch.setattr(app, "CACHE", tmp_path / "cache")
    assert app.load_live().version == release.current_version(data_store)
    assert (tmp_path / "cache" / release.current_version(data_store)).is_dir()


def watch_until(condition):
    """Run watch_releases until ``condition()`` holds (or 5 s pass), then stop it."""
    async def go():
        task = asyncio.create_task(app.watch_releases())
        for _ in range(500):
            if condition() or task.done():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        return task
    return asyncio.run(go())


def test_watch_releases_retries_after_an_error(data_store, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATA_STORE_URL", str(data_store.root))
    monkeypatch.setattr(app, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(app, "POLL_SECONDS", 0)
    monkeypatch.setattr(app, "DATA", SimpleNamespace(version="old"))
    monkeypatch.setattr(app, "Dataset", lambda folder, manifest: SimpleNamespace(version=manifest["version"]))
    real, calls = release.current_version, []

    def flaky(store):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("blob storage unreachable")
        return real(store)
    monkeypatch.setattr(release, "current_version", flaky)
    live = real(data_store)
    with caplog.at_level(logging.ERROR, "uvicorn.error"):
        watch_until(lambda: app.DATA.version == live)
    assert app.DATA.version == live and len(calls) >= 2
    assert "will retry" in caplog.text


def test_watch_releases_keeps_the_data_when_nothing_changed(data_store, monkeypatch):
    monkeypatch.setenv("DATA_STORE_URL", str(data_store.root))
    monkeypatch.setattr(app, "POLL_SECONDS", 0)
    current = SimpleNamespace(version=release.current_version(data_store))
    monkeypatch.setattr(app, "DATA", current)
    monkeypatch.setattr(app, "Dataset", lambda *a: pytest.fail("must not reload the same release"))
    calls = []
    monkeypatch.setattr(release, "current_version", lambda s: calls.append(1) or current.version)
    watch_until(lambda: len(calls) >= 3)
    assert app.DATA is current


def test_watch_releases_stops_at_once_without_a_store(monkeypatch):
    monkeypatch.delenv("DATA_STORE_URL", raising=False)
    assert watch_until(lambda: False).done()


# --- Sidebar labels ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key, grp", [("poi_education", "Amenities (POI count)"), ("green_belt_pct", "Policy"),
                                      ("brownfield_ha", "Policy"), ("population", "Core"), ("cbd_distance_m", "Core")])
def test_group(key, grp):
    assert app.group(key) == grp


@pytest.mark.parametrize("key, text", [("poi_health_care", "Health care"), ("population", "Population"),
                                       ("cbd_distance_m", "CBD distance (m)"), ("station_count", "Station count"),
                                       ("brownfield_ha", "Brownfield land (ha)")])
def test_label(key, text):
    assert app.label(key) == text


# --- Exclusions, scoring and clustering --------------------------------------------------------------------------

def test_excluded_mask(ds):
    cov = ds.levels["msoa"]["cov"]["flood"]
    assert not app.excluded_mask(ds, req()).any()
    assert not app.excluded_mask(ds, req(exclusions={"not_a_layer": 0.0})).any()  # unknown layers ignored
    assert (app.excluded_mask(ds, req(exclusions={"flood": 0.2})) == (cov >= 0.2 - 1e-6)).all()
    both = app.excluded_mask(ds, req(exclusions={"flood": 0.2, "habitats": 0.2}))
    assert (both == ((cov >= 0.2 - 1e-6) | (ds.levels["msoa"]["cov"]["habitats"] >= 0.2 - 1e-6))).all()


def tiny(norm, **cov):
    """A stand-in Dataset with one level, "t", of len(norm) zones."""
    norm = np.asarray(norm, dtype=float)
    L = {"norm": norm, "cov": {g: np.asarray(cov.get(g, [0.0] * len(norm))) for g in app.EXCLUSIONS},
         "cells": [{"id": f"z{i}", "polys": []} for i in range(len(norm))], "raw": norm.tolist()}
    feats = [f"f{i}" for i in range(norm.shape[1])]
    return SimpleNamespace(level=lambda k: L, levels={"t": L}, base_level="t", idx={f: i for i, f in enumerate(feats)},
                           features=feats, version="t")


def test_excluded_mask_counts_fully_covered_zones_at_100_percent():
    ds = tiny([[0.1], [0.2], [0.3]], flood=[1 - 1e-9, 0.999, 0.0])  # float error in a fully covered zone
    assert app.excluded_mask(ds, req(exclusions={"flood": 1.0})).tolist() == [True, False, False]


def test_with_gaps():
    mask = np.array([False, True, False, True])
    assert app.with_gaps(["a", "b"], mask) == ["a", None, "b", None]
    assert app.with_gaps([], np.array([True, True])) == [None, None]
    assert app.with_gaps([], np.array([], dtype=bool)) == []


def test_score_ignores_unknown_features(ds):
    r = app.score_zones(ds, req({"not_a_feature": 1}))
    assert set(r["pct"]) == {0.5} and r["excluded"] == 0 and r["version"] == "local"


def test_negative_weight_reverses_the_ranking(ds):
    up = app.score_zones(ds, req({"cbd_distance_m": 1}))["pct"]
    down = app.score_zones(ds, req({"cbd_distance_m": -1}))["pct"]
    assert int(np.argmax(up)) == int(np.argmin(down))
    assert all(0 < p <= 1 for p in up) and all(p == round(p, 4) for p in up)


def test_score_percentiles_are_among_surviving_zones(ds):
    r = app.score_zones(ds, req({"population": 1}, exclusions={"flood": 0.05}))
    kept = [p for p in r["pct"] if p is not None]
    assert len(kept) == len(r["pct"]) - r["excluded"] and max(kept) == 1.0


def test_score_with_every_zone_excluded(ds):
    r = app.score_zones(ds, req({"population": 1}, exclusions={"flood": 0.0, "habitats": 0.0}))
    assert r["excluded"] == len(r["pct"]) and set(r["pct"]) == {None}


def test_cluster_without_features_is_one_group(ds):
    r = app.cluster_zones(ds, req({"not_a_feature": 1}))
    assert r["keys"] == [] and set(r["labels"]) == {0} and r["centers"] == [[]]


def test_cluster_of_one(ds):
    r = app.cluster_zones(ds, req({"population": 1, "cbd_distance_m": 0}, n_clusters=1))
    assert set(r["labels"]) == {0} and r["keys"] == ["population", "cbd_distance_m"]
    assert len(r["centers"]) == 1 and len(r["centers"][0]) == 2 and all(0 <= c <= 100 for c in r["centers"][0])


def test_cluster_with_every_zone_excluded(ds):
    r = app.cluster_zones(ds, req({"population": 1}, exclusions={"flood": 0.0}))
    assert set(r["labels"]) == {None} and r["centers"] == [] and r["excluded"] == len(r["labels"])


def test_cluster_count_is_capped_by_surviving_zones():
    ds = tiny([[0.1, 0.9], [0.5, 0.5], [0.9, 0.1], [0.3, 0.3]], flood=[0, 0, 0, 1])
    r = app.cluster_zones(ds, req({"f0": 1, "f1": 1}, n_clusters=10, exclusions={"flood": 1}))
    assert r["labels"][3] is None and sorted(r["labels"][:3]) == [0, 1, 2]
    assert sorted(map(tuple, r["centers"])) == [(10.0, 90.0), (50.0, 50.0), (90.0, 10.0)]


def test_cluster_weights_are_ignored(ds):
    a = app.cluster_zones(ds, req({"population": 1, "cbd_distance_m": 1}, n_clusters=3))
    b = app.cluster_zones(ds, req({"population": -0.2, "cbd_distance_m": 0.7}, n_clusters=3))
    assert a["labels"] == b["labels"] and a["centers"] == b["centers"]


# --- What the agent sees -----------------------------------------------------------------------------------------

def test_describe(ds):
    d = app.describe(ds, "boroughs")
    assert d["described_level"] == "boroughs"
    assert d["zone_levels"]["boroughs"] == {"label": "Boroughs", "zones": 33, "zones_missing_data": 0}
    assert [f["key"] for f in d["features"]] == ds.features
    pop = next(f for f in d["features"] if f["key"] == "population")
    assert pop["description"] == "Resident population" and pop["aggregation"] == "sum" and pop["group"] == "Core"
    q = pop["min_p25_median_p75_max"]
    assert q == sorted(q) and len(q) == 5
    poi = next(f for f in d["features"] if f["key"] == "poi_education")
    assert poi["description"] == "Count of education points of interest (Overture)"
    assert 0 <= poi["share_of_zones_zero"] <= 1
    pairs = d["most_correlated_feature_pairs"]
    assert len(pairs) == 12 and all(-1 <= p["spearman"] <= 1 for p in pairs)
    assert [abs(p["spearman"]) for p in pairs] == sorted((abs(p["spearman"]) for p in pairs), reverse=True)
    assert [e["key"] for e in d["hard_exclusions"]] == list(app.EXCLUSIONS)
    flood = d["hard_exclusions"][0]
    assert flood["zones_covered_at_least_50pct"] >= flood["zones_fully_covered"] >= 0
    json.dumps(d)  # sent to the model as JSON


def test_describe_defaults_to_the_base_level_and_refuses_unknown_ones(ds):
    assert app.describe(ds)["described_level"] == "h3_8"
    with pytest.raises(ValueError, match="Unknown zones"):
        app.describe(ds, "nope")


def test_parse_config_defaults(ds):
    r, mode = app.parse_config(ds, {})
    assert (r.level, mode, r.n_clusters, r.weights, r.exclusions) == ("h3_8", "score", 5, {}, {})


def test_parse_config_switches_off_what_is_not_enabled(ds):
    r, _ = app.parse_config(ds, {"features": {"population": {"enabled": True}, "cbd_distance_m": {"enabled": False},
                                              "station_count": None},
                                 "hard_exclusions": {"flood": {"enabled": True}, "habitats": None,
                                                     "monuments": {"enabled": False, "threshold": 0.5}}})
    assert r.weights == {"population": 0.5} and r.exclusions == {"flood": 1.0}


def test_parse_config_clips_numbers(ds):
    r, _ = app.parse_config(ds, {"n_clusters": 0, "features": {"population": {"enabled": True, "weight": -7}},
                                 "hard_exclusions": {"flood": {"enabled": True, "threshold": 3}}})
    assert (r.n_clusters, r.weights["population"], r.exclusions["flood"]) == (1, -1.0, 1.0)


@pytest.mark.parametrize("cfg, msg", [({"zones": "nope"}, "Unknown zones"), ({"mode": "rank"}, "mode must be"),
                                      ({"hard_exclusions": {"lava": {"enabled": True}}}, "Unknown keys")])
def test_parse_config_refuses(ds, cfg, msg):
    with pytest.raises(ValueError, match=msg):
        app.parse_config(ds, cfg)


def test_evaluate_score_mode(ds):
    out = app.evaluate(ds, {"zones": "boroughs", "features": {"population": {"enabled": True, "weight": 1}}})
    assert (out["zones"], out["mode"], out["total_zones"], out["excluded"]) == ("boroughs", "score", 33, 0)
    top, bottom = out["top_10"], out["bottom_5"]
    assert len(top) == 10 and len(bottom) == 5
    assert top[0]["score_percentile"] == 100 and set(top[0]) == {"zone", "score_percentile", "population"}
    assert [r["population"] for r in top] == sorted((r["population"] for r in top), reverse=True)
    assert [r["population"] for r in bottom] == sorted(r["population"] for r in bottom)
    assert top[0]["population"] > bottom[-1]["population"]


def test_evaluate_score_mode_skips_excluded_zones(ds):
    out = app.evaluate(ds, {"zones": "msoa", "features": {"population": {"enabled": True}},
                            "hard_exclusions": {"flood": {"enabled": True, "threshold": 0.05}}})
    excluded = {c["id"] for c, m in zip(ds.levels["msoa"]["cells"],
                                        app.excluded_mask(ds, req(exclusions={"flood": 0.05}))) if m}
    assert out["excluded"] == len(excluded) > 0
    assert not excluded & {r["zone"] for r in out["top_10"] + out["bottom_5"]}


def test_evaluate_cluster_mode(ds):
    out = app.evaluate(ds, {"zones": "msoa", "mode": "cluster", "n_clusters": 3,
                            "features": {"population": {"enabled": True}, "cbd_distance_m": {"enabled": True}}})
    assert out["mode"] == "cluster" and len(out["cluster_sizes"]) == 3
    assert sum(out["cluster_sizes"]) == out["total_zones"] - out["excluded"]
    assert out["cluster_sizes"] == sorted(out["cluster_sizes"], reverse=True)
    assert [set(c) for c in out["cluster_centre_percentiles"]] == [{"population", "cbd_distance_m"}] * 3


def test_evaluate_cluster_mode_with_every_zone_excluded():
    ds = tiny([[0.1], [0.9]], flood=[1, 1])
    out = app.evaluate(ds, {"mode": "cluster", "features": {"f0": {"enabled": True}},
                            "hard_exclusions": {"flood": {"enabled": True}}})
    assert out["excluded"] == out["total_zones"] == 2 and out["cluster_sizes"] == [] and out["cluster_centre_percentiles"] == []


def test_evaluate_refuses_an_invalid_config(ds):
    with pytest.raises(ValueError):
        app.evaluate(ds, {"features": {"nope": {"enabled": True}}})


# --- Endpoints not covered in test_app.py ------------------------------------------------------------------------

def test_index_serves_the_front_end(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.text == (app.HERE / "index.html").read_text()


def test_health_reports_the_image(client, monkeypatch):
    monkeypatch.setenv("IMAGE_TAG", "abc123")
    assert client.get("/health").json() == {"status": "ok", "image": "abc123",
                                            "data_version": app.DATA.version}
    monkeypatch.delenv("IMAGE_TAG")
    assert client.get("/health").json()["image"] == "dev"


def test_meta_defaults_to_the_base_level(client):
    m = client.get("/meta").json()
    assert len(m["cells"]) == len(app.DATA.levels["h3_8"]["norm"])
    assert [e["key"] for e in m["exclusions"]] == list(app.EXCLUSIONS) == list(m["cov"])
    pop = next(f for f in m["features"] if f["key"] == "population")
    assert pop == {"key": "population", "label": "Population", "group": "Core", "default": 0.6, "agg": "sum"}
    assert all(len(v) == len(m["cells"]) for v in m["cov"].values())


def test_cluster_endpoint_rejects_bad_input(client):
    assert client.post("/cluster", json={"weights": {}, "level": "nope"}).status_code == 422
    assert client.post("/cluster", json={"weights": "x"}).status_code == 422


def test_missing_config_is_404(client, state_dir):
    assert client.get("/configs/local/nope.yaml").status_code == 404
    assert client.get("/configs/local/..%2Fx").status_code in (403, 404)


def test_malformed_user_id_is_401(client):
    assert client.get("/configs", headers={"x-ms-client-principal-id": "bad id!"}).status_code == 401


def test_chat_streams_agent_events_with_tools_bound_to_the_user(client, state_dir, monkeypatch):
    seen = {}

    async def fake_chat(message, current_config, session_id, tools):
        seen.update(message=message, config=current_config, session=session_id)
        tools.claim_conversation("conv_new")
        seen["saved"] = tools.save_config("zones: msoa\n")
        seen["described"] = tools.describe("boroughs")["described_level"]
        seen["evaluated"] = tools.evaluate({"zones": "boroughs"})["total_zones"]
        yield {"type": "text", "text": "Hello"}
        yield {"type": "done", "session_id": "conv_new", "tokens": 3, "error": False}
    monkeypatch.setattr(app.agent, "chat", fake_chat)
    alice = principal("alice-oid")
    r = client.post("/chat", headers=alice, json={"message": "hi", "config_yaml": "zones: msoa\n"})
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert [json.loads(line) for line in r.text.splitlines()] == [
        {"type": "text", "text": "Hello"}, {"type": "done", "session_id": "conv_new", "tokens": 3, "error": False}]
    assert seen | {"saved": None} == {"message": "hi", "config": "zones: msoa\n", "session": None, "saved": None,
                                      "described": "boroughs", "evaluated": 33}
    assert (state_dir / "configs" / "alice-oid" / seen["saved"]).exists()
    # alice started conv_new, so she may continue it
    r = client.post("/chat", headers=alice, json={"message": "more", "session_id": "conv_new"})
    assert seen["session"] == "conv_new" and json.loads(r.text.splitlines()[0])["type"] == "text"


def test_chat_refuses_an_unknown_conversation(client, state_dir, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("the agent must not run")
        yield
    monkeypatch.setattr(app.agent, "chat", never)
    r = client.post("/chat", json={"message": "hi", "session_id": "conv_forgotten"})
    assert json.loads(r.text) == {"type": "error", "text": "Unknown conversation: start a new one"}


# --- Missing data: kept as gaps, never filled ----------------------------------------------------------------------

NAN = float("nan")


def test_nullable():
    assert app.nullable([1.234, NAN], 1) == [1.2, None]
    assert app.nullable([[NAN, 2.0]], 0) == [[None, 2.0]]
    out = app.nullable(np.array([0.12345]), 3)
    assert out == [0.123] and type(out[0]) is float
    json.dumps(app.nullable([NAN, 1], 1), allow_nan=False)


@pytest.fixture(scope="module")
def gappy(tmp_path_factory):
    """data/levels' msoa and boroughs tables with some values blanked out, as a release folder and manifest.
    Boroughs 0-2 miss cbd_distance_m, borough 3 misses population and borough 4 its flood coverage; MSOA 0
    misses cbd_distance_m."""
    folder = tmp_path_factory.mktemp("gappy")
    (folder / "levels").mkdir()
    for key, blanks in {"boroughs": {"cbd_distance_m": [0, 1, 2], "population": [3], "cov_flood": [4]},
                        "msoa": {"cbd_distance_m": [0]}}.items():
        g = gpd.read_parquet(LEVELS / f"{key}.parquet")
        for col, rows in blanks.items():
            g.loc[rows, col] = np.nan
        g.to_parquet(folder / "levels" / f"{key}.parquet")
    return folder, release.build_manifest(folder / "levels", "gappy")


@pytest.fixture(scope="module")
def gds(gappy):
    return app.Dataset(*gappy)


def test_gaps_load_as_none_and_nan(gds):
    L, i = gds.levels["boroughs"], gds.idx["cbd_distance_m"]
    assert [row[i] for row in L["raw"][:4]] == [None, None, None, L["raw"][3][i]] and L["raw"][3][i] is not None
    assert np.isnan(L["norm"][:3, i]).all() and not np.isnan(L["norm"][3:, i]).any()
    assert np.nanmax(L["norm"][:, i]) == 1.0 and np.nanmin(L["norm"][:, i]) == pytest.approx(1 / 30)  # among 30
    assert np.isnan(L["cov"]["flood"][4])


def test_zones_missing_a_weighted_feature_get_no_score(gds):
    r = app.score_zones(gds, req({"cbd_distance_m": 1, "population": 0.5}, level="boroughs"))
    assert r["missing"] == [0, 1, 2, 3] and r["excluded"] == 0
    assert r["pct"][:4] == [None] * 4 and None not in r["pct"][4:]
    scored = [p for p in r["pct"] if p is not None]
    assert max(scored) == 1.0 and min(scored) == pytest.approx(1 / 29, abs=1e-4)  # ranked among the 29 scored


def test_a_feature_with_zero_weight_needs_no_value(gds):
    r = app.score_zones(gds, req({"cbd_distance_m": 0, "station_count": 1}, level="boroughs"))
    assert r["missing"] == [] and None not in r["pct"]


def test_excluded_zones_are_not_reported_as_missing(gds):
    flood = gds.levels["boroughs"]["cov"]["flood"]
    r = app.score_zones(gds, req({"cbd_distance_m": 1}, level="boroughs", exclusions={"flood": 0.05}))
    excluded = {i for i, c in enumerate(flood) if c >= 0.05}
    assert r["excluded"] == len(excluded) > 0 and not set(r["missing"]) & excluded
    assert set(r["missing"]) == {0, 1, 2} - excluded
    assert 4 not in excluded and r["pct"][4] is not None  # unknown flood coverage doesn't exclude


def test_zones_missing_a_clustered_feature_are_left_out(gds):
    r = app.cluster_zones(gds, req({"cbd_distance_m": 1, "station_count": 1}, level="boroughs", n_clusters=3))
    assert r["missing"] == [0, 1, 2] and r["labels"][:3] == [None] * 3
    assert sorted(set(r["labels"][3:])) == [0, 1, 2] and all(not np.isnan(c).any() for c in r["centers"])
    one = app.cluster_zones(gds, req({"population": 1}, level="boroughs", n_clusters=1))
    assert one["missing"] == [3] and one["labels"][3] is None and one["centers"][0][0] == pytest.approx(
        np.nanmean(gds.levels["boroughs"]["norm"][:, gds.idx["population"]]) * 100, abs=0.05)


def test_describe_reports_gaps(gds):
    d = app.describe(gds, "boroughs")
    assert d["zone_levels"]["boroughs"]["zones_missing_data"] == 4  # the flood coverage gap isn't a feature
    assert d["zone_levels"]["msoa"]["zones_missing_data"] == 1
    cbd = next(f for f in d["features"] if f["key"] == "cbd_distance_m")
    assert cbd["share_of_zones_missing"] == round(3 / 33, 2) and None not in cbd["min_p25_median_p75_max"]
    assert next(f for f in d["features"] if f["key"] == "poi_lodging")["share_of_zones_missing"] == 0
    json.dumps(d, allow_nan=False)


def test_describe_with_a_feature_missing_everywhere(gds):
    L = gds.levels["msoa"]
    i = gds.idx["station_count"]
    saved = (L["norm"].copy(), [row[i] for row in L["raw"]])
    try:
        L["norm"] = L["norm"].copy()
        L["norm"][:, i] = NAN
        for row in L["raw"]:
            row[i] = None
        d = app.describe(gds, "msoa")
    finally:
        L["norm"] = saved[0]
        for row, v in zip(L["raw"], saved[1]):
            row[i] = v
    sc = next(f for f in d["features"] if f["key"] == "station_count")
    assert sc["min_p25_median_p75_max"] is None and sc["share_of_zones_missing"] == 1
    assert all("station_count" not in (p["a"], p["b"]) for p in d["most_correlated_feature_pairs"])
    json.dumps(d, allow_nan=False)


def test_evaluate_reports_zones_left_out_for_missing_data(gds):
    cfg = {"zones": "boroughs", "features": {"cbd_distance_m": {"enabled": True, "weight": 1}}}
    out = app.evaluate(gds, cfg)
    assert out["missing_data"] == 3 and len(out["top_10"]) == 10
    gaps = {c["id"] for c in gds.levels["boroughs"]["cells"][:3]}
    assert gaps.isdisjoint(r["zone"] for r in out["top_10"] + out["bottom_5"])
    assert app.evaluate(gds, cfg | {"mode": "cluster"})["missing_data"] == 3


def test_the_api_sends_gaps_as_null(gappy, state_dir, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    store = LocalStore(tmp_path / "data")
    release.publish(store, gappy[0] / "levels", promote_it=True)
    monkeypatch.setenv("DATA_STORE_URL", str(store.root))
    monkeypatch.setattr(app, "CACHE", tmp_path / "cache")
    with TestClient(app.app) as c:
        m = json.loads(c.get("/meta?level=boroughs").text)
        cols = [f["key"] for f in m["features"]]
        assert [row[cols.index("cbd_distance_m")] for row in m["raw"][:3]] == [None] * 3
        assert m["cov"]["flood"][4] is None
        s = c.post("/score", json={"weights": {"cbd_distance_m": 1}, "level": "boroughs"}).json()
        assert s["missing"] == [0, 1, 2] and s["pct"][:3] == [None] * 3
        k = c.post("/cluster", json={"weights": {"population": 1}, "level": "boroughs", "n_clusters": 2}).json()
        assert k["missing"] == [3] and k["labels"][3] is None
