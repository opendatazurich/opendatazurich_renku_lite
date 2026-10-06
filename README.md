# OpenDataZurich starter notebooks in JupyterLite

A static [JupyterLite](https://jupyterlite.readthedocs.io/) site that opens a starter notebook for any dataset of the [Stadt Zürich open data catalogue](https://data.stadt-zuerich.ch/). It is served as one shared Renku app. Python runs **in each visitor's browser** (Pyodide), so every visitor gets their own isolated kernel and their own copy of the notebook. The server only serves static files, plus a small download proxy (see [CORS](#cors-and-the-download-proxy)).

It is the lightweight counterpart of the per-user Renku sessions in [opendatazurich_renku](https://github.com/opendatazurich/opendatazurich_renku) (Python) and [opendatazurich_renku_r](https://github.com/opendatazurich/opendatazurich_renku_r) (R). Each notebook ends with a link to those sessions for heavier work or R.

## Link format

```
https://<app-host>/lab/index.html?package_id=<ckan-dataset-name>[&resource_id=<resource-uuid>]
```

For example, `…/lab/index.html?package_id=politik_abstimmungen_seit1933&resource_id=3e87b102-f19c-47f4-ab50-a679b51cf77e`. The parameters are the same `PACKAGE_ID` / `RESOURCE_ID` that the Renku session launcher takes. `https://<app-host>/?package_id=…` also works.

When the link opens, the `odz-launcher` JupyterLab plugin does what `post-init.sh` + papermill do in the Renku session:

1. Validates the parameters and fetches the dataset's CKAN metadata.
2. Picks `templates/TemplateGeo.ipynb` (the dataset has a GeoJSON/WFS resource) or `templates/TemplateCsv.ipynb`.
3. Writes the `parameters` cell and saves the notebook as `OpenData-<package_id>[--<resource>].ipynb` in the browser's storage, then opens it.

If the visitor already has that notebook, their edited copy is reopened instead. Nothing runs until the visitor runs the cells. Notebooks are generated on the fly, so the image never needs rebuilding when the catalogue changes.

## Layout

| Path | What |
|---|---|
| `extension/odz-launcher/` | JupyterLab 4 plugin (TypeScript) that turns the URL parameters into a notebook |
| `content/templates/` | Notebook templates, ported from the Renku session templates by `scripts/port_templates.py` (do not edit by hand) |
| `opendata_lite/` | Browser port of `opendatazurich_renku/src/opendata.py`, shipped as a wheel through piplite. Same API, but network calls are `await`ed (`await odz.get_package(...)`, `await resource.load()`), and WFS layers are read as GeoJSON instead of through owslib |
| `build/` | Pinned build toolchain and the extra pure-Python wheels (missingno, seaborn) served with the site |
| `nginx/templates/` | nginx config: static site on port 8080, `/healthz`, `/proxy/` |
| `Dockerfile` | Stage 1 runs `jupyter lite build`; stage 2 serves the result with non-root `nginx-unprivileged:alpine` on **8080** |

Runtime: the kernel is Pyodide 314.0.6 (from `jupyterlite-pyodide-kernel` 0.8.6), with Python 3.14, pandas 3.0, pyarrow 22, geopandas 1.1 and matplotlib 3.10. Pyodide itself loads from the jsDelivr CDN.

## Development

```bash
uv sync
uv run pytest                                   # opendata_lite unit tests (offline)

docker build -t odz-lite .
docker run --rm -p 18080:8080 odz-lite          # http://localhost:18080/?package_id=…
tests/test_proxy.sh http://localhost:18080      # site + proxy allowlist checks
uv run playwright install chromium
uv run python tests/smoke_playwright.py http://localhost:18080   # run-all on real datasets
```

To pick up upstream template changes, run `uv run python scripts/port_templates.py` (add `--ref <git-ref>` to port from another ref). The script fails if an upstream cell it rewrites has changed.

## Deploying as a Renku app

Deploy the image `ghcr.io/rokroskar/opendatazurich_renku_lite:<tag>`, built by `.github/workflows/build.yaml`. The container listens on port **8080**, needs no environment variables or volumes, and serves `/healthz` for probes. It is stateless, so any number of Knative replicas works without session affinity. The site expects to be served at the root of its host.

## CORS and the download proxy

In a Renku session, data is downloaded server-side by pandas, which sends no `Origin` header, so CORS does not apply. In JupyterLite, the browser downloads the data, so every request carries the page's `Origin` and must be allowed by the data host. Status as of 2026-10-06:

| Host | Browser access |
|---|---|
| CKAN API `data.stadt-zuerich.ch/api/3/action/*` | ✅ `Access-Control-Allow-Origin: *` |
| WFS `www.ogd.stadt-zuerich.ch/wfs/geoportal/*` (used for geo datasets) | ✅ `Access-Control-Allow-Origin: *` |
| Downloads `data.stadt-zuerich.ch/dataset/*/download/*` (~80% of CSV/parquet resources) | ❌ The redirect chain goes to `ckan-prod.zurich.datopian.com` → `proxy-server.datopian.com` → Cloudflare R2. `proxy-server.datopian.com` answers **403 "Domain not allowed"** whenever an `Origin` header is present (for every origin tested) and 200 without one. |

For now, the image proxies those downloads at the same origin: `/proxy/data.stadt-zuerich.ch/dataset/…/download/…`. The proxy:

- accepts only that host and path shape, and only GET;
- sends no client headers (no `Origin`, no cookies) and identifies itself with `User-Agent: OpenDataZurich-RenkuLite/1.0 (…)`;
- follows the redirect chain server-side, and only to the allowlisted Datopian/R2 hosts, so it cannot be used as an open proxy.

`opendata_lite.resolve_url` routes only these downloads through it; everything else goes directly from the browser.

**Request to the city / Datopian.** Please add the app's origin (e.g. `https://<app-host>`) to the allowed domains of `proxy-server.datopian.com` for the `dx-zurich-prod` bucket, or allow all origins for this public OGD data. After that, remove `data.stadt-zuerich.ch` from `PROXIED_DOWNLOAD_HOSTS` in `opendata_lite` and drop the `/proxy/` location.

## Limitations

- An in-browser kernel is limited to the browser tab's memory (a few GB). Very large resources need a full Renku session.
- Only packages from the Pyodide distribution, or pure-Python wheels, can be used. `%pip install` (piplite) works for the latter.
- Notebooks live in the visitor's browser storage. Use *File → Download* to keep a copy.
- R is not supported; use the R Renku session.
