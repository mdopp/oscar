FROM docker.io/python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY shared/oscar_logging /tmp/oscar_logging
COPY connectors/weather/pyproject.toml connectors/weather/README.md /app/
COPY connectors/weather/src /app/src

RUN pip install --no-cache-dir /tmp/oscar_logging \
 && pip install --no-cache-dir /app \
 && rm -rf /tmp/oscar_logging

ENV PYTHONUNBUFFERED=1 \
    OSCAR_COMPONENT=connector-weather

EXPOSE 8801

ENTRYPOINT ["python", "-m", "weather.server"]
