from llm_summary.llm import _SYSTEM, _compose


def test_system_prompt_is_structured():
    for tag in ("<role>", "</role>", "<style>", "</style>", "<domain>", "</domain>"):
        assert tag in _SYSTEM
    # Shared rules live in the system prompt (stated once).
    low = _SYSTEM.lower()
    assert "push" in low and "mergify" in low and "markdown" in low


def test_compose_wraps_sections():
    msg = _compose("do the thing", {"a": 1}, output="plain text")
    assert "<task>\ndo the thing\n</task>" in msg
    assert '<input>\n{"a": 1}\n</input>' in msg
    assert "<output>\nplain text\n</output>" in msg
    # Sections appear in order: task, input, output.
    assert msg.index("<task>") < msg.index("<input>") < msg.index("<output>")


def test_compose_without_output():
    msg = _compose("t", {"k": "v"})
    assert "<output>" not in msg
    assert "<task>" in msg and "<input>" in msg
