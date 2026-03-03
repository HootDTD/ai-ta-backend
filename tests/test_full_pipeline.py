"""
Test the full /ask pipeline: retrieval + LLM answer generation.
Runs against the backend directly (no server needed).
"""
import asyncio, os, sys, json

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# Load .env
env_path = os.path.join(_REPO_ROOT, '.env')
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

os.environ['USE_PGVECTOR_RETRIEVAL'] = 'true'

import requests

DEFAULT_CLASS_NAME = (
    os.getenv("AI_TA_CLASS_NAME")
    or os.getenv("TEXTBOOK_SUBJECT")
    or "Fluid Mechanics"
)
DEFAULT_READ_TIMEOUT_SEC = int(os.getenv("AI_TA_ASK_TIMEOUT_SEC", "300"))
DEFAULT_CONNECT_TIMEOUT_SEC = int(os.getenv("AI_TA_CONNECT_TIMEOUT_SEC", "10"))


def test_ask(question: str, class_name: str = DEFAULT_CLASS_NAME):
    print(f"\nQuestion: {question}")
    print(f"Class: {class_name}")
    print(f"Timeout: connect={DEFAULT_CONNECT_TIMEOUT_SEC}s, read={DEFAULT_READ_TIMEOUT_SEC}s")
    print("=" * 60)
    print("Sending to /ask endpoint... (this may take 30-60 seconds)\n")

    try:
        resp = requests.post(
            "http://localhost:8000/ask",
            json={
                "question": question,
                "class": class_name,
                "attachments": [],
            },
            timeout=(DEFAULT_CONNECT_TIMEOUT_SEC, DEFAULT_READ_TIMEOUT_SEC),
        )
    except requests.exceptions.ReadTimeout:
        print(
            "ERROR: Request timed out waiting for /ask response.\n"
            f"Try increasing AI_TA_ASK_TIMEOUT_SEC (current: {DEFAULT_READ_TIMEOUT_SEC}) "
            "or check backend logs for slow/hanging retrieval or model calls."
        )
        return
    except requests.exceptions.ConnectTimeout:
        print(
            "ERROR: Timed out connecting to http://localhost:8000.\n"
            "Ensure the backend server is running and reachable."
        )
        return
    except requests.exceptions.ConnectionError as exc:
        print(
            "ERROR: Could not connect to backend at http://localhost:8000.\n"
            f"Details: {exc}"
        )
        return
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: Request failed: {exc}")
        return

    if resp.status_code != 200:
        print(f"ERROR {resp.status_code}: {resp.text[:500]}")
        return

    data = resp.json()

    # Print the answer
    print("ANSWER:")
    print("-" * 60)
    print(data.get("answer", "(no answer field)"))

    # Print citations
    citations = data.get("citations", [])
    if citations:
        print("\n" + "-" * 60)
        print(f"CITATIONS ({len(citations)}):")
        for c in citations:
            if isinstance(c, dict):
                print(f"  - {c}")
            else:
                print(f"  - {c}")

    # Print logs summary if present
    logs = data.get("logs", "")
    if logs:
        print("\n" + "-" * 60)
        print("LOGS (first 500 chars):")
        print(str(logs)[:500])

if __name__ == "__main__":
    question = input("\nEnter your question (or press Enter for default): ").strip()
    if not question:
        question = "What is the Reynolds number and when does flow become turbulent?"

    class_name = input(f"Enter class name (or press Enter for default: {DEFAULT_CLASS_NAME}): ").strip()
    if not class_name:
        class_name = DEFAULT_CLASS_NAME

    test_ask(question, class_name)
