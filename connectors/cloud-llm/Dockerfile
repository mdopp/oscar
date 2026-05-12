FROM docker.io/python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY shared/oscar_logging /tmp/oscar_logging
COPY connectors/cloud-llm/pyproject.toml connectors/cloud-llm/README.md /app/
COPY connectors/cloud-llm/src /app/src

RUN pip install --no-cache-dir /tmp/oscar_logging \
 && pip install --no-cache-dir /app \
 && rm -rf /tmp/oscar_logging

ENV PYTHONUNBUFFERED=1 \
    OSCAR_COMPONENT=connector-cloud-llm

EXPOSE 8802

ENTRYPOINT ["python", "-m", "cloud_llm.server"]
