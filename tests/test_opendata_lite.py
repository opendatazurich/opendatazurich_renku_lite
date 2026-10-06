"""Offline tests for opendata_lite, with Pyodide's ``pyodide.http`` and ``js`` faked."""

import asyncio
import io
import json
import sys
import types

import pandas as pd
import pytest

import opendata_lite as odl

ORIGIN = "https://lite.example.org"
CSV_URL = "https://data.stadt-zuerich.ch/dataset/pkg/download/data.csv"
PARQUET_URL = "https://data.stadt-zuerich.ch/dataset/pkg/download/data.parquet"
GEOJSON_LINK = "https://www.stadt-zuerich.ch/geodaten/download/Parkplaetze?format=geojson_link"
WFS_URL = "https://www.ogd.stadt-zuerich.ch/wfs/geoportal/Parkplaetze"

CAPABILITIES = b"""<?xml version="1.0"?>
<WFS_Capabilities xmlns="http://www.opengis.net/wfs" version="1.1.0">
  <FeatureTypeList>
    <FeatureType><Name>layer_b</Name><Title>B</Title></FeatureType>
    <FeatureType><Name>layer_a</Name><Title>A</Title></FeatureType>
  </FeatureTypeList>
</WFS_Capabilities>"""

GEOJSON = json.dumps(
    {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [8.54, 47.37]},
                "properties": {"typ": "Güterumschlag", "objectid": 1},
            }
        ],
    }
).encode()


def package_show(resources):
    return json.dumps(
        {
            "success": True,
            "result": {
                "name": "pkg",
                "title": "Package",
                "notes": "Notes",
                "sszBemerkungen": "",
                "author": "SSZ",
                "maintainer": "OGD",
                "maintainer_email": "ogd@example.org",
                "groups": [{"display_name": "Bevölkerung"}],
                "tags": [{"display_name": "tag"}],
                "metadata_created": "2026-01-01",
                "metadata_modified": "2026-10-01",
                "resources": [
                    {"name": f"r{i}", "package_id": "pkg", "resource_type": None, **r}
                    for i, r in enumerate(resources)
                ],
            },
        }
    ).encode()


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body

    async def bytes(self):
        return self._body


@pytest.fixture
def fetched(monkeypatch):
    """Install fake ``js``/``pyodide.http`` modules; return (routes, requested URLs)."""
    routes: dict[str, tuple[int, bytes]] = {}
    requested: list[str] = []

    async def pyfetch(url):
        requested.append(url)
        for prefix, (status, body) in routes.items():
            if url.startswith(prefix):
                return FakeResponse(status, body)
        return FakeResponse(404, b"")

    pyodide = types.ModuleType("pyodide")
    http = types.ModuleType("pyodide.http")
    http.pyfetch = pyfetch
    pyodide.http = http
    js = types.ModuleType("js")
    js.location = types.SimpleNamespace(origin=ORIGIN)
    monkeypatch.setitem(sys.modules, "pyodide", pyodide)
    monkeypatch.setitem(sys.modules, "pyodide.http", http)
    monkeypatch.setitem(sys.modules, "js", js)
    return routes, requested


def proxied(url):
    return f"{ORIGIN}/proxy/{url.removeprefix('https://')}"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CSV_URL, f"{ORIGIN}/proxy/data.stadt-zuerich.ch/dataset/pkg/download/data.csv"),
        (
            "https://data.stadt-zuerich.ch/dataset/pkg/download/x.csv?a=1&b=2",
            f"{ORIGIN}/proxy/data.stadt-zuerich.ch/dataset/pkg/download/x.csv?a=1&b=2",
        ),
        # CKAN API and WFS send CORS headers: fetched directly.
        ("https://data.stadt-zuerich.ch/api/3/action/package_show?id=x", None),
        (f"{WFS_URL}?service=WFS", None),
        ("https://raw.githubusercontent.com/o/r/main/x.csv", None),
    ],
)
def test_resolve_url_proxies_only_blocked_downloads(url, expected):
    assert odl.resolve_url(url, origin=ORIGIN + "/") == (expected or url)


def test_read_tabular_csv_falls_back_to_semicolon():
    comma = odl.read_tabular(b"a,b\n1,2\n3,4\n", "csv")
    semicolon = odl.read_tabular(b"Jahr;Wert\n2024;1,5\n2025;2\n", "csv")
    assert list(comma.columns) == ["a", "b"] and len(comma) == 2
    assert list(semicolon.columns) == ["Jahr", "Wert"] and len(semicolon) == 2


def test_read_tabular_csv_tolerates_bom_and_bad_bytes():
    df = odl.read_tabular('﻿"x","y"\n"ä",1\n'.encode() + b'"\xff",2\n', "csv")
    assert df.shape == (2, 2)


def test_read_tabular_parquet_roundtrip():
    buffer = io.BytesIO()
    pd.DataFrame({"x": [1, 2], "y": ["a", "b"]}).to_parquet(buffer)
    df = odl.read_tabular(buffer.getvalue(), "parquet")
    assert df["x"].tolist() == [1, 2]


def test_read_tabular_rejects_other_formats(capsys):
    assert odl.read_tabular(b"", "") is None
    assert "Cannot load data" in capsys.readouterr().out


def test_get_dataset_fetches_via_proxy(fetched):
    routes, requested = fetched
    routes[proxied(CSV_URL)] = (200, b"a;b\n1;2\n")
    df = asyncio.run(odl.get_dataset(CSV_URL))
    assert requested == [proxied(CSV_URL)]
    assert df.to_dict("list") == {"a": [1], "b": [2]}


def test_fetch_raises_on_http_error(fetched):
    routes, _ = fetched
    routes[proxied(CSV_URL)] = (403, b"Domain not allowed")
    with pytest.raises(RuntimeError, match="HTTP 403"):
        asyncio.run(odl.get_dataset(CSV_URL))


def test_tabular_package_selects_resource_by_id_or_index(fetched):
    routes, requested = fetched
    routes[odl.CKAN_API_LINK] = (
        200,
        package_show(
            [
                {"id": "parq", "format": "parquet", "url": PARQUET_URL},
                {"id": "csv", "format": "CSV", "url": CSV_URL},
                {"id": "pdf", "format": "PDF", "url": "https://example.org/x.pdf"},
            ]
        ),
    )
    routes[proxied(CSV_URL)] = (200, b"a,b\n1,2\n")

    async def run():
        package = await odl.OpenDataZurich().get_package(name="pkg")
        return package, await package.tabular_resource(id="csv").load()

    package, df = asyncio.run(run())
    assert requested[0] == f"{odl.CKAN_API_LINK}/package_show?id=pkg"
    assert package.dataset_type == "csv"
    assert package.tabular_resource(0).metadata["id"] == "parq"
    assert df.shape == (1, 2)


def test_geo_package_loads_first_wfs_layer(fetched):
    routes, requested = fetched
    routes[odl.CKAN_API_LINK] = (
        200,
        package_show(
            [
                {"id": "csv", "format": "CSV", "url": CSV_URL},
                {"id": "geo", "format": "JSON", "url": GEOJSON_LINK},
            ]
        ),
    )
    routes[f"{WFS_URL}?service=WFS&version=1.1.0&request=GetCapabilities"] = (200, CAPABILITIES)
    routes[f"{WFS_URL}?service=WFS&version=1.1.0&request=GetFeature&typename=layer_b"] = (
        200,
        GEOJSON,
    )

    async def run():
        package = await odl.OpenDataZurich().get_package(name="pkg")
        resource = package.geo_resource(id="geo")
        return package, resource, await resource.load(), await resource.get_layers()

    package, resource, gdf, layers = asyncio.run(run())
    assert package.dataset_type == "geo"
    assert resource.wfs_url == WFS_URL
    assert layers == ["layer_b", "layer_a"]
    assert gdf.crs.to_epsg() == 4326
    assert gdf.iloc[0]["typ"] == "Güterumschlag"
    assert all(not u.startswith(f"{ORIGIN}/proxy/") for u in requested)


@pytest.mark.parametrize(
    ("resources", "expected"),
    [
        ([{"format": "CSV", "url": CSV_URL}, {"format": "JSON", "url": GEOJSON_LINK}], "geo"),
        ([{"format": "parquet", "url": PARQUET_URL}], "csv"),
        ([{"format": "PDF", "url": "https://example.org/x.pdf"}], "unknown"),
    ],
)
def test_dataset_type_matches_renku_rule(resources, expected):
    assert odl.dataset_type(resources) == expected


def test_display_metadata_renders_without_network(fetched):
    routes, _ = fetched
    routes[odl.CKAN_API_LINK] = (
        200,
        package_show([{"id": "csv", "format": "CSV", "url": CSV_URL}]),
    )
    package = asyncio.run(odl.OpenDataZurich().get_package(name="pkg"))
    package.display_metadata()
    package.display_resource_summary()
    package.tabular_resource(0).display_metadata()


def test_ensure_package_installs_missing_modules_only(monkeypatch):
    installed = []

    async def install(name):
        installed.append(name)

    monkeypatch.setitem(sys.modules, "micropip", types.SimpleNamespace(install=install))
    asyncio.run(odl.ensure_package("pandas"))
    asyncio.run(odl.ensure_package("not_a_real_module_xyz", "some-dist"))
    assert installed == ["some-dist"]
