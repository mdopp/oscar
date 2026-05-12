FROM docker.io/python:3.12-slim

WORKDIR /app

# System deps kept minimal. Wyoming is pure Python; httpx wheel is pre-built.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY shared/oscar_logging /tmp/oscar_logging
COPY gatekeeper/pyproject.toml gatekeeper/README.md /app/
COPY gatekeeper/src /app/src

RUN pip install --no-cache-dir /tmp/oscar_logging \
 && pip install --no-cache-dir /app \
 && rm -rf /tmp/oscar_logging

ENV PYTHONUNBUFFERED=1 \
    OSCAR_COMPONENT=gatekeeper

# Wyoming pipeline endpoint that satellites (HA Voice PE, wyoming-satellite
# CLI, etc.) connect to. The Wyoming-internal whisper/piper services are
# reached via 127.0.0.1 inside the same pod.
EXPOSE 10700

ENTRYPOINT ["python", "-m", "gatekeeper"]
