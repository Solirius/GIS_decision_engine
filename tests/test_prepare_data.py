"""Building the zone tables (prepare_data.py): downloads with retries and paging, the area maths, and a full build
from the shipped base data with the planning layers and MSOA boundaries replaced by small fakes."""
import io
import json
from urllib.parse import parse_qs, urlparse

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely

import app
import prepare_data as pdata
from conftest import LEVELS

BNG = pdata.BNG


# --- Downloads ---------------------------------------------------------------------------------------------------

def test_get_json_retries_with_growing_waits(monkeypatch):
    sleeps, attempts = [], []
    monkeypatch.setattr(pdata.time, "sleep", sleeps.append)

    def urlopen(url, timeout):
        attempts.append(url)
        if len(attempts) < 3:
            raise TimeoutError("slow server")
        return io.BytesIO(b'{"ok": true}')
    monkeypatch.setattr(pdata, "urlopen", urlopen)
    assert pdata.get_json("https://x") == {"ok": True}
    assert sleeps == [5, 10] and len(attempts) == 3


def test_get_json_gives_up_with_the_last_error(monkeypatch):
    sleeps = []
    monkeypatch.setattr(pdata.time, "sleep", sleeps.append)
    monkeypatch.setattr(pdata, "urlopen", lambda url, timeout: (_ for _ in ()).throw(ConnectionError(url)))
    with pytest.raises(ConnectionError, match="https://x"):
        pdata.get_json("https://x", retries=3)
    assert sleeps == [5, 10]  # no wait after the last attempt


def test_fetch_planning_tile_follows_pages(monkeypatch):
    queries = []

    def get_json(url):
        q = parse_qs(urlparse(url).query)
        queries.append(q)
        offset = int(q["offset"][0])
        n = 500 if offset < 1000 else 3
        return {"features": [{"id": offset + i} for i in range(n)]}
    monkeypatch.setattr(pdata, "get_json", get_json)
    features = pdata.fetch_planning_tile("flood-risk-zone", (0.1, 51.0, 0.2, 51.1))
    assert [f["id"] for f in features] == list(range(1003))
    assert [q["offset"] for q in queries] == [["0"], ["500"], ["1000"]]
    assert queries[0]["dataset"] == ["flood-risk-zone"] and queries[0]["geometry_relation"] == ["intersects"]
    assert queries[0]["geometry"] == ["POLYGON((0.1 51.0,0.2 51.0,0.2 51.1,0.1 51.1,0.1 51.0))"]


def feature(entity, geom, **props):
    return {"type": "Feature", "geometry": shapely.geometry.mapping(geom), "properties": {"entity": entity, **props}}


def test_planning_layer_downloads_dedupes_repairs_clips_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(pdata, "RAW", tmp_path)
    london = gpd.GeoDataFrame(geometry=[shapely.box(0, 51, 0.25, 51.1)], crs=4326)
    bowtie = shapely.Polygon([(0.01, 51.01), (0.05, 51.05), (0.05, 51.01), (0.01, 51.05)])  # self-intersecting
    tiles = []

    def fetch_tile(dataset, bounds):
        tiles.append(bounds)
        return [feature(1, shapely.box(0.2, 51.02, 0.3, 51.03)),  # half outside London: clipped
                feature(2, bowtie), feature(3, shapely.box(1, 52, 1.1, 52.1))]  # outside: dropped
    monkeypatch.setattr(pdata, "fetch_planning_tile", fetch_tile)
    gdf = pdata.planning_layer("flood-risk-zone", london)
    assert len(tiles) == 3 * 2  # 0.25 deg wide in 0.1 steps, 0.1 high in 0.07 steps
    assert sorted(gdf.entity) == [1, 2]  # one copy per entity, whatever the number of tiles it appeared in
    assert gdf.geometry.is_valid.all() and gdf.total_bounds[2] <= 0.25 + 1e-9
    assert (tmp_path / "flood-risk-zone.parquet").exists()
    monkeypatch.setattr(pdata, "fetch_planning_tile", lambda *a: pytest.fail("should come from the cache"))
    assert sorted(pdata.planning_layer("flood-risk-zone", london).entity) == [1, 2]


def test_london_msoas_pages_filters_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(pdata, "RAW", tmp_path)
    box = shapely.box(0, 51, 0.01, 51.01)
    pages = [[feature(None, box, MSOA21CD="E1", MSOA21NM="Camden 001"),
              feature(None, box, MSOA21CD="E2", MSOA21NM="Epping Forest 003")],
             [feature(None, box, MSOA21CD="E1", MSOA21NM="Camden 001"),  # repeated across pages
              feature(None, box, MSOA21CD="E3", MSOA21NM="City of London 001")], []]
    offsets = []

    def get_json(url):
        offsets.append(parse_qs(urlparse(url).query)["resultOffset"][0])
        return {"features": pages[len(offsets) - 1]}
    monkeypatch.setattr(pdata, "get_json", get_json)
    gdf = pdata.london_msoas({"Camden", "City of London"})
    assert offsets == ["0", "2", "4"]
    assert list(gdf.MSOA21CD) == ["E1", "E3"]
    monkeypatch.setattr(pdata, "get_json", lambda url: pytest.fail("should come from the cache"))
    assert list(pdata.london_msoas(set()).MSOA21CD) == ["E1", "E3"]


def test_cbd_distance_matches_the_base_dataset():
    base = gpd.read_parquet(pdata.DATA / "london_physical_features.geoparquet")
    d = pdata.cbd_distance(base.h3_id)
    known = base.cbd_distance_m.notna().to_numpy()
    assert np.abs(d[known] - base.cbd_distance_m.to_numpy()[known]).max() < 0.1  # metres
    assert d[base.h3_id.to_numpy() == pdata.CBD_CELL].tolist() == [0]
    assert not np.isnan(d).any() and len(d) == len(base)


def test_cbd_distance_of_known_places():
    import h3
    one_degree_north = h3.latlng_to_cell(h3.cell_to_latlng(pdata.CBD_CELL)[0] + 1, h3.cell_to_latlng(pdata.CBD_CELL)[1], 8)
    assert pdata.cbd_distance([one_degree_north])[0] == pytest.approx(111_195, rel=0.005)  # ~111 km per degree
    assert pdata.cbd_distance([]).shape == (0,)


# --- Area maths (all in BNG metres) ------------------------------------------------------------------------------

def test_chop_tiles_the_union():
    geoms = np.array([shapely.box(0, 0, 1500, 1000), shapely.box(1000, 0, 2500, 1000)])  # overlapping
    parts = pdata.chop(geoms)
    assert parts.crs.to_epsg() == BNG
    assert parts.area.sum() == pytest.approx(2500 * 1000)  # overlap counted once
    assert len(parts) == 3 and (parts.area <= 1000 * 1000 + 1e-6).all()
    assert shapely.union_all(parts.values).equals(shapely.box(0, 0, 2500, 1000))
    assert len(pdata.chop(geoms, size=500)) == 10


def test_chop_of_nothing_is_empty():
    assert len(pdata.chop(np.array([], dtype=object))) == 0
    assert len(pdata.chop(np.array([shapely.Polygon()]))) == 0


def test_coverage():
    zones = gpd.GeoSeries([shapely.box(0, 0, 100, 100), shapely.box(100, 0, 200, 100), shapely.box(500, 500, 600, 600)],
                          crs=BNG)
    parts = pdata.chop(np.array([shapely.box(50, 0, 200, 100)]))
    assert pdata.coverage(zones, parts) == pytest.approx([0.5, 1.0, 0.0])
    assert list(pdata.coverage(zones, gpd.GeoSeries([], crs=BNG))) == [0, 0, 0]


def test_area_weighted_splits_counts_and_averages_the_rest():
    cells = gpd.GeoSeries([shapely.box(0, 0, 100, 100), shapely.box(100, 0, 200, 100)], crs=BNG)
    zones = gpd.GeoSeries([shapely.box(0, 0, 150, 100), shapely.box(150, 0, 200, 100), shapely.box(900, 900, 950, 950)],
                          crs=BNG)
    values = np.array([[100.0, 4.0, 10.0], [40.0, 8.0, 20.0]])
    with np.errstate(invalid="ignore"):
        df = pdata.area_weighted(zones, cells, values, ["population", "poi_shops", "cbd_distance_m"])
    assert list(df.columns) == ["population", "poi_shops", "cbd_distance_m"]
    assert df.population[:2].tolist() == pytest.approx([100 + 20, 20])  # whole first cell + half the second
    assert df.poi_shops[:2].tolist() == pytest.approx([4 + 4, 4])
    assert df.cbd_distance_m[:2].tolist() == pytest.approx([(10 * 100 + 20 * 50) / 150, 20])
    assert df.population[2] == 0 and np.isnan(df.cbd_distance_m[2])  # a zone with no cells: no mean


def test_area_weighted_keeps_gaps():
    cells = gpd.GeoSeries([shapely.box(0, 0, 100, 100), shapely.box(100, 0, 200, 100)], crs=BNG)
    zones = gpd.GeoSeries([shapely.box(0, 0, 150, 100), shapely.box(0, 0, 100, 100)], crs=BNG)
    values = np.array([[100.0, 10.0], [np.nan, np.nan]])
    df = pdata.area_weighted(zones, cells, values, ["population", "cbd_distance_m"])
    assert np.isnan(df.population[0]) and np.isnan(df.cbd_distance_m[0])  # touches the cell with a gap
    assert (df.population[1], df.cbd_distance_m[1]) == (100, 10)  # doesn't


# --- Full build ----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Run main() on the shipped base data, with one small flood zone, habitat, monument, Green Belt patch and
    brownfield site, and the boroughs standing in for the MSOAs."""
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("prepare")
    mp.setattr(pdata, "RAW", tmp / "raw")
    mp.setattr(pdata, "LEVELS", tmp / "levels")
    boroughs = gpd.read_file(pdata.DATA / "london_boroughs.geojson")
    camden = boroughs[boroughs.LAD23NM == "Camden"].geometry.iloc[0]
    patch = shapely.box(*shapely.buffer(camden.centroid, 0.01).bounds).intersection(camden)  # ~2 km, in Camden only
    site = camden.centroid
    layers = {"flood-risk-zone": [patch], "green-belt": [patch], "brownfield-land": [site]}
    requested = []

    def planning_layer(dataset, london):
        requested.append(dataset)
        geoms = layers.get(dataset, [shapely.box(*shapely.buffer(camden.centroid, 0.001).bounds)])
        gdf = gpd.GeoDataFrame({"entity": range(len(geoms))}, geometry=geoms, crs=4326)
        if dataset == "brownfield-land":
            gdf["hectares"] = ["2.5"]
        return gdf
    msoa = gpd.GeoDataFrame({"MSOA21CD": boroughs.LAD23CD, "MSOA21NM": boroughs.LAD23NM + " 001"},
                            geometry=boroughs.geometry, crs=4326)
    mp.setattr(pdata, "planning_layer", planning_layer)
    mp.setattr(pdata, "london_msoas", lambda names: msoa)
    with np.errstate(invalid="ignore"):
        pdata.main()
    mp.undo()
    return tmp / "levels", requested


def test_build_writes_every_level_with_the_shipped_columns(built):
    folder, requested = built
    assert sorted(requested) == sorted([d for ds in pdata.EXCLUSIONS.values() for d in ds] + ["green-belt",
                                                                                              "brownfield-land"])
    assert sorted(p.name for p in folder.iterdir()) == sorted(p.name for p in LEVELS.iterdir())
    for p in folder.iterdir():
        assert list(pd.read_parquet(p).columns) == list(pd.read_parquet(LEVELS / p.name).columns)


def test_build_levels_load_in_the_app(built):
    folder, _ = built
    feats = {}
    for key in ("h3_8", "h3_7", "msoa", "boroughs"):
        feats[key], L = app.load_level(folder / f"{key}.parquet")
        assert len(L["cells"]) > 0
    assert len({tuple(f) for f in feats.values()}) == 1


def test_build_keeps_totals_and_places_layers(built):
    folder, _ = built
    t = {k: gpd.read_parquet(folder / f"{k}.parquet") for k in ("h3_8", "h3_7", "msoa", "boroughs")}
    assert len(t["boroughs"]) == len(t["msoa"]) == 33
    assert t["msoa"].zone_id.str.endswith(" 001").all()
    total = t["h3_8"].population.sum()
    assert t["h3_7"].population.sum() == pytest.approx(total, rel=0.02)
    assert t["boroughs"].population.sum() == pytest.approx(total, rel=0.05)
    b = t["boroughs"].set_index("zone_id")
    assert b.loc["Camden", "brownfield_ha"] == 2.5 and b.brownfield_ha.sum() == 2.5
    assert b.loc["Camden", "cov_brownfield"] == pytest.approx(2.5e4 / gpd.GeoSeries(
        [b.loc["Camden", "geometry"]], crs=4326).to_crs(BNG).area.iloc[0], rel=0.01)
    for col in ("cov_flood", "cov_habitats", "cov_monuments", "green_belt_pct"):
        assert b[col].idxmax() == "Camden" and (b.drop("Camden")[col] < 1e-4).all()  # slivers where outlines meet
    assert 0 < b.loc["Camden", "cov_flood"] < 1
    assert b.loc["Camden", "green_belt_pct"] == pytest.approx(b.loc["Camden", "cov_flood"] * 100)
    h3 = t["h3_8"]
    assert h3.cov_flood.max() > 0.95 and (h3.cov_flood > 0).sum() > 5  # cells (nearly) inside the patch
    base = gpd.read_parquet(pdata.DATA / "london_physical_features.geoparquet")
    assert base.cbd_distance_m.isna().any()  # the base dataset has gaps...
    for k in t:
        assert not t[k].cbd_distance_m.isna().any()  # ...but the distance is recomputed, so no level has any
