FROM docker.io/python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY shared/oscar_logging /tmp/oscar_logging
COPY shared/oscar_db/pyproject.toml shared/oscar_db/README.md shared/oscar_db/alembic.ini /app/
COPY shared/oscar_db/migrations /app/migrations
COPY shared/oscar_db/src /app/src

RUN pip install --no-cache-dir /tmp/oscar_logging \
 && pip install --no-cache-dir /app \
 && rm -rf /tmp/oscar_logging

ENV PYTHONUNBUFFERED=1 \
    OSCAR_COMPONENT=oscar-db-migrate

ENTRYPOINT ["python", "-m", "oscar_db.cli"]
CMD ["upgrade"]
