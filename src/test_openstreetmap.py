# Copyright (C) 2025 Bunting Labs, Inc.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import pytest
import os
import aiohttp
from unittest.mock import patch
from src.openstreetmap import download_from_openstreetmap
from src.dependencies.session import EditOrReadOnlyUserContext
from src.structures import async_conn


@pytest.fixture
async def test_map_id(auth_client):
    payload = {
        "title": "OSM Test Map",
        "description": "Test map for OpenStreetMap download",
    }
    response = await auth_client.post("/api/maps/create", json=payload)
    assert response.status_code == 200
    return response.json()["id"]


@pytest.mark.anyio
async def test_download_from_openstreetmap(auth_client, test_map_id):
    osm_path = "/app/test_fixtures/osm_lifeguard.geojson"
    with open(osm_path, "rb") as f:
        osm_data = f.read()

    class MockResponse:
        def __init__(self):
            self.status = 200

        async def read(self):
            return osm_data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_get(self, url, **kwargs):
        return MockResponse()

    with patch.object(aiohttp.ClientSession, "get", mock_get):
        with patch.dict(os.environ, {"BUNTINGLABS_OSM_API_KEY": "test_api_key"}):
            session = EditOrReadOnlyUserContext()

            result = await download_from_openstreetmap(
                map_id=test_map_id,
                bbox=[-180, -90, 180, 90],
                tags="emergency=lifeguard",
                new_layer_name="lifeguard",
                session=session,
            )

    assert result["status"] == "success"
    assert len(result["uploaded_layers"]) == 2

    points_layer = next(
        layer
        for layer in result["uploaded_layers"]
        if layer["geometry_type"] == "points"
    )
    assert points_layer["feature_count"] == 1

    polygons_layer = next(
        layer
        for layer in result["uploaded_layers"]
        if layer["geometry_type"] == "polygons"
    )
    assert polygons_layer["feature_count"] == 6

    async with async_conn("test_check_layer_exists") as conn:
        for layer in result["uploaded_layers"]:
            layer_result = await conn.fetchrow(
                "SELECT layer_id, name, type FROM map_layers WHERE layer_id = $1",
                layer["layer_id"],
            )
            assert layer_result is not None
            assert layer_result["layer_id"] == layer["layer_id"]
            assert layer_result["type"] == "vector"
            assert layer_result["name"] == f"lifeguard_{layer['geometry_type']}"
