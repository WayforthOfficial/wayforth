import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WayforthLabs/1.0)"}


@router.get("/stock")
async def stock(symbol: str = Query(...)):
    symbol_upper = symbol.upper()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _YAHOO_URL.format(symbol=symbol_upper),
                params={"interval": "1d", "range": "1d"},
                headers=_YAHOO_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 429:
            raise HTTPException(status_code=429, detail="Yahoo Finance rate limit exceeded")
        raise HTTPException(status_code=502, detail=f"Yahoo Finance HTTP error: {code}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Yahoo Finance unreachable: {exc}")

    chart = data.get("chart", {})

    if chart.get("error"):
        err = chart["error"]
        raise HTTPException(
            status_code=404,
            detail=f"Yahoo Finance error for '{symbol_upper}': {err.get('description', err)}",
        )

    results = chart.get("result")
    if not results:
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol_upper}' not found")

    try:
        meta = results[0]["meta"]
        price = float(meta["regularMarketPrice"])
        currency = meta.get("currency", "USD")
        previous_close = float(
            meta.get("previousClose") or meta.get("chartPreviousClose") or 0.0
        )
        change_pct = (
            round(((price - previous_close) / previous_close) * 100, 4)
            if previous_close
            else 0.0
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected Yahoo Finance response shape: {exc}",
        )

    return {
        "symbol": symbol_upper,
        "price": price,
        "currency": currency,
        "change_pct": change_pct,
        "service": "wayforth-labs-stock",
    }
