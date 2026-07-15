from apollo.smart_questions.common_words import COMMON_ENGLISH_WORDS


def test_common_english_words_hygiene():
    assert 2500 <= len(COMMON_ENGLISH_WORDS) <= 3500
    assert all(word.islower() for word in COMMON_ENGLISH_WORDS)
    assert all(len(word) >= 3 for word in COMMON_ENGLISH_WORDS)
    assert all(not any(character.isdigit() for character in word) for word in COMMON_ENGLISH_WORDS)
    assert {
        "basically",
        "change",
        "defined",
        "feeling",
        "kind",
        "period",
        "started",
        "time",
    } <= COMMON_ENGLISH_WORDS
