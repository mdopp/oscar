"""forecast — OpenWeatherMap /data/2.5/forecast (5-day / 3-hour)."""

from datetime import datetime, timezone

import httpx
from oscar_logging import log
from pydantic import BaseModel, Field

from ..config import settings


class ForecastInput(BaseModel):
    location: str = Field(
        ..., description="Place name or postal code, e.g. 'Berlin' or '10115,DE'"
    )
    days: int = Field(3, ge=1, le=5, description="How many days ahead (1-5)")


class ForecastPoint(BaseModel):
    time: str
    temperature_c: float
    condition: str
    rain_mm: float = 0.0


class ForecastOutput(BaseModel):
    location: str
    points: list[ForecastPoint]
    fetched_at: str


async def run(input: ForecastInput, ctx) -> ForecastOutput:
    trace_id = (
        ctx.request_context.meta.get("trace_id")
        if hasattr(ctx, "request_context") and ctx.request_context.meta
        else None
    )
    log.info(
        "connector.call",
        event_type="forecast",
        trace_id=trace_id,
        location=input.location,
        days=input.days,
    )

    url = f"{settings.weather_base_url}/forecast"
    params = {
        "q": input.location,
        "appid": settings.weather_api_key,
        "units": settings.weather_units,
        "lang": settings.weather_language,
        # OWM returns 3-hour buckets; cap by `cnt` to limit response size.
        "cnt": input.days * 8,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        log.error(
            "connector.external_error",
            event_type="forecast",
            trace_id=trace_id,
            error=str(exc),
        )
        raise

    if response.status_code >= 400:
        log.warn(
            "connector.external_fail",
            event_type="forecast",
            trace_id=trace_id,
            status=response.status_code,
            body=response.text[:200],
        )
        response.raise_for_status()

    data = response.json()
    log.debug("connector.call.body", trace_id=trace_id, response_keys=list(data.keys()))

    points = [
        ForecastPoint(
            time=item["dt_txt"],
            temperature_c=item["main"]["temp"],
            condition=item["weather"][0]["description"],
            rain_mm=item.get("rain", {}).get("3h", 0.0),
        )
        for item in data.get("list", [])
    ]

    return ForecastOutput(
        location=data.get("city", {}).get("name", input.location),
        points=points,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
