"""current_weather — OpenWeatherMap /data/2.5/weather."""

from datetime import datetime, timezone

import httpx
from oscar_logging import log
from pydantic import BaseModel, Field

from ..config import settings


class CurrentWeatherInput(BaseModel):
    location: str = Field(
        ..., description="Place name or postal code, e.g. 'Berlin' or '10115,DE'"
    )


class CurrentWeatherOutput(BaseModel):
    location: str
    temperature_c: float
    feels_like_c: float
    condition: str
    humidity_pct: int
    wind_kph: float
    fetched_at: str


async def run(input: CurrentWeatherInput, ctx) -> CurrentWeatherOutput:
    trace_id = (
        ctx.request_context.meta.get("trace_id")
        if hasattr(ctx, "request_context") and ctx.request_context.meta
        else None
    )
    log.info(
        "connector.call",
        event_type="current_weather",
        trace_id=trace_id,
        location=input.location,
    )

    url = f"{settings.weather_base_url}/weather"
    params = {
        "q": input.location,
        "appid": settings.weather_api_key,
        "units": settings.weather_units,
        "lang": settings.weather_language,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        log.error(
            "connector.external_error",
            event_type="current_weather",
            trace_id=trace_id,
            error=str(exc),
        )
        raise

    if response.status_code >= 400:
        log.warn(
            "connector.external_fail",
            event_type="current_weather",
            trace_id=trace_id,
            status=response.status_code,
            body=response.text[:200],
        )
        response.raise_for_status()

    data = response.json()
    log.debug("connector.call.body", trace_id=trace_id, response=data)

    return CurrentWeatherOutput(
        location=data.get("name", input.location),
        temperature_c=data["main"]["temp"],
        feels_like_c=data["main"]["feels_like"],
        condition=data["weather"][0]["description"],
        humidity_pct=data["main"]["humidity"],
        wind_kph=round(data["wind"]["speed"] * 3.6, 1)
        if settings.weather_units == "metric"
        else data["wind"]["speed"],
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
