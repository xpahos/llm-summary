import json

import pytest

from llm_summary.llm import DayViewModel, _loads_first_json, _parse_view_model

VALID = '{"date": "2026-06-29", "repo": "r", "sections": []}'


def test_plain_object():
    assert _loads_first_json(VALID)["date"] == "2026-06-29"


def test_code_fenced():
    assert _loads_first_json("```json\n" + VALID + "\n```")["repo"] == "r"


def test_trailing_prose_ignored():
    # The exact failure: valid JSON followed by extra data.
    raw = VALID + "\n\nHope this helps! Let me know if you need changes."
    assert _loads_first_json(raw)["date"] == "2026-06-29"


def test_second_object_ignored():
    raw = VALID + '\n{"stray": true}'
    assert _loads_first_json(raw)["repo"] == "r"


def test_leading_prose_before_object():
    raw = "Here is the JSON:\n" + VALID
    assert _loads_first_json(raw)["date"] == "2026-06-29"


def test_no_object_raises():
    with pytest.raises(ValueError):
        _loads_first_json("no json here at all")


def test_truncated_json_raises():
    with pytest.raises(json.JSONDecodeError):
        _loads_first_json('{"date": "2026-06-29", "repo":')  # incomplete


def test_parse_view_model_recovers_from_extra_data():
    raw = (
        '{"date":"2026-06-29","repo":"tianocore/edk2","sections":'
        '[{"id":"merged","title":"Merged","items":[]}]}\n\nDone.'
    )
    vm = _parse_view_model(raw, {})
    assert isinstance(vm, DayViewModel)
    assert vm.date == "2026-06-29" and vm.sections[0].id == "merged"
