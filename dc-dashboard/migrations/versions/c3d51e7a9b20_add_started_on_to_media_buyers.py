"""add started_on to media_buyers

Revision ID: c3d51e7a9b20
Revises: a988f6481b3a
Create Date: 2026-09-29 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d51e7a9b20'
down_revision = 'a988f6481b3a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('media_buyers', sa.Column('started_on', sa.Date(), nullable=True))
    # DC1 empieza a trabajar el 2026-10-01 (decisión 2026-09-29) — el gasto
    # previo en su cuenta de Meta no es suyo y no debe generarle déficit.
    op.execute("UPDATE media_buyers SET started_on = DATE '2026-10-01' WHERE display_name = 'DC1'")


def downgrade() -> None:
    op.drop_column('media_buyers', 'started_on')
