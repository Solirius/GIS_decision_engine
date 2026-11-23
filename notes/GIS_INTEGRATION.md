# Using the decision engine with ArcGIS or QGIS

How much work it takes to show the decision engine's results in ArcGIS or QGIS instead of, or as well as, the current web map. This is an assessment: none of it is built yet.

Written 23 November 2026, against the code on `feat--platform-foundations`.

## Short answer

Fairly easy, because the analysis and the map are already separate. All the analysis (hard exclusions, scoring, k-means clustering) runs in Python behind three JSON endpoints in `app.py`: `/meta`, `/score` and `/cluster`. The map itself is a single deck.gl `PolygonLayer` of about 30 lines in `index.html` (the `DeckGL` setup and `render()`).

## What ties the API to the current map

- **Geometry format.** `/meta` sends zone polygons as custom JSON (`{id, polys: [[x, y], …]}`, WGS84, rounded to 5 decimal places; see `load_level` in `app.py`), not GeoJSON.
- **Results are matched by position.** `/score` returns `pct` and `/cluster` returns `labels` as arrays in the same zone order as `/meta` (see `with_gaps` in `app.py`). They don't include `zone_id`. The browser copes because it holds the `/meta` order. An external GIS tool doing a table join can't.
- **Styling lives in the browser.** The red-white-green score ramp, the decile classes, the cluster colours (TAB10), the greyed-out excluded zones and the tooltip are all built in JavaScript in `index.html`. Any other client has to rebuild them, for example as a renderer or a style file.

## Options

| Option | What it involves | Rough effort |
| --- | --- | --- |
| **1. ArcGIS look and feel in the web app** | Use an Esri basemap, or the ArcGIS Maps SDK for JavaScript. deck.gl's `@deck.gl/arcgis` module can draw the current layer on an Esri `MapView` or `SceneView` almost unchanged. Another way is to rewrite `render()` as an Esri `FeatureLayer` built from client-side graphics with a class-breaks renderer. The sidebar, chat agent and API stay as they are. | 0.5–2 days |
| **2. Export results for desktop GIS** | Add an `/export` endpoint that takes the same body as `/score` (and `/cluster`) and returns a GeoPackage or GeoJSON. Each zone carries `zone_id`, its feature values, its score percentile or cluster label, and an excluded flag. Analysts open the file in ArcGIS Pro or QGIS. Optionally ship a `.qml` (QGIS) or `.lyrx` (ArcGIS Pro) style file so the colours match the web app. | About 1 day |
| **3. Live, interactive inside QGIS or ArcGIS** | A QGIS Python plugin or an ArcGIS Pro Python toolbox with the weight and exclusion controls. It calls `/score` or `/cluster` and joins the results onto a zone layer loaded once from `/meta` or the level parquet files. For ArcGIS Online, a script would overwrite a hosted feature layer, or you'd build an Experience Builder widget. The chat agent's "apply config to the map" step would need rebuilding in the plugin, or it would be lost. | 1–2 weeks for the plugin |

## API changes that help every option

1. **Return `zone_id` with results.** Add it alongside the `pct` and `labels` arrays, or return results keyed by zone. This is the one real blocker for joining results in an external GIS.
2. **Serve standard geometry.** Have `/meta`, or a new `/geometry` endpoint, return GeoJSON. Better still, expose each zone level as an OGC API – Features collection, which QGIS and ArcGIS Pro both read natively.
3. **Return classes, not just values.** Include the decile for each zone in the response, so every client shows the same classes without copying the JavaScript.

## Recommendation

If the aim is to let analysts take results into their own GIS, do option 2 together with API change 1. That's about a day of work and covers both QGIS and ArcGIS. Only take on option 3 if people need the live slider experience inside the desktop tool.
