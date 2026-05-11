"""Unit tests cho preview_deploys.slugify_branch — Phase 2 P2.1."""
from app.services.preview_deploys import slugify_branch


def test_basic_branch_name():
    assert slugify_branch("main") == "main"
    assert slugify_branch("develop") == "develop"


def test_feature_branch_with_slash():
    assert slugify_branch("feature/stripe-checkout") == "feature-stripe-checkout"
    assert slugify_branch("hotfix/login-bug") == "hotfix-login-bug"


def test_release_branch_with_dots():
    assert slugify_branch("release/v1.2.0") == "release-v1-2-0"


def test_uppercase_normalized():
    assert slugify_branch("FIX-Bug#123") == "fix-bug-123"
    assert slugify_branch("UPPERCASE") == "uppercase"


def test_starts_with_non_letter_gets_prefix():
    """Branch starting with digit gets 'p-' prefix (DNS-safe).

    Leading dashes get stripped first; if result still starts with non-letter,
    prefix added.
    """
    assert slugify_branch("123-feature") == "p-123-feature"
    # Leading dash stripped → starts with 'l' (letter) → no prefix needed
    assert slugify_branch("-leading-dash") == "leading-dash"


def test_special_chars_replaced():
    assert slugify_branch("feature@2024") == "feature-2024"
    assert slugify_branch("a/b/c") == "a-b-c"


def test_multiple_dashes_collapsed():
    assert slugify_branch("feature---ABC") == "feature-abc"


def test_max_length_63():
    long_branch = "feature/" + "a" * 100
    result = slugify_branch(long_branch)
    assert len(result) <= 63


def test_empty_input():
    # Empty or all special chars → fallback "p-"
    result = slugify_branch("")
    assert result.startswith("p-") or len(result) == 0 or result == "p-"


def test_idempotent():
    """Slugifying an already-slugged branch should be stable."""
    s1 = slugify_branch("feature/auth-flow")
    s2 = slugify_branch(s1)
    assert s1 == s2
