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

import asyncpg
from fastapi import HTTPException, status
from typing import List
from src.database.models import MapLayer


async def fetch_mvt_tile(
    layer: MapLayer, conn: asyncpg.Connection, z: int, x: int, y: int
) -> bytes:
    # Check if layer is a PostGIS type
    if layer.type != "postgis":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a PostGIS type. MVT tiles can only be generated from PostGIS data.",
        )

    if not layer.postgis_attribute_column_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PostGIS layer {layer.name} has no attribute columns, you must re-create the layer.",
        )
    non_geom_column_names: List[str] = layer.postgis_attribute_column_list + ["id"]

    mvt_query = f"""
        WITH
        bounds_webmerc AS (
            SELECT ST_TileEnvelope($1, $2, $3) AS wm_geom
        ),
        transformed AS (
            SELECT {", ".join([f"t.{name}" for name in non_geom_column_names])}, ST_Transform(t.geom, 3857) AS geom
            FROM ({layer.postgis_query}) t
        ),
        candidates AS (
            SELECT {", ".join([f"t.{name}" for name in non_geom_column_names])}, ST_MakeValid(t.geom) AS geom
            FROM transformed t, bounds_webmerc b
            WHERE t.geom && b.wm_geom
                AND ST_Intersects(t.geom, b.wm_geom)
        ),
        mvtgeom AS (
            SELECT {", ".join([f"c.{name}" for name in non_geom_column_names])}, ST_AsMVTGeom(c.geom, b.wm_geom::box2d) AS geom
            FROM candidates c, bounds_webmerc b
        )
        SELECT ST_AsMVT(mvtgeom, 'reprojectedfgb', 4096, 'geom', 'id') FROM mvtgeom
        """

    return await conn.fetchval(mvt_query, z, x, y)
