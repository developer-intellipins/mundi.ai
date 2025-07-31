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

import os
import json
import asyncpg
from fastapi import (
    APIRouter,
    HTTPException,
    status,
    Request,
    Depends,
)
from fastapi.responses import StreamingResponse, Response
from ..dependencies.db_pool import get_pooled_connection
from ..dependencies.dag import get_layer
from pydantic import BaseModel
from src.database.models import MapLayer
from ..dependencies.session import (
    verify_session_required,
    UserContext,
)
from ..utils import get_openai_client
import logging
import re
from redis import Redis
import tempfile
import asyncio
from typing import List

from src.utils import (
    get_bucket_name,
    get_async_s3_client,
)
import duckdb
import subprocess
from src.duckdb import execute_duckdb_query
from src.structures import get_async_db_connection, async_conn
from ..dependencies.layer_describer import LayerDescriber, get_layer_describer
from ..dependencies.chat_completions import ChatArgsProvider, get_chat_args_provider
from opentelemetry import trace

# Global semaphore to limit concurrent social image renderings
# This prevents OOM issues when many maps load simultaneously
SOCIAL_RENDER_SEMAPHORE = asyncio.Semaphore(2)  # Max 2 concurrent renders

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

redis = Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    decode_responses=True,
)


layer_router = APIRouter()


@layer_router.get(
    "/layer/{layer_id}.cog.tif",
    operation_id="view_layer_as_cog_tif",
)
async def get_layer_cog_tif(
    request: Request,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
):
    # Check if layer is a raster type
    if layer.type != "raster":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a raster type. COG can only be generated from raster data.",
        )

    async with get_async_db_connection() as conn:
        # Check if layer is associated with any maps via the layers array
        map_result = await conn.fetchrow(
            """
            SELECT m.id, p.link_accessible, m.owner_uuid
            FROM user_mundiai_maps m
            JOIN user_mundiai_projects p ON m.project_id = p.id
            WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL
            """,
            layer.layer_id,
        )

        if map_result and not map_result["link_accessible"]:
            # If not publicly accessible, verify that we have auth
            if session.get_user_id() != str(map_result["owner_uuid"]):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        # Continue with the same implementation as the map-scoped endpoint
        bucket_name = get_bucket_name()

        # Check if metadata has cog_key
        cog_key = layer.metadata_dict.get("cog_key")

        # Set up MinIO/S3 client
        s3_client = await get_async_s3_client()

        # If COG doesn't exist, create it
        if not cog_key:
            with tempfile.TemporaryDirectory() as temp_dir:
                # Download the raster file
                s3_key = layer.s3_key
                file_extension = os.path.splitext(s3_key)[1]
                local_input_file = os.path.join(
                    temp_dir, f"layer_{layer.layer_id}{file_extension}"
                )

                # Download from S3 using async client
                s3 = await get_async_s3_client()
                await s3.download_file(bucket_name, s3_key, local_input_file)
                # Create COG file path
                local_cog_file = os.path.join(
                    temp_dir, f"layer_{layer.layer_id}.cog.tif"
                )

                # Check raster info (needed for band count)
                gdalinfo_cmd = ["gdalinfo", "-json", local_input_file]
                try:
                    gdalinfo_result = subprocess.run(
                        gdalinfo_cmd, check=True, capture_output=True, text=True
                    )
                    gdalinfo_json = json.loads(gdalinfo_result.stdout)
                except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
                    logger.error(
                        f"Failed to get gdalinfo for {layer.layer_id}: {e}",
                        exc_info=True,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to process raster info for layer {layer.layer_id}.",
                    )

                # Always attempt reprojection to EPSG:3857 (gdalwarp is a no-op if already in 3857)
                reprojected_file_path = os.path.join(
                    temp_dir, f"layer_{layer.layer_id}_3857.tif"
                )
                gdalwarp_cmd = [
                    "gdalwarp",
                    "-t_srs",
                    "EPSG:3857",
                    "-r",
                    "bilinear",  # resampling method
                    local_input_file,
                    reprojected_file_path,
                ]
                try:
                    print(
                        f"INFO: Running gdalwarp for layer {layer.layer_id} to EPSG:3857."
                    )
                    subprocess.run(
                        gdalwarp_cmd, check=True, capture_output=True, text=True
                    )
                    input_file_for_cog = reprojected_file_path
                    print(
                        f"INFO: Layer {layer.layer_id} successfully processed/reprojected to EPSG:3857."
                    )
                except subprocess.CalledProcessError as e:
                    print(
                        f"ERROR: gdalwarp failed for layer {layer.layer_id}: {e.stderr}. Using original file for COG creation."
                    )
                    # Fallback to original file if reprojection fails
                    input_file_for_cog = local_input_file

                # Get band count from the original gdalinfo output
                num_bands = len(gdalinfo_json.get("bands", []))
                needs_color_ramp_suffix = False

                if num_bands == 1:
                    try:
                        # Try expanding to RGB first
                        local_rgb_file = os.path.join(
                            temp_dir, f"layer_{layer.layer_id}_rgb.tif"
                        )
                        rgb_cmd = [
                            "gdal_translate",
                            "-of",
                            "GTiff",
                            "-expand",
                            "rgb",
                            local_input_file,
                            local_rgb_file,
                        ]
                        subprocess.run(
                            rgb_cmd, check=True, capture_output=True, text=True
                        )
                        input_file_for_cog = local_rgb_file
                        print(
                            f"INFO: Expanded single band to RGB for layer {layer.layer_id}"
                        )
                    except subprocess.CalledProcessError as e:
                        print(
                            f"WARN: gdal_translate -expand rgb failed for layer {layer.layer_id}: {e.stderr}. Using single-band with color ramp."
                        )
                        # Use the existing raster_value_stats_b1 from metadata
                        if "raster_value_stats_b1" in layer.metadata_dict:
                            needs_color_ramp_suffix = True
                            print(
                                f"INFO: Using existing raster_value_stats_b1 for layer {layer.layer_id}"
                            )
                        # Keep input_file_for_cog as the original single-band file

                # Convert to Cloud Optimized GeoTIFF
                cog_cmd_base = [
                    "gdal_translate",
                    "-of",
                    "COG",
                    "-co",
                    "BLOCKSIZE=256",
                ]
                if needs_color_ramp_suffix:
                    cog_cmd_base.extend(["-ot", "Float32"])
                    cog_cmd_compression = ["-co", "COMPRESS=LZW"]
                else:
                    cog_cmd_compression = ["-co", "COMPRESS=JPEG", "-co", "QUALITY=85"]

                cog_cmd = (
                    cog_cmd_base
                    + cog_cmd_compression
                    + [
                        "-co",
                        "OVERVIEWS=AUTO",
                        input_file_for_cog,
                        local_cog_file,
                    ]
                )

                try:
                    subprocess.run(cog_cmd, check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as e:
                    error_detail = f"COG generation failed. Command: {' '.join(e.cmd)}\nStderr: {e.stderr}\nStdout: {e.stdout}"
                    print(f"ERROR: {error_detail}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"COG generation failed: {e.stderr or 'Unknown GDAL error'}",
                    )

                # Upload the COG file to S3
                cog_key = f"cog/layer/{layer.layer_id}.cog.tif"
                s3 = await get_async_s3_client()
                await s3.upload_file(local_cog_file, bucket_name, cog_key)
                print(f"INFO: Uploaded COG to s3://{bucket_name}/{cog_key}")

                # Update the layer metadata with the COG key
                metadata = layer.metadata_dict
                metadata["cog_key"] = cog_key

                # Update the database
                await conn.execute(
                    """
                    UPDATE map_layers
                    SET metadata = $1
                    WHERE layer_id = $2
                    """,
                    json.dumps(metadata),
                    layer.layer_id,
                )
                print(f"INFO: Updated metadata for layer {layer.layer_id}", metadata)

        # Ensure cog_key is available if it was just generated
        if not cog_key:
            cog_key = layer.metadata_dict.get("cog_key")
            if not cog_key:
                # This case should ideally not be reached if generation logic is sound
                raise HTTPException(
                    status_code=500, detail="COG key missing after generation attempt."
                )

        # Get the file size first to handle range requests
        s3_head = await s3_client.head_object(Bucket=bucket_name, Key=cog_key)
        file_size = s3_head["ContentLength"]

        # Check for Range header to support byte serving
        range_header = request.headers.get("range", None) if request else None
        start_byte = 0
        end_byte = file_size - 1

        # Parse the Range header if present
        if range_header:
            range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
            if range_match:
                start_byte = int(range_match.group(1))
                end_group = range_match.group(2)
                if end_group:
                    end_byte = min(int(end_group), file_size - 1)
                else:
                    end_byte = file_size - 1

            # Calculate content length for the range
            content_length = end_byte - start_byte + 1

            # Get the specified range from S3
            s3_response = await s3_client.get_object(
                Bucket=bucket_name,
                Key=cog_key,
                Range=f"bytes={start_byte}-{end_byte}",
            )

            # Set response status and headers for partial content
            status_code = 206  # Partial Content
            headers = {
                "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Type": "image/tiff",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Range, Content-Type",
            }
        else:
            # Get the entire file
            s3_response = await s3_client.get_object(Bucket=bucket_name, Key=cog_key)
            status_code = 200
            headers = {
                "Content-Length": str(file_size),
                "Content-Type": "image/tiff",
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Range, Content-Type",
            }

        # Create an async generator to stream the file
        async def stream_s3_file():
            # Get the body of the S3 object (this is a stream)
            body = s3_response["Body"]

            # Stream the content in chunks
            chunk_size = 8192  # 8KB chunks
            while True:
                chunk = await body.read(chunk_size)
                if not chunk:
                    break
                yield chunk

            # Close the body
            body.close()

        # Return a streaming response with the appropriate status and headers
        return StreamingResponse(
            stream_s3_file(), status_code=status_code, headers=headers
        )


@layer_router.get(
    "/layer/{layer_id}.pmtiles",
    operation_id="view_layer_as_pmtiles",
)
async def get_layer_pmtiles(
    request: Request,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
):
    async with get_async_db_connection() as conn:
        # Check if layer is a vector type
        if layer.type != "vector":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Layer is not a vector type. PMTiles can only be generated from vector data.",
            )

        # Check if layer is associated with any maps via the layers array
        map_result = await conn.fetchrow(
            """
            SELECT m.id, p.link_accessible, m.owner_uuid, m.project_id
            FROM user_mundiai_maps m
            JOIN user_mundiai_projects p ON m.project_id = p.id
            WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL
            """,
            layer.layer_id,
        )

        if map_result and not map_result["link_accessible"]:
            # If not publicly accessible, verify that we have auth
            # NOTE: owner_uuid is <class 'asyncpg.pgproto.pgproto.UUID'>
            if session.get_user_id() != str(map_result["owner_uuid"]):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                    headers={"WWW-Authenticate": "Bearer"},
                )

    # Set up S3 client and bucket
    bucket_name = get_bucket_name()

    # Check if metadata has pmtiles_key
    pmtiles_key = layer.metadata_dict.get("pmtiles_key")

    # If PMTiles doesn't exist, create it
    if not pmtiles_key:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Vector tiles for this layer have not been generated yet",
        )

    # Get the file size first to handle range requests using async S3
    s3 = await get_async_s3_client()
    s3_head = await s3.head_object(Bucket=bucket_name, Key=pmtiles_key)
    file_size = s3_head["ContentLength"]

    # Check for Range header to support byte serving
    range_header = request.headers.get("range", None) if request else None
    start_byte = 0
    end_byte = file_size - 1

    # Parse the Range header if present
    if range_header:
        range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start_byte = int(range_match.group(1))
            end_group = range_match.group(2)
            if end_group:
                end_byte = min(int(end_group), file_size - 1)
            else:
                end_byte = file_size - 1

        # Calculate content length for the range
        content_length = end_byte - start_byte + 1

    # Create streaming function that handles S3 connection properly
    async def stream_s3_file():
        s3 = await get_async_s3_client()
        if range_header:
            # Get range from S3
            s3_response = await s3.get_object(
                Bucket=bucket_name,
                Key=pmtiles_key,
                Range=f"bytes={start_byte}-{end_byte}",
            )
        else:
            # Get entire file from S3
            s3_response = await s3.get_object(Bucket=bucket_name, Key=pmtiles_key)

        # Read all content and yield in chunks
        body = s3_response["Body"]
        chunk_size = 8192
        while True:
            chunk = await body.read(chunk_size)
            if not chunk:
                break
            yield chunk

    # Set headers based on range request
    if range_header:
        status_code = 206  # Partial Content
        headers = {
            "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": "application/octet-stream",
        }
    else:
        status_code = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": "application/octet-stream",
        }

    # Return a streaming response with the appropriate status and headers
    return StreamingResponse(stream_s3_file(), status_code=status_code, headers=headers)


@layer_router.get(
    "/layer/{layer_id}.laz",
    operation_id="view_layer_as_laz",
)
async def get_layer_laz(
    request: Request,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
):
    if layer.type != "point_cloud":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a point cloud type",
        )

    if session.get_user_id() != str(layer.owner_uuid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Layer not found"
        )

    # Set up S3 client and bucket
    bucket_name = get_bucket_name()

    # Check if layer has s3_key
    s3_key = layer.s3_key

    # If S3 key doesn't exist, return error
    if not s3_key:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="LAZ file for this layer has not been generated yet",
        )

    # Get the file size first to handle range requests using async S3
    s3 = await get_async_s3_client()
    s3_head = await s3.head_object(Bucket=bucket_name, Key=s3_key)
    file_size = s3_head["ContentLength"]

    # Check for Range header to support byte serving
    range_header = request.headers.get("range", None) if request else None
    start_byte = 0
    end_byte = file_size - 1

    # Parse the Range header if present
    if range_header:
        range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start_byte = int(range_match.group(1))
            end_group = range_match.group(2)
            if end_group:
                end_byte = min(int(end_group), file_size - 1)
            else:
                end_byte = file_size - 1

        # Calculate content length for the range
        content_length = end_byte - start_byte + 1

    # Create streaming function that handles S3 connection properly
    async def stream_s3_file():
        s3 = await get_async_s3_client()
        if range_header:
            # Get range from S3
            s3_response = await s3.get_object(
                Bucket=bucket_name,
                Key=s3_key,
                Range=f"bytes={start_byte}-{end_byte}",
            )
        else:
            # Get entire file from S3
            s3_response = await s3.get_object(Bucket=bucket_name, Key=s3_key)

        # Read all content and yield in chunks
        body = s3_response["Body"]
        chunk_size = 8192
        while True:
            chunk = await body.read(chunk_size)
            if not chunk:
                break
            yield chunk

    # Set headers based on range request
    if range_header:
        status_code = 206  # Partial Content
        headers = {
            "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": "application/octet-stream",
        }
    else:
        status_code = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": "application/octet-stream",
        }

    # Return a streaming response with the appropriate status and headers
    return StreamingResponse(stream_s3_file(), status_code=status_code, headers=headers)


@layer_router.get(
    "/layer/{layer_id}/{z}/{x}/{y}.mvt",
    operation_id="get_layer_mvt_tile",
)
async def get_layer_mvt_tile(
    z: int,
    x: int,
    y: int,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
):
    # Validate tile coordinates
    if z < 0 or z > 18 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid tile coordinates"
        )
    async with async_conn("mvt") as conn:
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

        # Get PostGIS connection details and verify ownership
        connection_details = await conn.fetchrow(
            """
            SELECT user_id, connection_uri
            FROM project_postgres_connections
            WHERE id = $1
            """,
            layer.postgis_connection_id,
        )

        if not connection_details:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="PostGIS connection not found",
            )

        # Require that the connection owner is the requester
        # TODO this is a double check?
        if session.get_user_id() != str(connection_details["user_id"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Calculate tile bounds in Web Mercator (EPSG:3857)
    # Web Mercator bounds: [-20037508.34, -20037508.34, 20037508.34, 20037508.34]
    world_size = 20037508.34 * 2
    tile_size = world_size / (1 << z)

    xmin = -20037508.34 + x * tile_size
    ymin = 20037508.34 - (y + 1) * tile_size
    xmax = -20037508.34 + (x + 1) * tile_size
    ymax = 20037508.34 - y * tile_size
    try:
        # some geometries just aren't valid, so make them valid.
        # can't save the world
        async with get_pooled_connection(
            connection_details["connection_uri"]
        ) as postgis_conn:
            mvt_query = f"""
            WITH
            bounds_webmerc AS (
                SELECT ST_MakeEnvelope($1, $2, $3, $4, 3857) AS wm_geom
            ),
            bounds_native AS (
                SELECT ST_Transform(wm_geom, (SELECT ST_SRID(geom) FROM ({layer.postgis_query}) LIMIT 1)) AS nat_geom,
                       wm_geom::box2d AS b2d
                FROM bounds_webmerc
            ),
            candidates AS (
                SELECT {", ".join([f"t.{name}" for name in non_geom_column_names])}, ST_MakeValid(t.geom) AS geom
                FROM ({layer.postgis_query}) t, bounds_native b
                WHERE t.geom && b.nat_geom
                  AND ST_Intersects(t.geom, b.nat_geom)
            ),
            mvtgeom AS (
                SELECT {", ".join([f"c.{name}" for name in non_geom_column_names])}, ST_AsMVTGeom(ST_Transform(c.geom, 3857), b.b2d) AS geom
                FROM candidates c, bounds_native b
            )
            SELECT ST_AsMVT(mvtgeom, 'reprojectedfgb', 4096, 'geom', 'id') FROM mvtgeom
            """

            # Execute query and get MVT data
            mvt_data = await postgis_conn.fetchval(mvt_query, xmin, ymin, xmax, ymax)

        if mvt_data is None:
            mvt_data = b""

        return Response(
            content=mvt_data,
            media_type="application/vnd.mapbox-vector-tile",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=3600",
            },
        )

    except asyncpg.exceptions.InternalServerError as e:
        # Gracefully handle the specific projection-domain error
        if "transform: Point outside of projection domain" in str(e):
            return Response(
                content=b"",
                media_type="application/vnd.mapbox-vector-tile",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "public, max-age=3600",
                },
            )
        else:
            raise e


@layer_router.get(
    "/layer/{layer_id}.geojson",
    operation_id="view_layer_as_geojson",
)
async def get_layer_geojson(
    layer: MapLayer = Depends(get_layer),
):
    # Check if layer is a vector type
    if layer.type != "vector":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a vector type. GeoJSON format is only available for vector data.",
        )

    # Retrieve the vector data
    bucket_name = get_bucket_name()
    with tempfile.TemporaryDirectory() as temp_dir:
        # Get file extension from s3_key
        s3_key = layer.s3_key
        file_extension = os.path.splitext(s3_key)[1]

        local_input_file = os.path.join(
            temp_dir, f"layer_{layer.layer_id}_input{file_extension}"
        )

        # Download from S3 using async client
        s3 = await get_async_s3_client()
        await s3.download_file(bucket_name, s3_key, local_input_file)

        # Convert to GeoJSON using ogr2ogr
        local_geojson_file = os.path.join(temp_dir, f"layer_{layer.layer_id}.geojson")
        ogr_cmd = [
            "ogr2ogr",
            "-f",
            "GeoJSON",
            "-t_srs",
            "EPSG:4326",  # Ensure coordinates are in WGS84
            "-lco",
            "COORDINATE_PRECISION=6",  # ~1m precision at equator
            local_geojson_file,
            local_input_file,
        ]
        subprocess.run(ogr_cmd, check=True)

        # Read the GeoJSON file and return it
        with open(local_geojson_file, "r") as f:
            geojson_content = f.read()

        # Return the GeoJSON with appropriate headers and cache control
        return Response(
            content=geojson_content,
            media_type="application/geo+json",
            headers={
                "Content-Disposition": f'attachment; filename="{layer.name}.geojson"',
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",  # Cache for 24 hours
            },
        )


KUE_SQL_SYSTEM_PROMPT = """
You are an AI assistant that is converting natural language queries to SQL for DuckDB with spatial extension.
The user is viewing a GIS attribute table, and will type ad-hoc into an input text box. You are to
write a DuckDB SQL query that will automatically be executed and the results will be shown to them in
the attribute table. DuckDB is read-only.

<QueryInstructions>
In your SELECT statement, list out the columns explicitly, most important first. DO NOT `SELECT * FROM ...`.
This is because the user has limited screen space, and some columns are more interesting than others.

ONLY quote column names that have colons in them, e.g. "name:en" vs CountryCode (no quotes).

If the user message is empty, give a SQL query that would best display table data.
Generally limit your SQL query to 6 columns, as more is too much to display.

DO NOT INCLUDE Geometry / geom columns in the SELECT statement.
</QueryInstructions>

<Columns>
Shape__Area and Shape__Length are generally useless unless the user asks for them.
</Columns>

<ResponseFormat>
Begin IMMEDIATELY with SQL command.
NEVER preface or suffix with English.
NEVER use ` or ``` or ```sql to begin or end the SQL.
NEVER add comments like #, --, //, or /*.

Use newlines to wrap text at 80 characters and use tabs to indent semantically.
However, do not put each column on a new line, as it takes up too much vertical height.
This is the perfect sweet spot:

SELECT foo, bar, baz
FROM FROM Lexample
WHERE col LIKE '%test%'
    AND baz = 'qux'
LIMIT 10;

</ResponseFormat>
"""


class LayerQueryRequest(BaseModel):
    natural_language_query: str
    max_n_rows: int = 20


@layer_router.post(
    "/layer/{layer_id}/query",
    operation_id="query_layer",
)
async def query_layer(
    request: Request,
    body: LayerQueryRequest,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
    layer_describer: LayerDescriber = Depends(get_layer_describer),
    chat_args: ChatArgsProvider = Depends(get_chat_args_provider),
):
    natural_language_query = body.natural_language_query
    max_n_rows = min(body.max_n_rows, 25)  # Cap at 25 rows

    # Check if schema info is cached in Redis
    schema_info = redis.get(f"vector_schema:{layer.layer_id}:duckdb")
    if not schema_info:
        # ~0.5 seconds
        schema_info = await describe_layer_internal(
            layer.layer_id, layer_describer, session.get_user_id()
        )

        # 5 minute expiry
        redis.set(
            f"vector_schema:{layer.layer_id}:duckdb",
            schema_info,
            ex=5 * 60,
        )

    # Generate SQL from natural language query using async client
    client = get_openai_client(request)

    sql_messages = [
        {
            "role": "system",
            "content": KUE_SQL_SYSTEM_PROMPT,
        },
        {
            "role": "system",
            "content": f"""
The table name representing the layer is {layer.layer_id}.

The column names are from "Attribute Fields" in the table schema.
DO NOT select column names that are not listed. Do not assume there
is a primary key column like ID or id, unless it's affirmatively listed.

<TableSchema>
{schema_info}
</TableSchema>
""",
        },
        {
            "role": "user",
            "content": natural_language_query,
        },
    ]

    # Loop in case we see an error or two
    for _ in range(2):
        # ~1.4 seconds
        chat_completions_args = await chat_args.get_args(
            session.get_user_id(), "query_layer"
        )
        response = await client.chat.completions.create(
            **chat_completions_args,
            messages=sql_messages,
            max_completion_tokens=512,
        )

        sql_query = response.choices[0].message.content.strip()

        # Use the execute_duckdb_query function from src/duckdb.py
        try:
            # ~1.1 seconds
            result = await execute_duckdb_query(sql_query, layer.layer_id, max_n_rows)
            return result

        except (duckdb.duckdb.BinderException, duckdb.duckdb.CatalogException) as e:
            sql_messages.append(
                {
                    "role": "system",
                    "content": f"<SQLQueryError> {e} </SQLQueryError> Fix your above query.",
                }
            )
            print("error", e, "trying again")
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An error occurred while executing the SQL query",
            )


async def describe_layer_internal(
    layer_id: str,
    layer_describer: LayerDescriber,
    session_user_id: str,
) -> str:
    async with get_async_db_connection() as conn:
        layer = await conn.fetchrow(
            """
            SELECT layer_id, name, type, metadata, bounds, geometry_type,
                   created_on, last_edited, feature_count, s3_key,
                   postgis_query, postgis_connection_id
            FROM map_layers
            WHERE layer_id = $1
            """,
            layer_id,
        )

        if not layer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Layer not found"
            )

        # Check if the layer is associated with any maps via the layers array
        # Order by created_on DESC to get the most recently created map first
        map_result = await conn.fetchrow(
            """
            SELECT id, title, description, owner_uuid
            FROM user_mundiai_maps
            WHERE $1 = ANY(layers) AND soft_deleted_at IS NULL
            ORDER BY created_on DESC
            """,
            layer_id,
        )
        if map_result:
            # User must own the map to access this endpoint
            if session_user_id != str(map_result["owner_uuid"]):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You must own this map to access layer description",
                )

        # Use the injected LayerDescriber to generate the response
        markdown_response = await layer_describer.describe_layer(layer_id, dict(layer))

        # Fetch active style JSON if layer is associated with a map
        if map_result:
            style_result = await conn.fetchrow(
                """
                SELECT ls.style_json, ls.style_id
                FROM map_layer_styles mls
                JOIN layer_styles ls ON mls.style_id = ls.style_id
                WHERE mls.map_id = $1 AND mls.layer_id = $2
                """,
                map_result["id"],
                layer_id,
            )
            if style_result:
                # Add style information if available (for vector layers)
                style_section = f"\n## Style ID ({style_result['style_id']})\n"
                style_section += "```json\n"
                # Parse style_json if it's a string (asyncpg returns JSON as strings)
                style_json = style_result["style_json"]
                if isinstance(style_json, str):
                    style_section += style_json
                else:
                    style_section += json.dumps(style_json)
                style_section += "\n```"
                markdown_response += style_section

        return markdown_response


@layer_router.get(
    "/layer/{layer_id}/describe",
    operation_id="describe_layer",
)
async def describe_layer(
    layer_id: str,
    request: Request,
    session: UserContext = Depends(verify_session_required),
    layer_describer: LayerDescriber = Depends(get_layer_describer),
):
    markdown_response = await describe_layer_internal(
        layer_id, layer_describer, session.get_user_id()
    )

    return Response(
        content=markdown_response,
        media_type="text/plain",
    )
