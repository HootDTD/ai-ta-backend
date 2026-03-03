"""AI-TA pgvector retrieval package.

Public API:
    retrieve_for_question(query, keywords, search_space_id, db_session, ...) -> (snippets, diag)
"""

from .pipeline import retrieve_for_question

__all__ = ["retrieve_for_question"]
