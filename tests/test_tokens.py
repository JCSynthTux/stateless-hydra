"""Unit tests for the deterministic, encrypted NZB download token helpers."""

from __future__ import annotations

import base64
import time

import pytest

from stateless_hydra.tokens import BUCKET_SECONDS, _day_bucket, sign, verify

KEY = "unit-test-signing-key-0123456789abcdef"
INDEXER = "nzbgeek"
GUID = "guid-one"

# An arbitrary fixed instant, so bucket arithmetic is unambiguous.
T = 1_700_000_000.0


def _parts(token: str) -> list[str]:
    """Split a token into its ``[salt, cipher, mac]`` base64url parts."""
    return token.split(".")


def _b64decode(value: str) -> bytes:
    """Decode a (padding-stripped) base64url part."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _flip(value: str) -> str:
    """Return ``value`` with its first character changed."""
    return ("A" if value[0] != "A" else "B") + value[1:]


def test_day_bucket_is_int_of_time_over_bucket_seconds():
    assert _day_bucket(T) == int(T / BUCKET_SECONDS)
    assert isinstance(_day_bucket(T), int)


def test_sign_verify_round_trip():
    token = sign(KEY, INDEXER, GUID, now=T)

    assert verify(KEY, INDEXER, token, now=T) == GUID


def test_token_is_deterministic():
    assert sign(KEY, INDEXER, GUID, now=T) == sign(KEY, INDEXER, GUID, now=T)


def test_token_shape_and_key_is_never_embedded():
    token = sign(KEY, INDEXER, GUID, now=T)

    # Exactly three parts, no colon (so ``split_guid`` stays safe).
    assert token.count(".") == 2
    assert _parts(token) == _parts(token)
    assert ":" not in token
    assert KEY not in token


def test_different_guids_use_different_salt_and_cipher():
    salt_a, cipher_a, _ = _parts(sign(KEY, INDEXER, "guid-one", now=T))
    salt_b, cipher_b, _ = _parts(sign(KEY, INDEXER, "guid-two", now=T))

    assert salt_a != salt_b
    assert cipher_a != cipher_b


def test_token_does_not_contain_guid_plaintext():
    # A URL-shaped guid embeds an indexer API key; base64-decoding the token
    # must not recover any of it.
    guid = "https://api.althub.co.za/getnzb/abc.nzb&i=1&r=SECRETKEY"
    token = sign(KEY, INDEXER, guid, now=T)

    decoded = b"".join(_b64decode(part) for part in _parts(token))

    assert b"SECRETKEY" not in decoded
    assert b"api.althub.co.za" not in decoded
    assert guid.encode() not in decoded


def test_long_guid_round_trip():
    # Longer than one SHA-256 block, so the keystream loop must iterate.
    long_guid = "https://api.example.com/getnzb/name.nzb?" + "a" * 220

    token = sign(KEY, INDEXER, long_guid, now=T)

    assert verify(KEY, INDEXER, token, now=T) == long_guid


def test_wrong_indexer_fails():
    token = sign(KEY, INDEXER, GUID, now=T)

    assert verify(KEY, "slug", token, now=T) is None


def test_tampered_salt_cipher_and_mac_fail():
    salt, cipher, mac = _parts(sign(KEY, INDEXER, GUID, now=T))

    assert verify(KEY, INDEXER, f"{_flip(salt)}.{cipher}.{mac}", now=T) is None
    assert verify(KEY, INDEXER, f"{salt}.{_flip(cipher)}.{mac}", now=T) is None
    assert verify(KEY, INDEXER, f"{salt}.{cipher}.{_flip(mac)}", now=T) is None


def test_previous_bucket_is_accepted():
    token = sign(KEY, INDEXER, GUID, now=T)

    assert verify(KEY, INDEXER, token, now=T + BUCKET_SECONDS) == GUID


def test_older_bucket_is_rejected():
    token = sign(KEY, INDEXER, GUID, now=T)

    assert verify(KEY, INDEXER, token, now=T + 2 * BUCKET_SECONDS) is None


def test_non_token_garbage_is_rejected():
    for garbage in (
        "",
        "not-a-token",
        ".",
        "..",
        "...",
        "a.b",
        "a.b.c.d",
        "no-dot-here",
        "!!!.???.###",
        "=padding=.",
        "payload.",
        ".signature",
    ):
        assert verify(KEY, INDEXER, garbage, now=T) is None


def test_bad_base64_part_is_rejected():
    # A syntactically plausible three-part shape whose salt is not valid base64.
    _, cipher, mac = _parts(sign(KEY, INDEXER, GUID, now=T))

    assert verify(KEY, INDEXER, f"@@@{cipher}.{mac}", now=T) is None


def test_standard_base64_alphabet_is_rejected():
    # ``+`` and ``/`` are accepted by base64.b64decode even with altchars; they
    # are not part of the url-safe alphabet and must be rejected outright.
    token = sign(KEY, INDEXER, GUID, now=T)

    assert verify(KEY, INDEXER, token + "+", now=T) is None
    assert verify(KEY, INDEXER, token + "/", now=T) is None
    assert verify(KEY, INDEXER, "+" + token, now=T) is None
    assert verify(KEY, INDEXER, token.replace(".", "+", 1), now=T) is None


def test_wrong_key_fails():
    token = sign(KEY, INDEXER, GUID, now=T)

    assert verify("another-signing-key-0123456789abcdef", INDEXER, token, now=T) is None


def test_verify_never_raises():
    token = sign(KEY, INDEXER, GUID, now=T)
    candidates = ["", ".", "..", "...", "a.b.c", token, token + "+", "@@@.???.###", "x" * 1000]

    for candidate in candidates:
        for indexer in (INDEXER, "slug"):
            try:
                verify(KEY, indexer, candidate, now=T)
            except Exception as exc:  # noqa: BLE001 - the guarantee is "never raises"
                pytest.fail(f"verify raised for {candidate!r}: {exc!r}")


def test_default_clock_uses_current_time():
    token = sign(KEY, INDEXER, GUID)
    now = time.time()

    assert verify(KEY, INDEXER, token, now=now) == GUID
