from __future__ import annotations

from takuro_collector.secrets import protect, unprotect


def test_secret_roundtrip():
    value = "a" * 64
    encoded = protect(value)
    assert encoded != value
    assert unprotect(encoded) == value
