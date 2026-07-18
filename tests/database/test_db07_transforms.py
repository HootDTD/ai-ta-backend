from __future__ import annotations

import pytest

from database.transforms import (
    document_status_columns,
    merge_course_settings,
    normalize_upload_job_state,
    normalize_upload_status,
)


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
