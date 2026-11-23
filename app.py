"""Homes England investment decision engine — PoC backend.

Run: uv run app.py  ->  http://localhost:8000

The zone tables come from the live data release in DATA_STORE_URL (see release.py), downloaded once into a local
cache. A background task checks for a newly promoted release every RELEASE_POLL_SECONDS and swaps it in without a
restart. Without DATA_STORE_URL, the app serves the tables in data/levels.

Environment:
    DATA_STORE_URL        data release store (Blob container URL or folder); unset = data/levels
    STATE_STORE_URL       saved configs and conversation owners (default ./store/state)
    RELEASE_CACHE         local folder for downloaded releases (default ./store/cache)
    RELEASE_POLL_SECONDS  how often to check for a new live release (default 300)
    REQUIRE_SIGN_IN       "true" to refuse requests without a signed-in user (set in prod)
    IMAGE_TAG             reported by /health so the pipeline can tell revisions apart
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import shapely
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sklearn.cluster import KMeans

import agent
import release
import state
from store import data_store

HERE = Path(__file__).parent
CACHE = Path(os.environ.get("RELEASE_CACHE") or HERE / "store" / "cache")
POLL_SECONDS = float(os.environ.get("RELEASE_POLL_SECONDS", 300))
REQUIRE_SIGN_IN = os.environ.get("REQUIRE_SIGN_IN", "").lower() == "true"
log = logging.getLogger("uvicorn.error")

EXCLUSIONS = {"flood": "Flood zones 2 & 3", "habitats": "Protected habitats", "monuments": "Scheduled monuments",
              "brownfield": "Brownfield land"}


def ring(coords):
    """Convert a polygon ring to a JSON-friendly list of points rounded to 5 decimals.

    Five decimal places of a degree is about 1 m, which is ample for display and keeps the /meta payload small.

    Args:
        coords: Iterable of (x, y) coordinate pairs, e.g. a shapely exterior's ``coords``.

    Returns:
        List of ``[x, y]`` lists with each coordinate rounded to 5 decimal places.
    """
    return [[round(x, 5), round(y, 5)] for x, y in coords]


def nullable(values, decimals):
    """Round an array for JSON, with None where a value is missing (NaN), since JSON has no NaN.

    Args:
        values: Float array of any shape.
        decimals: Decimal places to round to.

    Returns:
        Nested lists of the same shape: Python floats, or None where ``values`` is NaN.
    """
    a = np.round(np.asarray(values, dtype=np.float64), decimals)
    return np.where(np.isnan(a), None, a).tolist()


def load_level(path):
    """Load one zone level's feature table (built by prepare_data.py) into memory.

    Missing values are kept as they are, never filled: the data owner decides what the tables hold. A zone
    missing a feature has no percentile for it, and percentiles are ranked among the zones that have a value.

    Args:
        path: The level's GeoParquet file.

    Returns:
        A ``(features, level)`` tuple. ``features`` lists the feature column names. ``level`` is a dict with:
        ``cells`` (zone id and polygon rings per zone, for the map), ``raw`` (feature values rounded to 1
        decimal, one row per zone, None where missing), ``norm`` (percentile rank of each feature in [0, 1], as
        a float array of shape (zones, features), NaN where missing) and ``cov`` (exclusion layer key -> share of
        each zone it covers).
    """
    t = duckdb.connect().sql("select * exclude (geometry), geometry::BLOB as wkb from read_parquet($path)",
                             params={"path": str(path)}).to_arrow_table()
    geoms = shapely.from_wkb(t.column("wkb").to_numpy(zero_copy_only=False))
    g = t.drop_columns(["wkb"]).to_pandas()
    feats = [c for c in g.columns if c != "zone_id" and not c.startswith("cov_")]
    return feats, {
        # A zone can have several polygons (e.g. boroughs split by the Thames).
        "cells": [{"id": z, "polys": [ring(p.exterior.coords) for p in shapely.get_parts(geom)]}
                  for z, geom in zip(g.zone_id, geoms)],
        "raw": nullable(g[feats].to_numpy(dtype=np.float64), 1),
        # Percentile-rank each feature to [0, 1] so weights are comparable despite skew/units.
        "norm": g[feats].rank(pct=True).to_numpy(dtype=np.float64),
        "cov": {c[4:]: g[c].to_numpy() for c in g.columns if c.startswith("cov_")},
    }


# How base cells were combined into larger zones (area-weighted, see prepare_data.py).
SUMMED = {"population", "station_count", "brownfield_ha"}
LABELS = {"cbd_distance_m": "CBD distance (m)", "green_belt_pct": "Green Belt coverage (%)",
          "brownfield_ha": "Brownfield land (ha)"}


class Dataset:
    """One data release, loaded into memory: every zone level with its features, percentiles and coverages."""

    def __init__(self, folder, manifest):
        """Load every level listed in a release's manifest.

        Args:
            folder: Local folder the manifest's ``file`` paths are relative to.
            manifest: The release manifest (see release.py).

        Raises:
            ValueError: If the levels don't all have the same features.
        """
        self.version = manifest["version"]
        self.base_level = manifest["base_level"]
        self.labels = {lv["key"]: lv["label"] for lv in manifest["levels"]}
        self.levels = {}
        features = None
        for lv in manifest["levels"]:
            feats, self.levels[lv["key"]] = load_level(Path(folder) / lv["file"])
            if features is not None and feats != features:
                raise ValueError(f"Level {lv['key']} has different features from the others")
            features = feats
        self.features = features
        self.idx = {c: i for i, c in enumerate(features)}
        self.agg = {c: "sum" if c in SUMMED or c.startswith("poi_") else "mean" for c in features}

    def level(self, key):
        """A level's data, ``key`` defaulting to the base level. Raises HTTP 422 for an unknown level."""
        key = key or self.base_level
        if key not in self.levels:
            raise HTTPException(422, f"Unknown zones {key!r}; use one of {list(self.levels)}")
        return self.levels[key]


DATA = None  # the Dataset being served; replaced as a whole when a new release goes live


def load_live():
    """Load the live release from the data store, or data/levels when no store is set or nothing is live yet."""
    store = data_store()
    folder, manifest = release.fetch(store, CACHE) if store else (None, None)
    if folder is None:
        if store:
            log.warning("No live release in %s yet: serving data/levels", store.url)
        folder, manifest = release.local()
    return Dataset(folder, manifest)


async def watch_releases():
    """Every ``POLL_SECONDS``, swap in the live release if it has changed. Errors are logged and retried."""
    global DATA
    store = data_store()
    while store:
        await asyncio.sleep(POLL_SECONDS)
        try:
            version = await asyncio.to_thread(release.current_version, store)
            if version and version != DATA.version:
                log.info("Loading data release %s", version)
                DATA = await asyncio.to_thread(lambda: Dataset(*release.fetch(store, CACHE, version)))
                log.info("Now serving data release %s", version)
        except Exception:
            log.exception("Checking for a new data release failed; will retry")


@asynccontextmanager
async def lifespan(_):
    """Load the live release before serving, then watch for new ones."""
    global DATA
    DATA = await asyncio.to_thread(load_live)
    log.info("Serving data release %s", DATA.version)
    watcher = asyncio.create_task(watch_releases())
    yield
    watcher.cancel()


def group(c):
    """Sidebar group a feature is listed under.

    Args:
        c: Feature column name.

    Returns:
        ``"Amenities (POI count)"`` for ``poi_*`` features, ``"Policy"`` for Green Belt and brownfield,
        otherwise ``"Core"``.
    """
    if c.startswith("poi_"):
        return "Amenities (POI count)"
    if c in ("green_belt_pct", "brownfield_ha"):
        return "Policy"
    return "Core"


def label(c):
    """Human-readable label for a feature.

    Args:
        c: Feature column name.

    Returns:
        The label from ``LABELS`` if there is one, otherwise the column name without its ``poi_`` prefix and
        with underscores as spaces, in sentence case (e.g. ``"poi_health_care"`` -> ``"Health care"``).
    """
    s =LABELS.get(c) or c.removeprefix("poi_").replace("_", " ")
    return s[0].upper() + s[1:]


DEFAULTS = {"population": 0.6, "cbd_distance_m": -0.4, "brownfield_ha": 0.5, "poi_health_care": 0.3}

app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def sign_in_gate(request: Request, call_next):
    """With REQUIRE_SIGN_IN, refuse every request except /health that has no signed-in user.

    Container Apps sign-in already blocks anonymous requests; this is a second lock in case it is ever switched off.
    """
    if REQUIRE_SIGN_IN and request.url.path != "/health" and not request.headers.get("x-ms-client-principal-id"):
        return JSONResponse({"detail": "Sign-in required"}, status_code=401)
    return await call_next(request)


def current_user(request: Request):
    """The signed-in user, from the headers Container Apps sign-in adds (``local`` without sign-in)."""
    try:
        return state.user_from_headers(request.headers, REQUIRE_SIGN_IN)
    except PermissionError as e:
        raise HTTPException(401, str(e)) from None


@app.get("/")
def index():
    """Serve the single-page front end.

    Returns:
        index.html as a file response.
    """
    return FileResponse(HERE / "index.html")


@app.get("/health")
def health():
    """Liveness and version check for the deploy pipeline: which image and data release this copy serves."""
    return {"status": "ok", "image": os.environ.get("IMAGE_TAG", "dev"), "data_version": DATA.version}


@app.get("/meta")
def meta(level: str | None = None):
    """Everything the front end needs to draw a zone level and build its sidebar.

    Args:
        level: Zone level key (defaults to the release's base level).

    Returns:
        Dict with the data ``version``, the available ``levels``, the hard ``exclusions``, the ``features`` (key,
        label, group, default weight and how base cells were aggregated), the zone ``cells`` (polygons), the
        ``raw`` feature values per zone (None where missing) and each exclusion layer's ``cov`` share per zone
        (for the tooltip).

    Raises:
        HTTPException: 422 if ``level`` is not a known zone level.
    """
    ds = DATA
    L = ds.level(level)
    return {
        "version": ds.version,
        "levels": [{"key": k, "label": v} for k, v in ds.labels.items()],
        "exclusions": [{"key": k, "label": v} for k, v in EXCLUSIONS.items()],
        "features": [{"key": c, "label": label(c), "group": group(c), "default": DEFAULTS.get(c), "agg": ds.agg[c]}
                     for c in ds.features],
        "cells": L["cells"],
        "raw": L["raw"],
        "cov": {g: nullable(L["cov"][g], 3) for g in EXCLUSIONS},  # for the tooltip
    }


class Weights(BaseModel):
    weights: dict[str, float]
    level: str | None = None           # default: the release's base level
    exclusions: dict[str, float] = {}  # enabled exclusion layer -> coverage threshold


def excluded_mask(ds, body):
    """Zones where any enabled exclusion layer covers at least its own threshold.

    Args:
        ds: Dataset to use.
        body: Request with ``level`` and ``exclusions`` (exclusion layer key -> coverage threshold in [0, 1]).
            Unknown layer keys are ignored.

    Returns:
        Boolean array with one entry per zone of the level, True where the zone is excluded.
    """
    L = ds.level(body.level)
    mask = np.zeros(len(L["norm"]), dtype=bool)
    for g, t in body.exclusions.items():
        if g in EXCLUSIONS:
            mask |= L["cov"][g] >= t - 1e-6  # tolerance so fully covered zones count at 100%
    return mask


def missing_mask(L, cols):
    """Zones missing a value for any of the given features.

    Args:
        L: A level's data (see ``load_level``).
        cols: Feature column indices.

    Returns:
        Boolean array with one entry per zone, True where any of ``cols`` is missing (all False if ``cols`` is
        empty).
    """
    return np.isnan(L["norm"][:, cols]).any(axis=1)


def with_gaps(values, mask):
    """Full-length list with None for zones left out (excluded, or missing data).

    Args:
        values: One value per zone kept, in zone order.
        mask: Boolean mask, one entry per zone, True for zones left out.

    Returns:
        List as long as ``mask``: the values in the positions of kept zones, None elsewhere.
    """
    out = [None] * len(mask)
    for i, v in zip(np.flatnonzero(~mask), values):
        out[i] = v
    return out


def score_zones(ds, body):
    """Score every zone as a weighted sum of feature percentiles.

    The score of a zone is the sum of weight x feature percentile over the weighted features. A zone missing
    a value for any feature with a non-zero weight has no score: it is left out, not guessed. Scores are then
    turned into percentiles among the zones that are scored.

    Args:
        ds: Dataset to use.
        body: Feature weights in [-1, 1] (unknown keys are ignored), zone level and enabled exclusions.

    Returns:
        Dict with ``pct``, the score percentile of each zone rounded to 4 decimals (None for zones excluded or
        missing data, 0.5 for every other zone when every weight is zero), ``excluded``, the number of excluded
        zones, ``missing``, the indices of the zones left unscored for missing data (excluded zones aren't
        listed), and the data ``version``.
    """
    w = np.zeros(len(ds.features))
    for k, v in body.weights.items():
        if k in ds.idx:
            w[ds.idx[k]] = v
    L = ds.level(body.level)
    excluded = excluded_mask(ds, body)
    missing = ~excluded & missing_mask(L, np.flatnonzero(w))
    left_out = excluded | missing
    norm = L["norm"][~left_out][:, w != 0]
    pct = pd.Series(norm @ w[w != 0]).rank(pct=True).to_numpy() if w.any() else np.full(len(norm), 0.5)
    return {"pct": with_gaps(np.round(pct, 4).tolist(), left_out), "excluded": int(excluded.sum()),
            "missing": np.flatnonzero(missing).tolist(), "version": ds.version}


@app.post("/score")
def score(body: Weights):
    """Score every zone of the live release; see ``score_zones``."""
    return score_zones(DATA, body)


class ClusterRequest(Weights):
    n_clusters: int = 5


def cluster_zones(ds, body):
    """Group the zones with k-means on the percentiles of the switched-on features.

    Weights are ignored: only which features are present in ``body.weights`` matters. The seed is fixed and
    clusters are numbered from largest to smallest, so the same inputs always give the same labels and colours.

    Args:
        ds: Dataset to use.
        body: Switched-on features (as ``weights`` keys), zone level, enabled exclusions and ``n_clusters``.
            The number of clusters is capped at the number of zones clustered.

    Returns:
        Dict with ``labels`` (cluster id per zone, None for zones excluded or missing a value for a switched-on
        feature), ``keys`` (the features used), ``centers`` (one row per cluster: centre percentile 0-100 of
        each feature in ``keys``), ``excluded`` (number of excluded zones), ``missing`` (indices of the zones left
        out for missing data; excluded zones aren't listed) and the data ``version``. With no features, a
        single cluster or no zones left, every zone clustered gets label 0.
    """
    # Vector per zone = percentiles of the features switched on (weights are ignored).
    keys = [k for k in body.weights if k in ds.idx]
    cols = [ds.idx[k] for k in keys]
    L = ds.level(body.level)
    excluded = excluded_mask(ds, body)
    missing = ~excluded & missing_mask(L, cols)
    mask = excluded | missing
    X = L["norm"][~mask][:, cols]
    k = max(1, min(body.n_clusters, len(X)))
    out = {"keys": keys, "excluded": int(excluded.sum()), "missing": np.flatnonzero(missing).tolist(),
           "version": ds.version}
    if not keys or k == 1 or len(X) == 0:
        centers = [np.round(X.mean(0) * 100, 1).tolist()] if len(X) else []
        return out | {"labels": with_gaps([0] * len(X), mask), "centers": centers}
    km = KMeans(n_clusters=k, random_state=0).fit(X)  # fixed seed: same inputs, same clusters
    # K-means label ids are arbitrary; renumber by cluster size so colours stay stable between runs.
    order = np.argsort(-np.bincount(km.labels_, minlength=k))
    return out | {
        "labels": with_gaps(np.argsort(order)[km.labels_].tolist(), mask),
        "centers": np.round(km.cluster_centers_[order] * 100, 1).tolist(),  # [cluster][feature], percentile 0-100
    }


@app.post("/cluster")
def cluster(body: ClusterRequest):
    """Cluster the zones of the live release; see ``cluster_zones``."""
    return cluster_zones(DATA, body)


# --- Chat agent (agent.py): tools to investigate the data and evaluate configs --------------

DESCRIPTIONS = {
    "population": "Resident population",
    "station_count": "Number of rail / Underground / Overground stations. DATA GAP: only populated for inner "
                     "London in the source data (0 in every outer borough), so it is unreliable outside inner London",
    "cbd_distance_m": "Distance to central London in metres (higher = further out)",
    "green_belt_pct": "Share of the zone designated Green Belt, in %",
    "brownfield_ha": "Brownfield land register sites, in hectares",
}
EXCLUSION_DESCRIPTIONS = {
    "flood": "Environment Agency Flood Zones 2 & 3 (river and sea flood risk)",
    "habitats": "SSSI, SAC, SPA and ancient woodland",
    "monuments": "Historic England scheduled monuments",
    "brownfield": "Brownfield land register sites (coverage estimated from site hectares / zone area)",
}


def describe(ds, level=None):
    """Describe the data at one zone level, for the chat agent's describe_data tool.

    Args:
        ds: Dataset to describe.
        level: Zone level key (default: the base level).

    Returns:
        Dict with the number of zones per level, and how many miss a value for at least one feature
        (``zone_levels``), the ``described_level``, each feature's label, group, aggregation, description,
        quartiles of the zones that have a value (None if none do), share of zones at zero and share missing a
        value (``features``), the 12 most correlated feature pairs by Spearman correlation over the zones that
        have both values (``most_correlated_feature_pairs``) and each exclusion layer's description and zone
        counts at 50% and full coverage (``hard_exclusions``).

    Raises:
        ValueError: If ``level`` is not a known zone level.
    """
    level = level or ds.base_level
    if level not in ds.levels:
        raise ValueError(f"Unknown zones {level!r}; use one of {list(ds.levels)}")
    L = ds.levels[level]
    feats, idx = ds.features, ds.idx
    raw = np.asarray(L["raw"], dtype=float)
    features = []
    for c in feats:
        col = raw[:, idx[c]]
        known = col[~np.isnan(col)]
        q = np.percentile(known, [0, 25, 50, 75, 100]).round(1).tolist() if len(known) else None
        features.append({"key": c, "label": label(c), "group": group(c), "aggregation": ds.agg[c],
                         "description": DESCRIPTIONS.get(c) or f"Count of {label(c).lower()} points of interest (Overture)",
                         "min_p25_median_p75_max": q, "share_of_zones_zero": round(float((col == 0).mean()), 2),
                         "share_of_zones_missing": round(float(np.isnan(col).mean()), 2)})
    corr = pd.DataFrame(L["norm"]).corr().to_numpy()  # Spearman, since norm holds ranks; pairwise over known values
    pairs = sorted(((abs(corr[i, j]), feats[i], feats[j], round(float(corr[i, j]), 2))
                    for i in range(len(feats)) for j in range(i + 1, len(feats)) if not np.isnan(corr[i, j])),
                   reverse=True)[:12]
    return {
        "zone_levels": {k: {"label": v, "zones": len(ds.levels[k]["norm"]),
                            "zones_missing_data": int(np.isnan(ds.levels[k]["norm"]).any(axis=1).sum())}
                        for k, v in ds.labels.items()},
        "described_level": level,
        "features": features,
        "most_correlated_feature_pairs": [{"a": a, "b": b, "spearman": r} for _, a, b, r in pairs],
        "hard_exclusions": [{"key": g, "label": EXCLUSIONS[g], "description": EXCLUSION_DESCRIPTIONS[g],
                             "zones_covered_at_least_50pct": int((L["cov"][g] >= 0.5).sum()),
                             "zones_fully_covered": int((L["cov"][g] >= 1 - 1e-6).sum())} for g in EXCLUSIONS],
    }


def parse_config(ds, cfg):
    """Config dict (export format) -> (request, mode). Raises ValueError on invalid content.

    Features and exclusions that are missing or not enabled are switched off. Numbers are clipped to their
    valid range: ``n_clusters`` to 1-10, weights to [-1, 1] and thresholds to [0.05, 1].

    Args:
        ds: Dataset the config must fit.
        cfg: Parsed config, as written by the front end's YAML export (keys ``zones``, ``mode``,
            ``n_clusters``, ``hard_exclusions`` and ``features``).

    Returns:
        A ``(request, mode)`` tuple: a ``ClusterRequest`` usable by both ``score_zones`` and ``cluster_zones``,
        and the mode, ``"score"`` or ``"cluster"``.

    Raises:
        ValueError: If the zone level or mode is unknown, or a feature or exclusion key doesn't exist.
    """
    level = cfg.get("zones", ds.base_level)
    if level not in ds.levels:
        raise ValueError(f"Unknown zones {level!r}; use one of {list(ds.levels)}")
    mode = cfg.get("mode", "score")
    if mode not in ("score", "cluster"):
        raise ValueError("mode must be 'score' or 'cluster'")
    feats, excl = cfg.get("features") or {}, cfg.get("hard_exclusions") or {}
    unknown = [k for k in feats if k not in ds.idx] + [k for k in excl if k not in EXCLUSIONS]
    if unknown:
        raise ValueError(f"Unknown keys: {unknown}")
    req = ClusterRequest(
        level=level,
        n_clusters=int(np.clip(cfg.get("n_clusters", 5), 1, 10)),
        weights={k: float(np.clip(v.get("weight", 0.5), -1, 1)) for k, v in feats.items() if v and v.get("enabled")},
        exclusions={k: float(np.clip(v.get("threshold", 1), 0.05, 1)) for k, v in excl.items() if v and v.get("enabled")},
    )
    return req, mode


def evaluate(ds, cfg):
    """Summary of what a config produces, for the agent to check its work.

    Args:
        ds: Dataset to evaluate against.
        cfg: Parsed config in the export format (see ``parse_config``).

    Returns:
        Dict with the ``zones`` level, ``mode``, ``total_zones``, ``excluded`` count and the number of zones left
        out because they miss a value for an enabled feature (``missing_data``). In cluster mode it also
        has ``cluster_sizes`` and ``cluster_centre_percentiles`` (feature -> centre percentile, per cluster). In
        score mode it has ``top_10`` and ``bottom_5``: zone id, score percentile and raw values of the enabled
        features.

    Raises:
        ValueError: If the config is invalid (see ``parse_config``).
    """
    req, mode = parse_config(ds, cfg)
    L = ds.level(req.level)
    ids = [c["id"] for c in L["cells"]]
    enabled = list(req.weights)
    out = {"zones": req.level, "mode": mode, "total_zones": len(ids)}
    if mode == "cluster":
        r = cluster_zones(ds, req)
        labels = [v for v in r["labels"] if v is not None]
        out |= {"excluded": r["excluded"], "missing_data": len(r["missing"]), "cluster_sizes": np.bincount(labels).tolist() if labels else [],
                "cluster_centre_percentiles": [dict(zip(r["keys"], c)) for c in r["centers"]]}
        return out
    r = score_zones(ds, req)
    ranked = sorted((p, i) for i, p in enumerate(r["pct"]) if p is not None)
    row = lambda p, i: {"zone": ids[i], "score_percentile": round(p * 100),
                        **{f: L["raw"][i][ds.idx[f]] for f in enabled}}
    out |= {"excluded": r["excluded"], "missing_data": len(r["missing"]), "top_10": [row(p, i) for p, i in ranked[::-1][:10]],
            "bottom_5": [row(p, i) for p, i in ranked[:5]]}
    return out


# --- Saved configs: every read goes through state.can_read -------------------------------------------------------

@app.get("/configs")
def configs(user: state.User = Depends(current_user)):
    """Saved configs the signed-in user may see (their own; all of them for an Admin), newest first."""
    return state.list_configs(user)


@app.get("/configs/{owner}/{name}", response_class=PlainTextResponse)
def config(owner: str, name: str, user: state.User = Depends(current_user)):
    """One saved config's YAML. 403 if it isn't the user's (and they aren't an Admin), 404 if it doesn't exist."""
    try:
        return state.read_config(user, owner, name)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except FileNotFoundError:
        raise HTTPException(404, "No such config") from None


class ChatRequest(BaseModel):
    message: str
    config_yaml: str = ""          # the user's current sidebar settings
    session_id: str | None = None  # continue a previous conversation


@app.post("/chat")
async def chat(body: ChatRequest, user: state.User = Depends(current_user)):
    """Streams agent events as newline-delimited JSON.

    A follow-up message must come from the user who started the conversation. The agent's tools see the data
    release that was live when the message arrived, even if a new one goes live mid-answer.

    Args:
        body: The user's message, their current sidebar config as YAML and, to continue a conversation, the
            session id returned by the previous turn.
        user: The signed-in user.

    Returns:
        A streaming ``application/x-ndjson`` response with one JSON event per line (see ``agent.chat``).
    """
    ds = DATA
    tools = agent.Tools(describe=lambda level: describe(ds, level),
                        evaluate=lambda cfg: evaluate(ds, cfg),
                        save_config=lambda text: state.save_config(user, text, "agent"),
                        claim_conversation=lambda cid: state.claim_conversation(user, cid))

    async def events():
        """Serialise each agent event as one line of JSON.

        Yields:
            Each event from ``agent.chat`` as a JSON string ending in a newline.
        """
        if body.session_id:
            try:
                await asyncio.to_thread(state.check_conversation, user, body.session_id)
            except PermissionError as e:
                yield json.dumps({"type": "error", "text": str(e)}) + "\n"
                return
        async for e in agent.chat(body.message, body.config_yaml, body.session_id, tools):
            yield json.dumps(e) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
