import asyncio, os, sys, json
sys.path.insert(0, '.')

os.environ['SUPABASE_DB_URL'] = 'postgresql+asyncpg://postgres.jiryedlknfqqzdzgakxv:IshaanFeller@aws-1-us-east-2.pooler.supabase.com:5432/postgres'

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def fix():
    engine = create_async_engine(os.environ['SUPABASE_DB_URL'])
    async with engine.begin() as conn:
        result = await conn.execute(
            text("UPDATE aita_documents SET status = :s WHERE status->>'state' = 'ready'"),
            {'s': json.dumps({'state': 'pending'})}
        )
        print(f'Reset {result.rowcount} documents to pending')

asyncio.run(fix())
