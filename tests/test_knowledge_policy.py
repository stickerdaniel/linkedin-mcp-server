"""Contract for the root knowledge-policy pointer."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_POINTER = (
    "- Before changing repository knowledge, invariant comments, or temporary "
    "migration artifacts, read the [knowledge policy](docs/knowledge-policy.md)."
)
_GENERATED_FACTS = (
    "Do not restate derivable facts by hand. Keep generated facts only when a "
    "freshness check owns them."
)


def test_root_pointer_loads_the_knowledge_policy():
    instructions = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert instructions.count(_POINTER) == 1
    assert (_ROOT / "docs" / "knowledge-policy.md").is_file()


def test_generated_facts_require_a_freshness_check():
    policy = (_ROOT / "docs" / "knowledge-policy.md").read_text(encoding="utf-8")

    assert policy.count(_GENERATED_FACTS) == 1
