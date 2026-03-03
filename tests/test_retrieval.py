import asyncio, os, sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# Load .env manually (no dotenv dependency needed)
env_path = os.path.join(_REPO_ROOT, '.env')
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

os.environ['USE_PGVECTOR_RETRIEVAL'] = 'true'

from backend.retrieval.pipeline import retrieve_for_question
from backend.database.session import get_async_session
from backend.ai.main_ai import extract_and_filter_keywords

async def test(question):
    print(f'\nQuestion: {question}')
    print('-' * 60)

    _summary, raw = extract_and_filter_keywords(question, subject='Fluid Mechanics')
    keywords = [e['term'] for e in raw if isinstance(e, dict) and e.get('term')]
    print(f'Keywords: {keywords}')

    async with get_async_session() as db:
        snippets, diag = await retrieve_for_question(
            query=question,
            keywords=keywords,
            search_space_id=1,
            db_session=db,
        )

    print(f'Retrieved {len(snippets)} snippets')
    for i, s in enumerate(snippets[:5]):
        print(f'\n  [{i+1}] {s.citation_marker}')
        print(f'       {s.text[:200]}')

asyncio.run(test('What is the Reynolds number and when does flow become turbulent?'))
