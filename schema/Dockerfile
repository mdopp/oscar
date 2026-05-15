# Migration sidecar for the oscar-household ServiceBay template.
# Runs `alembic upgrade head` against /var/lib/oscar/oscar.db on every
# pod start. Idempotent.
#
# Image: ghcr.io/mdopp/oscar-household-init

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OSCAR_DB_URL=sqlite:////var/lib/oscar/oscar.db

WORKDIR /opt/oscar-schema

# Install runtime deps. No application package — we only need Alembic +
# SQLAlchemy to drive op.execute() against SQLite. The migration files
# themselves are baked in below.
COPY schema/pyproject.toml ./
RUN pip install --no-cache-dir alembic>=1.13 sqlalchemy>=2.0

COPY schema/alembic.ini ./
COPY schema/migrations ./migrations

# Default entrypoint runs the upgrade. The pod manifest may override
# with `args: ["downgrade", "base"]` etc.; default is idempotent upgrade.
ENTRYPOINT ["alembic"]
CMD ["upgrade", "head"]
