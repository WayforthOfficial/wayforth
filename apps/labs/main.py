import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from services.translator import router as translator_router
from services.weather import router as weather_router
from services.stock import router as stock_router
from services.summarizer import router as summarizer_router
from services.search import router as search_router

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Wayforth Labs starting on port 8001")
    yield
    logger.info("Wayforth Labs shutting down")


app = FastAPI(title="Wayforth Labs", lifespan=lifespan)

app.include_router(translator_router)
app.include_router(weather_router)
app.include_router(stock_router)
app.include_router(summarizer_router)
app.include_router(search_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "wayforth-labs",
        "services": ["translator", "weather", "stock", "summarizer", "search"],
    }
