# Migration sidecar for the solarisbay ServiceBay template.
# Runs `alembic upgrade head` against /var/lib/solaris/solaris.db on every
# pod start. Idempotent.
#
# Image: ghcr.io/mdopp/solaris-schema-init

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOLARIS_DB_URL=sqlite:////var/lib/solaris/solaris.db

WORKDIR /opt/solaris-schema

# Install runtime deps. No application package — we only need Alembic +
# SQLAlchemy to drive op.execute() against SQLite. The migration files
# themselves are baked in below.
COPY pyproject.toml ./
RUN pip install --no-cache-dir "alembic==1.19.1" "sqlalchemy==2.0.52"

COPY alembic.ini ./
COPY migrations ./migrations

# Default entrypoint runs the upgrade. The pod manifest may override
# with `args: ["downgrade", "base"]` etc.; default is idempotent upgrade.
ENTRYPOINT ["alembic"]
CMD ["upgrade", "head"]
