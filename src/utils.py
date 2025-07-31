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
import boto3
import tempfile
import zipfile
import shutil
import aioboto3
import asyncio
import secrets
from functools import lru_cache
from openai import AsyncOpenAI
from fastapi import Request


def generate_id(length=12, prefix=""):
    """Generate a unique ID for the map or layer.

    Using characters [1-9A-HJ-NP-Za-km-z] (excluding 0, O, I, l)
    to avoid ambiguity in IDs.
    """
    assert len(prefix) in [0, 1], "Prefix must be at most 1 character"
    valid_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    result = "".join(secrets.choice(valid_chars) for _ in range(length - len(prefix)))
    return prefix + result


@lru_cache
def get_s3_client():
    config = boto3.session.Config(
        signature_version="s3",
    )
    return boto3.Session().client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ["S3_DEFAULT_REGION"],
        config=config,
    )


# shared session, each asyncio loop gets a client
_session = aioboto3.Session()
_clients = {}


async def get_async_s3_client():
    loop = asyncio.get_running_loop()
    if loop not in _clients:
        config = boto3.session.Config(
            signature_version="s3",
        )
        _clients[loop] = await _session.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=os.environ["S3_DEFAULT_REGION"],
            config=config,
        ).__aenter__()
    return _clients[loop]


def get_bucket_name():
    return os.environ["S3_BUCKET"]


async def process_zip_with_shapefile(zip_file_path):
    temp_dir = tempfile.mkdtemp()

    try:
        with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
            zip_ref.extractall(temp_dir)

        # Find all .shp files in the extracted directory, excluding __MACOSX folders
        shp_files = []
        for root, _, files in os.walk(temp_dir):
            # Skip __MACOSX directories
            if "__MACOSX" in root:
                continue

            for file in files:
                if file.lower().endswith(".shp"):
                    shp_files.append(os.path.join(root, file))

        if not shp_files:
            raise ValueError("No shapefile found in the ZIP archive")

        if len(shp_files) > 1:
            raise ValueError(
                "Multiple shapefiles found in the ZIP archive. Only one shapefile is supported."
            )

        gpkg_file_path = os.path.join(temp_dir, "converted.gpkg")
        shp_file = shp_files[0]

        layer_name = os.path.splitext(os.path.basename(shp_file))[0]

        ogr_cmd = [
            "ogr2ogr",
            "-f",
            "GPKG",
            gpkg_file_path,
            shp_file,
            "-nln",
            layer_name,
        ]

        process = await asyncio.create_subprocess_exec(*ogr_cmd)
        await process.wait()

        if process.returncode != 0:
            raise Exception(
                f"ogr2ogr command failed with exit code {process.returncode}"
            )

        return gpkg_file_path, temp_dir

    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise e


def get_openai_client(request: Request) -> AsyncOpenAI:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return AsyncOpenAI(base_url=base_url)
