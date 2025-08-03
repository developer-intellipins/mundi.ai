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

"""add title column to user_mundiai_projects table

Revision ID: 158ce8e20754
Revises: a01d56f3eead
Create Date: 2025-08-02 21:49:09.440712

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "158ce8e20754"
down_revision: Union[str, None] = "a01d56f3eead"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_mundiai_projects",
        sa.Column("title", sa.String(), nullable=True, server_default="Untitled Map"),
    )
    op.execute(
        "UPDATE user_mundiai_projects SET title = 'Untitled Map' WHERE title IS NULL"
    )


def downgrade() -> None:
    op.drop_column("user_mundiai_projects", "title")
