from config import settings


def test_textbook_thresholds_present_and_sane():
    assert settings.TEXTBOOK_EMBEDDING_MODEL == "text-embedding-3-large"
    assert settings.TEXTBOOK_EMBEDDING_DIM == 3072
    assert settings.TEXTBOOK_DEDUP_EMBEDDING_CUTOFF == 0.85
    assert settings.TEXTBOOK_DEDUP_LLM_JUDGE_LOW == 0.75
    assert settings.TEXTBOOK_DEDUP_LLM_JUDGE_HIGH == 0.85
    assert 0.0 < settings.TEXTBOOK_PROBLEM_DETECTOR_ACCEPT_THRESHOLD < 1.0
    assert 0.0 < settings.TEXTBOOK_CLASSIFIER_ACCEPT_THRESHOLD < 1.0
    assert settings.TEXTBOOK_LLM_MAX_RETRIES >= 1
