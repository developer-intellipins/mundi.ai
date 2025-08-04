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

import json
from pathlib import Path
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from src.wsgi import app

app.openapi_url = "/openapi.json"


def custom_openapi():
    keep_names = {
        "create_map",
        "upload_layer_to_map",
        "set_layer_style",
        "render_map_to_png",
        "delete_project",
    }
    selected_routes = [
        r
        for r in app.router.routes
        if isinstance(r, APIRoute) and r.operation_id in keep_names
    ]

    openapi_schema = get_openapi(
        title="Mundi.ai Developer API",
        version="0.0.1",
        summary="Mundi.ai has a developer API for creating, editing, and sharing maps and map data.",
        description="""
These are the automatically generated API docs for Mundi's developer API. Mundi is a customizable,
open source web GIS and can be operated via API just like it can be used as a web app. You can programatically
create maps, upload geospatial data (vectors, raster, point clouds), and share map links or embed maps
in other web applications.

Mundi's API is both available as a [hosted cloud service](https://mundi.ai) or
[a self-hosted set of Docker images](https://github.com/buntinglabs/mundi.ai), open source under the AGPLv3 license.

Mundi.ai is below the v1.0.0 release. Backwards compatibility should be achieved by pinning Mundi to a specific
commit when self-hosting. In the near future, versioned API routes will guarantee backwards compatibility with
semver.
""",
        routes=selected_routes,
        terms_of_service="https://buntinglabs.com/legal/terms",
        contact={
            "name": "Bunting Labs",
            "url": "https://buntinglabs.com",
        },
    )

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

if __name__ == "__main__":
    # Generate the OpenAPI schema
    schema = custom_openapi()

    # Define the target path
    target_path = Path("docs/src/schema/openapi.json")

    # Create directories if they don't exist
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the schema to the file
    with open(target_path, "w") as f:
        json.dump(schema, f)

    print(f"OpenAPI schema written to {target_path}")
