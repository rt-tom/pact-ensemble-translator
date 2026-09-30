"""Shared output headroom after hybrid reasoning budget expansion."""
from pathlib import Path


from pact_v4.runtime.runtime_config import (
    REVIEWER_ROLES,
    TRANSLATOR_ROLES,
    build_resolved_pair_from_registry,
    load_providers_registry,
    _load_shared_role_budgets_from_registry,
)

REGISTRY = Path(__file__).resolve().parents[3] / "configs" / "providers.yaml"

# Content caps immediately before the shared-headroom adjustment. Entity was
# already raised to 20k in the preceding change on this branch.
CONTENT_CAPS = {
    "generator": (70000, None),
    "repair": (16384, 24576),
    "formatting": (8000, 24576),
    "gemma_audit": (4096, None),
    "qwen_audit": (12000, 24576),
    "fidelity_reviewer": (16384, 24576),
    "russian_selector": (1024, None),
    "entity_extractor": (20000, None),
    "russian_editor": (12000, None),
    "glossary_resolver": (4096, None),
}


def test_shared_caps_preserve_content_headroom_for_all_local_aliases():
    registry = load_providers_registry(REGISTRY)
    assert set(registry.role_budgets) == set(CONTENT_CAPS)
    local = registry.providers["local"]
    for role, (old_base, old_ceiling) in CONTENT_CAPS.items():
        aliases = ("gemma", "gemma31") if role in TRANSLATOR_ROLES else ("qwen", "qwen38")
        maximum_reasoning = max(
            local[alias].reasoning_budget + registry.role_budgets[role].reasoning_budget
            for alias in aliases
        )
        budget = registry.role_budgets[role]
        assert budget.max_output_tokens == old_base + maximum_reasoning
        for alias in aliases:
            pair = build_resolved_pair_from_registry(
                registry,
                alias if role in TRANSLATOR_ROLES else "gemma",
                alias if role in REVIEWER_ROLES else "qwen",
            )
            effective = pair.effective_reasoning_budget(role)
            assert budget.derive() - effective >= old_base
            if old_ceiling is not None:
                assert budget.output_budget.ceiling == old_ceiling + maximum_reasoning
                assert budget.derive(item_count=10000, span_tokens=10000) - effective >= old_ceiling


def test_shared_remote_budget_matches_registry_and_preserves_formula_slopes():
    registry = load_providers_registry(REGISTRY)
    remote = _load_shared_role_budgets_from_registry()
    assert remote == registry.role_budgets
    for role, (old_base, old_ceiling) in CONTENT_CAPS.items():
        budget = remote[role]
        assert budget.derive() == budget.max_output_tokens
        if old_ceiling is None:
            continue
        formula = budget.output_budget
        if role == "formatting":
            assert formula.per_span_tokens == 64
            assert budget.derive(span_tokens=1) == budget.max_output_tokens + 64
        else:
            assert formula.per_item_tokens == 128
            assert budget.derive(item_count=1) == budget.max_output_tokens + 128
        assert budget.derive(item_count=10000, span_tokens=10000) == formula.ceiling
