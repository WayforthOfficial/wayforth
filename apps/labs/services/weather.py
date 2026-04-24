import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_WTTR_URL = "https://wttr.in/{city}"


@router.get("/weather")
async def weather(
    city: str = Query(...),
    country_code: str = Query(default=None),
):
    city_param = f"{city},{country_code}" if country_code else city
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _WTTR_URL.format(city=city_param),
                params={"format": "j1"},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"wttr.in HTTP error: {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"wttr.in unreachable: {exc}")

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"wttr.in returned non-JSON for city '{city}'",
        )

    try:
        cond = data["current_condition"][0]
        temperature_c = float(cond["temp_C"])
        humidity = int(cond["humidity"])
        condition = cond["weatherDesc"][0]["value"]
    except (KeyError, IndexError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected wttr.in response shape: {exc}",
        )

    return {
        "city": city,
        "temperature_c": temperature_c,
        "condition": condition,
        "humidity": humidity,
        "service": "wayforth-labs-weather",
    }
