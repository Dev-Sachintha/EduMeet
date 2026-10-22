"""E-mail rules (no database, no internet needed)."""
from backend.emailcheck import builtin_domains, canonical_email, domain_parents, is_blocked


def test_aliases_are_the_same_address():
    assert canonical_email("Jo.Hn+school@Gmail.com") == "john@gmail.com"
    assert canonical_email("john@googlemail.com") == "john@gmail.com"
    assert canonical_email("a.b+x@school.edu") == "a.b@school.edu"      # dots only ignored for Gmail


def test_disposable_domains_are_blocked():
    blocked = builtin_domains()
    assert "mailinator.com" in blocked and "yopmail.com" in blocked
    assert is_blocked("sub.mailinator.com", blocked)                    # sub-domains too
    assert not is_blocked("gmail.com", blocked)
    assert not is_blocked("myschool.edu", blocked)


def test_domain_parents():
    assert domain_parents("a.b.c.com") == ["a.b.c.com", "b.c.com", "c.com"]
    assert domain_parents("com") == []
