import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


class SummarizeRequest(BaseModel):
    text: str
    max_sentences: int = Field(default=3, ge=1, le=50)


@router.post("/summarize")
async def summarize(body: SummarizeRequest):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="'text' must be non-empty")

    sentences = [s.strip() for s in _SENT_RE.split(text) if s.strip()]
    if not sentences:
        sentences = [text]

    selected = sentences[: body.max_sentences]
    summary = " ".join(selected)

    return {
        "summary": summary,
        "original_length": len(text),
        "summary_length": len(summary),
        "service": "wayforth-labs-summarizer",
    }
