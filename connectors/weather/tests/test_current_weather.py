"""Mock-backed tests for current_weather. No real API calls."""

import os

import pytest

os.environ.setdefault("CONNECTORS_BEARER", "test")
os.environ.setdefault("WEATHER_API_KEY", "test")

from weather.tools.current_weather import CurrentWeatherInput, run


class _Ctx:
    class _Rc:
        meta = {"trace_id": "test-trace"}

    request_context = _Rc()


@pytest.mark.asyncio
async def test_happy_path(httpx_mock):
    httpx_mock.add_response(
        url__regex=r".*/weather\?.*q=Berlin.*",
        json={
            "name": "Berlin",
            "main": {"temp": 18.4, "feels_like": 17.8, "humidity": 62},
            "weather": [{"description": "leichter Regen"}],
            "wind": {"speed": 3.6},
        },
    )

    result = await run(CurrentWeatherInput(location="Berlin"), _Ctx())
    assert result.location == "Berlin"
    assert result.temperature_c == 18.4
    assert result.condition == "leichter Regen"
    assert result.wind_kph == round(3.6 * 3.6, 1)


@pytest.mark.asyncio
async def test_external_error(httpx_mock):
    httpx_mock.add_response(status_code=500, text="upstream is unhappy")
    with pytest.raises(Exception):
        await run(CurrentWeatherInput(location="Nowhere"), _Ctx())
