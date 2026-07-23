from __future__ import annotations

import pytest

from database.models import (
    Course,
    CourseInvite,
    CourseMembership,
    Document,
    DocumentChunk,
    DocumentStatus,
    Upload,
    UploadJob,
)
from database.transforms import (
    chat_keywords_to_json_array,
    chat_keywords_to_text_array,
    document_status_columns,
    merge_course_settings,
    normalize_upload_job_state,
    normalize_upload_status,
)


def test_chat_keywords_json_string_to_text_array():
    assert chat_keywords_to_text_array('["entropy", "angular momentum"]') == [
        "entropy",
        "angular momentum",
    ]


def test_chat_keywords_text_array_to_json_string():
    assert chat_keywords_to_json_array(["mécanique", "heat"]) == '["mécanique", "heat"]'


@pytest.mark.parametrize("value", ['{"keyword": "heat"}', '["heat", 7]', "not-json"])
def test_chat_keywords_reject_non_string_arrays(value):
    with pytest.raises(ValueError, match="chat keywords"):
        chat_keywords_to_text_array(value)


def test_merge_course_settings_teacher_values_win_and_bounds_are_typed():
    result = merge_course_settings(
        {"textbook": 0.4, "notes": 0.3},
        {"notes": 0.8},
        {"min": "0.1", "max": 0.9},
        "6",
    )

    assert result == {
        "current_week": 6,
        "retrieval_weights": {"textbook": 0.4, "notes": 0.8},
        "retrieval_weight_min": 0.1,
        "retrieval_weight_max": 0.9,
    }


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ({"state": "failed", "reason": "OCR failed"}, ("failed", "OCR failed")),
        ({"state": "pending"}, ("queued", None)),
        ({"state": "unknown", "reason": "ignored"}, ("ready", None)),
        (None, ("ready", None)),
    ],
)
def test_document_status_columns_promote_state_and_failure_reason(legacy, expected):
    assert document_status_columns(legacy) == expected


def test_typed_upload_states_reject_unknown_values():
    assert normalize_upload_status("SUPERSEDED") == "superseded"
    assert normalize_upload_job_state("leased") == "leased"
    with pytest.raises(ValueError, match="Unknown upload status"):
        normalize_upload_status("pending")
    with pytest.raises(ValueError, match="Unknown upload job state"):
        normalize_upload_job_state("done")


def test_chat_keywords_none_normalizes_to_empty_list():
    # transforms.py:17 — the None guard short-circuits before JSON parsing.
    assert chat_keywords_to_text_array(None) == []


def test_merge_course_settings_inverted_bounds_reset_to_full_range():
    # transforms.py:46 — min > max is nonsensical, so it falls back to [0.0, 1.0].
    result = merge_course_settings(None, None, {"min": 0.9, "max": 0.1}, 5)
    assert result["current_week"] == 5
    assert result["retrieval_weight_min"] == 0.0
    assert result["retrieval_weight_max"] == 1.0


def test_merge_course_settings_non_numeric_bounds_and_week_use_defaults():
    # transforms.py:49-50 (week not int-able) + 95-96 (_bounded_float except).
    result = merge_course_settings(None, None, {"min": "abc"}, "not-a-week")
    assert result["current_week"] == 1
    assert result["retrieval_weight_min"] == 0.0
    assert result["retrieval_weight_max"] == 1.0


def test_merge_course_settings_out_of_range_week_clamps_to_one():
    # transforms.py:52 — week parses but falls outside the 1..16 course range.
    assert merge_course_settings(None, None, {}, 99)["current_week"] == 1


def test_document_status_failed_and_failure_reason_helpers():
    # database/models.py:81-82 (failed factory) + 94 (reason only when FAILED).
    assert DocumentStatus.failed("ocr blew up") == DocumentStatus.FAILED
    assert DocumentStatus.get_failure_reason(DocumentStatus.FAILED, "ocr blew up") == "ocr blew up"
    assert DocumentStatus.get_failure_reason(DocumentStatus.READY, "ocr blew up") is None


def test_db07_models_map_only_to_target_tables_and_columns():
    assert Course.__table__.fullname == "app.courses"
    assert CourseMembership.__table__.fullname == "app.course_memberships"
    assert CourseInvite.__table__.fullname == "app.course_invites"
    assert Document.__table__.fullname == "app.documents"
    assert DocumentChunk.__table__.fullname == "internal.document_chunks"
    assert Upload.__table__.fullname == "app.uploads"
    assert UploadJob.__table__.fullname == "internal.upload_jobs"
    assert {
        "current_week",
        "retrieval_weights",
        "retrieval_weight_min",
        "retrieval_weight_max",
    } <= set(Course.__table__.c.keys())
    assert {"status", "failure_reason", "metadata"} <= set(Document.__table__.c.keys())
    assert {"course_id", "document_id"} <= set(DocumentChunk.__table__.c.keys())
    assert {"course_id", "document_id", "ocr_details"} <= set(Upload.__table__.c.keys())
