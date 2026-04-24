import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_MYMEMORY_URL = "https://api.mymemory.translated.net/get"
_QUOTA_WARNING = "MYMEMORY WARNING"


class TranslateRequest(BaseModel):
    text: str
    target_language: str
    source_language: str = "auto"


@router.post("/translate")
async def translate(body: TranslateRequest):
    # MyMemory requires a valid ISO 639-1 code; "auto" is not accepted
    source = "en" if body.source_language == "auto" else body.source_language
    langpair = f"{source}|{body.target_language}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _MYMEMORY_URL,
                params={"q": body.text, "langpair": langpair},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"MyMemory HTTP error: {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"MyMemory unreachable: {exc}")

    if data.get("responseStatus") != 200:
        raise HTTPException(
            status_code=502,
            detail=f"MyMemory API error {data.get('responseStatus')}: {data.get('responseDetails', '')}",
        )

    translated = data["responseData"]["translatedText"]
    if _QUOTA_WARNING in translated:
        raise HTTPException(
            status_code=429,
            detail="MyMemory daily quota (1000 req/day) exhausted",
        )

    return {
        "translated_text": translated,
        "source_language": source,
        "target_language": body.target_language,
        "service": "wayforth-labs-translator",
    }
