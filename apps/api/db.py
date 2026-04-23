import os
import logging

import psycopg2

logger = logging.getLogger(__name__)


def get_db_url() -> str:
    return os.environ.get("DATABASE_URL", "")


def check_db() -> bool:
    url = get_db_url()
    if not url:
        logger.warning("DATABASE_URL not set")
        return False
    try:
        conn = psycopg2.connect(url)
        conn.cursor().execute("SELECT 1")
        conn.close()
        return True
    except Exception as e:
        logger.error(f"DB check failed: {e}")
        return False
