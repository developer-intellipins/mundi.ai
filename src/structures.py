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

from __future__ import annotations
import datetime
import json
import os
import sys
import asyncpg
from typing import Literal, Optional
from opentelemetry import trace
import asyncio
from pydantic import BaseModel
from src.database.models import MundiChatCompletionMessage
from src.geoprocessing.dispatch import get_tools
from openai.types.chat import ChatCompletionMessageToolCallParam

IS_RUNNING_PYTEST = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ

_async_connection_pool = None
_async_pool_lock = asyncio.Lock()

# Get tracer for this module
tracer = trace.get_tracer(__name__)


async def _get_async_connection_pool():
    global _async_connection_pool
    if _async_connection_pool is None:
        async with _async_pool_lock:
            if _async_connection_pool is None:
                # Construct URL from components
                user = os.environ["POSTGRES_USER"]
                password = os.environ["POSTGRES_PASSWORD"]
                host = os.environ["POSTGRES_HOST"]
                port = os.environ.get("POSTGRES_PORT", "5432")
                db = os.environ["POSTGRES_DB"]
                postgres_url = f"postgresql://{user}:{password}@{host}:{port}/{db}"
                _async_connection_pool = await asyncpg.create_pool(
                    dsn=postgres_url,
                    min_size=1,
                    max_size=10,
                )
    return _async_connection_pool


class AsyncDatabaseConnection:
    """Context-manager that yields an *exclusive* connection.

    Using a per-request dedicated connection completely avoids the
    "another operation is in progress" race that can occur when the same
    connection object is shared between overlapping coroutines.  The
    overhead of opening a new connection is negligible for the test
    suite and greatly simplifies correctness.
    """

    def __init__(self, span_name: Optional[str] = None):
        self.conn: Optional[asyncpg.Connection] = None
        self.span: Optional[trace.Span] = None
        self.span_name: Optional[str] = span_name

    async def __aenter__(self) -> asyncpg.Connection:
        # only create a span if we're in a recording context
        current_span = trace.get_current_span()
        if current_span.is_recording():
            self.span = tracer.start_span(self.span_name or "asyncpg")

        # In pytest, connect directly instead of using pool
        if IS_RUNNING_PYTEST:
            user = os.environ["POSTGRES_USER"]
            password = os.environ["POSTGRES_PASSWORD"]
            host = os.environ["POSTGRES_HOST"]
            port = os.environ.get("POSTGRES_PORT", "5432")
            db = os.environ["POSTGRES_DB"]
            postgres_url = f"postgresql://{user}:{password}@{host}:{port}/{db}"
            self.conn = await asyncpg.connect(postgres_url)
        else:
            # Get connection from the pool
            pool = await _get_async_connection_pool()
            self.conn = await pool.acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            if IS_RUNNING_PYTEST:
                await self.conn.close()
            else:
                pool = await _get_async_connection_pool()
                await pool.release(self.conn)
        if self.span:
            self.span.end()


def get_async_db_connection():
    return AsyncDatabaseConnection()


def async_conn(span_name: Optional[str] = None):
    return AsyncDatabaseConnection(f"pg {span_name}")


class SanitizedMessage(BaseModel):
    role: str
    content: Optional[str] = None
    has_tool_calls: bool
    tool_calls: list[SanitizedToolCall]
    map_id: str
    created_at: datetime.datetime
    conversation_id: int
    tool_response: Optional[SanitizedToolResponse] = None


def convert_mundi_message_to_sanitized(
    cc_message: MundiChatCompletionMessage,
) -> SanitizedMessage:
    role = cc_message.message_json["role"]
    assert role in ["user", "assistant", "tool"]

    tool_calls = []
    if cc_message.message_json.get("tool_calls"):
        for tool_call in cc_message.message_json["tool_calls"]:
            tool_call: ChatCompletionMessageToolCallParam = tool_call
            tool_calls.append(
                convert_openai_tool_call_to_sanitized_tool_call(tool_call)
            )

    tool_response = None
    if role == "tool":
        try:
            content = json.loads(cc_message.message_json.get("content"))
            # delicately detect errors... by assuming success
            tool_response = SanitizedToolResponse(
                id=cc_message.message_json["tool_call_id"],
                status="error" if content["status"] == "error" else "success",
            )
        except (json.JSONDecodeError, KeyError):
            pass

    return SanitizedMessage(
        role=role,
        content=cc_message.message_json["content"] if role != "tool" else None,
        has_tool_calls=bool(cc_message.message_json.get("tool_calls")),
        tool_calls=tool_calls,
        map_id=cc_message.map_id,
        created_at=cc_message.created_at,
        conversation_id=cc_message.conversation_id,
        tool_response=tool_response,
    )


class CodeBlock(BaseModel):
    language: str
    code: str


class SanitizedToolCall(BaseModel):
    id: str
    tagline: str
    icon: Literal[
        "text-search",
        "brush",
        "wrench",
        "map-plus",
        "cloud-download",
        "zoom-in",
        "qgis",
    ]
    code: CodeBlock | None
    table: dict | None


class SanitizedToolResponse(BaseModel):
    id: str
    status: Literal["success", "error"]


TC_ICON_MAP = {
    "query_duckdb_sql": "text-search",
    "query_postgis_database": "text-search",
    "new_layer_from_postgis": "text-search",
    "set_layer_style": "brush",
    "add_layer_to_map": "map-plus",
    "zoom_to_bounds": "zoom-in",
    "download_from_openstreetmap": "cloud-download",
}
TC_TAGLINE_MAP = {
    "query_duckdb_sql": "Querying layer in DuckDB...",
    "query_postgis_database": "Querying PostGIS layer...",
    "new_layer_from_postgis": "Creating layer from PostGIS...",
    "set_layer_style": "Setting layer style...",
    "add_layer_to_map": "Adding layer to map...",
    "zoom_to_bounds": "Zooming to bounds...",
    "download_from_openstreetmap": "Downloading from OpenStreetMap...",
}


def sanitized_fc_table_from_args(args: dict) -> dict:
    return args


def convert_openai_tool_call_to_sanitized_tool_call(
    tool_call: ChatCompletionMessageToolCallParam,
) -> SanitizedToolCall:
    args = json.loads(tool_call["function"]["arguments"])
    function_name = tool_call["function"]["name"]

    # Check if this is a geoprocessing tool
    all_tools = get_tools()
    geoprocessing_function_names = [tool["function"]["name"] for tool in all_tools]

    is_geoprocessing_tool = function_name in geoprocessing_function_names

    code_block: CodeBlock | None = None
    if tool_call["function"]["name"] == "query_duckdb_sql":
        code_block = CodeBlock(language="sql", code=args["sql_query"])
    elif tool_call["function"]["name"] == "query_postgis_database":
        code_block = CodeBlock(language="sql", code=args["sql_query"])
    elif tool_call["function"]["name"] == "new_layer_from_postgis":
        code_block = CodeBlock(language="sql", code=args["query"])

    table: dict | None = None
    if tool_call["function"]["name"] == "download_from_openstreetmap":
        table = sanitized_fc_table_from_args(
            {
                "tags": args["tags"],
                "bbox": ", ".join(map(str, args["bbox"])),
            }
        )
    elif is_geoprocessing_tool:
        # For geoprocessing tools, put all arguments in a table
        table = sanitized_fc_table_from_args(args)

    # Determine tagline
    if is_geoprocessing_tool:
        # Replace underscores with colons for geoprocessing tools
        tagline = function_name.replace("_", ":")
    else:
        tagline = TC_TAGLINE_MAP.get(function_name, function_name)

    icon = TC_ICON_MAP.get(tool_call["function"]["name"], "wrench")
    if is_geoprocessing_tool:
        icon = "qgis"

    return SanitizedToolCall(
        id=tool_call["id"],
        tagline=tagline,
        icon=icon,
        code=code_block,
        table=table,
    )
