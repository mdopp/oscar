FROM docker.io/python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Build context is the repo root so we can pip-install the shared library.
COPY shared/oscar_logging /tmp/oscar_logging
COPY connectors/CONNECTOR_NAME/pyproject.toml connectors/CONNECTOR_NAME/README.md /app/
COPY connectors/CONNECTOR_NAME/src /app/src

RUN pip install --no-cache-dir /tmp/oscar_logging \
 && pip install --no-cache-dir /app \
 && rm -rf /tmp/oscar_logging

ENV PYTHONUNBUFFERED=1 \
    OSCAR_COMPONENT=connector-CONNECTOR_NAME

EXPOSE 8800

ENTRYPOINT ["python", "-m", "CONNECTOR_NAME.server"]
