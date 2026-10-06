"""
Browser (Pyodide/JupyterLite) port of the OpenDataZurich helper module from
https://github.com/opendatazurich/opendatazurich_renku/blob/main/src/opendata.py

The public shape is the same; only network access differs:

* HTTP goes through ``pyodide.http.pyfetch`` (the browser's ``fetch``), so
  every call that touches the network is ``async`` and must be awaited.
* Downloads from hosts that reject cross-origin browser requests are routed
  through the same-origin ``/proxy/`` endpoint of the app serving this site.
* WFS is read with plain HTTP + GeoJSON instead of owslib.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import urlencode, urlsplit

import numpy as np
import pandas as pd
from IPython.display import HTML, Markdown, display

__version__ = "1.0.0"

PROVIDER = "OpenDataZurich"
BASELINK_DATAPORTAL = "https://data.stadt-zuerich.ch/dataset/"
CKAN_API_LINK = "https://data.stadt-zuerich.ch/api/3/action"
WFS_BASE = "https://www.ogd.stadt-zuerich.ch/wfs/geoportal/"

# Download hosts that refuse browser requests carrying an Origin header (the
# Datopian storage proxy behind data.stadt-zuerich.ch answers 403 "Domain not
# allowed"). Requests to these are sent via the app's same-origin /proxy/.
# Drop a host from here once it allowlists the app origin.
PROXIED_DOWNLOAD_HOSTS = frozenset({"data.stadt-zuerich.ch"})
PROXY_PATH = "/proxy/"


# HTTP


def _app_origin() -> str:
    """Origin of the page (and kernel worker) serving this JupyterLite site."""
    import js  # Pyodide-only module

    return str(js.location.origin)


def resolve_url(url: str, origin: str | None = None) -> str:
    """Return the URL the browser should fetch for ``url``."""
    parts = urlsplit(url)
    if parts.hostname in PROXIED_DOWNLOAD_HOSTS and "/download/" in parts.path:
        origin = _app_origin() if origin is None else origin
        query = f"?{parts.query}" if parts.query else ""
        return f"{origin.rstrip('/')}{PROXY_PATH}{parts.hostname}{parts.path}{query}"
    return url


async def fetch_bytes(url: str) -> bytes:
    """Fetch ``url`` from the browser and return the body; raise on HTTP errors."""
    from pyodide.http import pyfetch  # Pyodide-only module

    target = resolve_url(url)
    response = await pyfetch(target)
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status} while fetching {url}")
    return await response.bytes()


async def fetch_json(url: str) -> dict:
    return json.loads(await fetch_bytes(url))


async def ensure_package(module: str, package: str | None = None) -> None:
    """Load a Pyodide package on first use.

    Pyodide only auto-loads packages imported directly in a notebook cell, not
    ones imported inside this module, so geopandas is installed lazily from the
    Pyodide distribution. Not usable for pyarrow: pandas detects pyarrow once at
    import time, so the notebook must import pyarrow before pandas.
    """
    import importlib.util

    if importlib.util.find_spec(module) is None:
        import micropip  # Pyodide-only module

        await micropip.install(package or module)


# Utility functions


def filter_tabular(full_df):
    """Filter to datasets that have a tabular data distribution"""
    df = full_df.copy()
    df.resources = df.resources.apply(has_tabular_distribution)
    df.dropna(subset=["resources"], inplace=True)
    return df


def filter_geo(full_df):
    """Filter to datasets that have a geo data distribution"""
    df = full_df.copy()
    df.resources = df.resources.apply(has_geo_distribution)
    df.dropna(subset=["resources"], inplace=True)
    return df


def _url_extension(url: str) -> str:
    path = urlsplit(url).path
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def read_tabular(content: bytes, extension: str) -> pd.DataFrame | None:
    """Parse downloaded parquet or CSV bytes the same way the Renku helper does."""
    if extension == "parquet":
        return pd.read_parquet(BytesIO(content))
    if extension == "csv":
        options = {"on_bad_lines": "warn", "encoding_errors": "ignore", "low_memory": False}
        df = pd.read_csv(BytesIO(content), sep=",", **options)
        # if dataframe only has one column or less the data is not comma separated, use ";" instead
        if df.shape[1] <= 1:
            df = pd.read_csv(BytesIO(content), sep=";", **options)
            if df.shape[1] <= 1:
                print(
                    "The data wasn't imported properly. Very likely the correct separator couldn't be found.\nPlease check the dataset manually and adjust the code."
                )
        return df
    print("Cannot load data! Please provide an url with csv or parquet extension.")
    return None


async def get_dataset(url):
    """
    Return pandas df if url is parquet or csv file. Return None if not.
    """
    extension = _url_extension(url)
    if extension not in ("parquet", "csv"):
        return read_tabular(b"", extension)
    return read_tabular(await fetch_bytes(url), extension)


def has_tabular_distribution(dists):
    """Iterate over package resources and keep only tabular entries in list"""
    tabular_dists = [
        x for x in dists if x.get("format", "") == "CSV" or x.get("format", "") == "parquet"
    ]
    if tabular_dists != []:
        return tabular_dists
    else:
        return np.nan


def has_geo_distribution(dists):
    """Iterate over package resources and keep only geo entries in list"""
    geo_dists = [x for x in dists if x.get("format", "") == "WFS"]
    if geo_dists != []:
        return geo_dists
    else:
        return np.nan


def dataset_type(resources) -> str:
    """'geo', 'csv' or 'unknown' — same rule as get_dataset_type.py in the Renku project."""
    resources = list(resources)
    if any("geojson" in str(r.get("url", "")) for r in resources):
        return "geo"
    if any(r.get("format") in ("CSV", "parquet") for r in resources):
        return "csv"
    return "unknown"


def identifier_from_url(url):
    """
    Extracts the identifier from the url.
    """
    # Extract the identifier from the url
    identifier = re.search(r"\/([^\/\?]+)\?", url).group(1)
    return identifier


def url_to_geoportal_url(url):
    """
    Converts the url to a geoportal url.
    """
    # Extract the identifier from the url
    try:
        identifier = identifier_from_url(url)
        # Create the geoportal url
        geoportal_url = f"{WFS_BASE}{identifier}"
        return geoportal_url
    except AttributeError:
        if "/wfs/" in url:
            # If the url already contains /wfs/ return it
            return url.split("?", 1)[0]
        else:
            # If the url does not contain /wfs/ return None
            print("Could not extract identifier from url.")
            return None


def layers_from_capabilities(xml: bytes) -> list[str]:
    """Feature type names, in document order, from a WFS GetCapabilities response."""
    root = ET.fromstring(xml)
    return [
        el.text.strip()
        for ft in root.iter()
        if ft.tag.rsplit("}", 1)[-1] == "FeatureType"
        for el in ft
        if el.tag.rsplit("}", 1)[-1] == "Name" and el.text
    ]


def geodataframe_from_geojson(content: bytes):
    """Build a GeoDataFrame from a GeoJSON FeatureCollection."""
    import geopandas as gpd

    data = json.loads(content)
    # GeoJSON is WGS84 unless a (legacy) crs member says otherwise.
    crs = data.get("crs", {}).get("properties", {}).get("name") or "EPSG:4326"
    return gpd.GeoDataFrame.from_features(data["features"], crs=crs)


async def geojson_layers_from_wfs(wfs_url):
    query = urlencode({"service": "WFS", "version": "1.1.0", "request": "GetCapabilities"})
    return layers_from_capabilities(await fetch_bytes(f"{wfs_url}?{query}"))


async def read_geojson_from_wfs(wfs_url, layer):
    query = urlencode(
        {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typename": layer,
            "outputFormat": "GeoJSON",
        }
    )
    return await fetch_bytes(f"{wfs_url}?{query}")


# API
class OpenDataZurich:
    def __init__(self):
        self.provider = PROVIDER
        self.baselink_dataportal = BASELINK_DATAPORTAL
        self.ckan_api_link = CKAN_API_LINK

    async def get_package(self, id=None, name=None):
        """Get a package from CKAN API"""
        if id is None and name is None:
            print("Please provide either an id or a name.")
            return None
        query = urlencode({"id": id if id is not None else name})
        data = await fetch_json(f"{self.ckan_api_link}/package_show?{query}")
        if not data["success"]:
            print(data.get("error", "No error message provided."))
            return None
        return OpenDataPackage(self, pd.json_normalize(data["result"]).iloc[0])


class OpenDataPackage:
    def __init__(self, odz, metadata):
        self.odz = odz
        self.metadata = metadata
        self.distributions = metadata.get("resources", [])
        self.distribution_links = [x.get("url") for x in self.distributions]
        self._resource_metadata_df = None
        self._tabular_resource_metadata_df = None
        self._geo_resource_metadata_df = None

    @property
    def dataset_type(self):
        return dataset_type(self.distributions)

    def display_metadata(self):
        display(
            HTML(
                f"<h2>Open Government Data, provided by <i>{self.odz.provider}</i></h2>"
                + f"<i>Generated Python starter code for data set with identifier</i> <b>{self.metadata['name']}</b>"
            )
        )

        display(HTML("<h2>Dataset</h2>" + f"<b>{self.metadata['title']}</b>"))

        display(HTML("<h2>Description</h2>"))
        display(Markdown(self.metadata.get("notes") or ""))
        display(Markdown(self.metadata.get("sszBemerkungen") or ""))

        display(HTML("<h2>Data set links</h2>"))
        display(
            HTML(
                f"<a href='{BASELINK_DATAPORTAL}{self.metadata['name']}'>Direct link by OpenDataZurich for dataset</a>"
            )
        )
        url = self.metadata.resources[0]["url"]
        display(HTML(f"<a href='{url}'>{url}</a>"))

        display(HTML("<h2>Metadata</h2>"))
        groups = self.metadata.get("groups") or [{}]
        display_name = groups[0].get("display_name", "")
        display_tags = [t["display_name"] for t in self.metadata.get("tags", [])]
        display(
            Markdown(
                f"* **Publisher** {self.metadata.get('author')}\n"
                + f"* **Maintainer** {self.metadata.get('maintainer')}\n"
                + f"* **Maintainer email** {self.metadata.get('maintainer_email')}\n"
                + f"* **Keywords** {display_name}\n"
                + f"* **Tags** {display_tags}\n"
                + f"* **Metadata created** {self.metadata.get('metadata_created')}\n"
                + f"* **Metadata modified** {self.metadata.get('metadata_modified')}\n"
            )
        )

    def display_resource_summary(self):
        display(
            HTML(
                "<h2>Resources</h2>"
                + f"<b>{len(self.resource_metadata_df)} resource(s) found in this dataset.</b>"
            )
        )
        summary_ser = self.resource_metadata_df.groupby("format").count()["url"]
        summary_ser.name = "resources by type"
        display(HTML(summary_ser.to_frame().to_html()))

    @property
    def geo_resource_metadata_df(self):
        if self._geo_resource_metadata_df is None:
            self._geo_resource_metadata_df = self.resource_metadata_df[
                self.resource_metadata_df["url"].str.contains("geojson")
            ]
        return self._geo_resource_metadata_df

    def geo_resource(self, index=0, id=None):
        """Find a resource by id, if provided, otherwise by index"""
        if id is not None:
            metadata = self.geo_resource_metadata_df[
                self.geo_resource_metadata_df["id"] == id
            ].iloc[0]
        else:
            metadata = self.geo_resource_metadata_df.iloc[index]
        return OpenDataGeoResource(self, index, metadata)

    @property
    def resource_metadata_df(self):
        if self._resource_metadata_df is None:
            self._resource_metadata_df = pd.DataFrame(list(self.metadata["resources"]))
        return self._resource_metadata_df

    @property
    def tabular_resource_metadata_df(self):
        if self._tabular_resource_metadata_df is None:
            self._tabular_resource_metadata_df = self.resource_metadata_df[
                self.resource_metadata_df["format"].isin(["CSV", "parquet"])
            ]
        return self._tabular_resource_metadata_df

    def tabular_resource(self, index=0, id=None):
        """Find a resource by id, if provided, otherwise by index"""
        if id is not None:
            metadata = self.tabular_resource_metadata_df[
                self.tabular_resource_metadata_df["id"] == id
            ].iloc[0]
        else:
            metadata = self.tabular_resource_metadata_df.iloc[index]
        return OpenDataTabularResource(self, index, metadata)


class OpenDataGeoResource:
    def __init__(self, package, index, metadata):
        self.package = package
        self.index = index
        self.metadata = metadata
        self._df = None
        self._layers = None

    def display_metadata(self):
        display(
            Markdown(
                f"* **name** {self.metadata['name']}\n"
                f"* **format** {self.metadata['format']}\n"
                f"* **url** {self.metadata['url']}\n"
                f"* **id** {self.metadata['id']}\n"
                f"* **resource_type** {self.metadata.get('resource_type')}\n"
                f"* **package_id** {self.metadata['package_id']}\n"
            )
        )

    @property
    def wfs_url(self):
        return url_to_geoportal_url(self.metadata["url"])

    async def layer_df(self, layer):
        """Return a geopandas data frame of the layer."""
        await ensure_package("geopandas")
        return geodataframe_from_geojson(await read_geojson_from_wfs(self.wfs_url, layer))

    async def get_layers(self):
        if self._layers is None:
            self._layers = await geojson_layers_from_wfs(self.wfs_url)
        return self._layers

    async def load(self):
        """Return a geopandas data frame of the first layer."""
        if self._df is None:
            layers = await self.get_layers()
            self._df = await self.layer_df(layers[0])
        return self._df


class OpenDataTabularResource:
    def __init__(self, package, index, metadata):
        self.package = package
        self.index = index
        self.metadata = metadata
        self._df = None

    def display_metadata(self):
        display(
            Markdown(
                f"* **name** {self.metadata['name']}\n"
                f"* **format** {self.metadata['format']}\n"
                f"* **url** {self.metadata['url']}\n"
                f"* **id** {self.metadata['id']}\n"
                f"* **resource_type** {self.metadata.get('resource_type')}\n"
                f"* **package_id** {self.metadata['package_id']}\n"
            )
        )

    async def load(self):
        """Download and parse the resource (cached after the first call)."""
        if self._df is None:
            self._df = await get_dataset(self.metadata["url"])
        return self._df
