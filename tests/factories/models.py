"""factory_boy builders for the core ORM models.

Builders only — call ``.build()`` then persist with
:func:`tests.factories.persist`. Defaults satisfy every NOT NULL / UNIQUE
constraint so a bare ``Factory.build()`` is insertable; override per test.
"""

from __future__ import annotations

import uuid

import factory

from database.models import (
    AITAChunk,
    AITADocument,
    ChatSession,
    CourseMembership,
    SearchSpace,
    TeacherUpload,
)
from tests.fakes.embeddings import fake_embedding


class SearchSpaceFactory(factory.Factory):
    class Meta:
        model = SearchSpace

    name = factory.Sequence(lambda n: f"Course {n}")
    slug = factory.Sequence(lambda n: f"course-{n}")
    subject_name = "Aerospace Engineering"
    weight_overrides = factory.LazyFunction(dict)


class AITADocumentFactory(factory.Factory):
    class Meta:
        model = AITADocument

    title = factory.Sequence(lambda n: f"Lecture {n}")
    document_type = "EDUCATIONAL_FILE"
    material_kind = "lecture"
    content = factory.Sequence(lambda n: f"Body text for lecture {n}.")
    content_hash = factory.Sequence(lambda n: f"hash-{n:08d}")
    embedding = factory.LazyAttribute(lambda o: fake_embedding(o.title))
    status = factory.LazyFunction(lambda: {"state": "ready"})
    # search_space_id is required (FK) — always pass it explicitly.


class AITAChunkFactory(factory.Factory):
    class Meta:
        model = AITAChunk

    content = factory.Sequence(lambda n: f"Chunk {n} content.")
    embedding = factory.LazyAttribute(lambda o: fake_embedding(o.content))
    chunk_type = "body"
    # document_id is required (FK) — always pass it explicitly.


class CourseMembershipFactory(factory.Factory):
    class Meta:
        model = CourseMembership

    user_id = factory.LazyFunction(lambda: str(uuid.uuid4()))
    role = "student"
    # search_space_id is required (FK) — always pass it explicitly.


class ChatSessionFactory(factory.Factory):
    class Meta:
        model = ChatSession

    chat_id = factory.Sequence(lambda n: f"chat-{n}")
    user_id = factory.LazyFunction(lambda: str(uuid.uuid4()))
    meta = factory.LazyFunction(dict)
    memory_summary = ""
    # search_space_id is required (FK) — always pass it explicitly.


class TeacherUploadFactory(factory.Factory):
    class Meta:
        model = TeacherUpload

    week = 1
    kind = "lecture"
    title = factory.Sequence(lambda n: f"Week {n} notes")
    status = "ready"
    # search_space_id is required (FK) — always pass it explicitly.
