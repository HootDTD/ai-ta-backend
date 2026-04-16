"""Reset any 'ready' documents back to 'pending' for reindexing.

Reads SUPABASE_DB_URL from the environment (loads project .env if present).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, '.')

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

DATABASE_URL = (os.environ.get("SUPABASE_DB_URL") or "").strip()
if not DATABASE_URL:
    sys.exit("SUPABASE_DB_URL is required. Set it in the environment or .env.")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def fix():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        result = await conn.execute(
            text("UPDATE aita_documents SET status = :s WHERE status->>'state' = 'ready'"),
            {'s': json.dumps({'state': 'pending'})}
        )
        print(f'Reset {result.rowcount} documents to pending')

asyncio.run(fix())
