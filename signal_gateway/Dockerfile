FROM docker.io/python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY shared/oscar_logging /tmp/oscar_logging
COPY signal_gateway/pyproject.toml signal_gateway/README.md /app/
COPY signal_gateway/src /app/src

RUN pip install --no-cache-dir /tmp/oscar_logging \
 && pip install --no-cache-dir /app \
 && rm -rf /tmp/oscar_logging

ENV PYTHONUNBUFFERED=1 \
    OSCAR_COMPONENT=signal-gateway

ENTRYPOINT ["python", "-m", "signal_gateway"]
