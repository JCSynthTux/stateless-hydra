"""Deterministic, encrypted NZB download tokens.

Client-facing NZB downloads are addressed by an opaque token appended to the
owning indexer name in the composed guid (``"{indexer}:{token}"``). The token
carries the *original* upstream guid encrypted with a deterministic, keyed
construction -- an HMAC-SHA256-derived keystream plus an HMAC-SHA256 tag over a
rolling daily time bucket. Rendering the same search twice therefore produces
byte-identical tokens and no per-request state is written anywhere.

The guid is encrypted rather than merely encoded: its plaintext never appears
in the token, so a URL-shaped guid (the upstream download URL, which may embed
an indexer API key in a query parameter) is not recoverable by a client that
base64-decodes the token. The signing key is only ever used as HMAC key
material; it never appears in a token (and hence never in a URL or response),
and nothing is stored server-side. The recovered guid is expanded back into the
upstream URL at download time.

A token is valid in its signing bucket and the one before it. Accepting the
previous bucket gives a render issued just before midnight a full day of grace
without keeping an unbounded history.

The token is ``base64url(salt).base64url(cipher).base64url(mac)``: neither the
dot nor the url-safe base64 alphabet contains a colon, so ``split_guid`` (which
splits on the first colon) still recovers the ``(indexer, token)`` pair.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

# Tokens roll over once per day; ``int(time.time() / BUCKET_SECONDS)`` is the
# (UTC) day index, independent of the local timezone.
BUCKET_SECONDS = 86400

_SEPARATOR = "."
_SALT_BYTES = 16
_MAC_BYTES = 16


def _day_bucket(now: float | None = None) -> int:
    """Return the current (UTC) day index, or the one for an explicit ``now``."""
    return int((time.time() if now is None else now) / BUCKET_SECONDS)


def _b64encode(raw: bytes) -> str:
    """Base64url-encode without padding (padding is re-added on decode)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes | None:
    """Base64url-decode strictly, returning ``None`` on any malformed input.

    The standard base64 alphabet's ``+`` and ``/`` are rejected explicitly:
    :func:`base64.b64decode` accepts them even with ``altchars`` and
    ``validate=True``, which would let a non-urlsafe token through.
    """
    if "+" in value or "/" in value:
        return None
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None


def _hmac(key: str, message: bytes) -> bytes:
    """Full HMAC-SHA256 digest of ``message`` under ``key``."""
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()


def _salt(key: str, bucket: int, indexer: str, guid: str) -> bytes:
    """Deterministic per-(bucket, indexer, guid) salt.

    Deriving the salt from the guid means a distinct keystream is used for every
    item, so no two tokens ever reuse the same XOR keystream.
    """
    message = b"salt:" + str(bucket).encode() + b":" + indexer.encode() + b":" + guid.encode()
    return _hmac(key, message)[:_SALT_BYTES]


def _keystream(key: str, salt: bytes, length: int) -> bytes:
    """Variable-length HMAC-based keystream derived only from ``salt``/``key``.

    Never derived from the guid: verification recovers the guid, so it cannot
    depend on the plaintext it is decrypting.
    """
    blocks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        block = _hmac(key, b"enc:" + salt + b":" + str(counter).encode())
        blocks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(blocks)[:length]


def _xor(data: bytes, keystream: bytes) -> bytes:
    """XOR two equal-length byte strings."""
    return bytes(left ^ right for left, right in zip(data, keystream, strict=True))


def _mac(key: str, bucket: int, indexer: str, salt: bytes, cipher: bytes) -> bytes:
    """Authentication tag over the ciphertext and its context.

    Computed over the *encrypted* guid, so the MAC is checked before any
    decryption is attempted.
    """
    message = b"sig:" + str(bucket).encode() + b":" + indexer.encode() + b":" + salt + b":" + cipher
    return _hmac(key, message)[:_MAC_BYTES]


def sign(key: str, indexer: str, guid: str, now: float | None = None) -> str:
    """Return the deterministic token for ``(indexer, guid)``.

    The result is ``base64url(salt) + "." + base64url(cipher) + "." +
    base64url(mac)``. Encrypt-then-MAC: the ciphertext is authenticated by the
    tag so verification can reject forgeries before decrypting.
    """
    bucket = _day_bucket(now)
    guid_bytes = guid.encode("utf-8")
    salt = _salt(key, bucket, indexer, guid)
    cipher = _xor(guid_bytes, _keystream(key, salt, len(guid_bytes)))
    mac = _mac(key, bucket, indexer, salt, cipher)
    return f"{_b64encode(salt)}{_SEPARATOR}{_b64encode(cipher)}{_SEPARATOR}{_b64encode(mac)}"


def verify(key: str, indexer: str, token: str, now: float | None = None) -> str | None:
    """Verify ``token`` for ``indexer``, returning the recovered guid or ``None``.

    The MAC is recomputed for the current and the previous bucket and compared
    in constant time; the guid is decrypted only after a MAC match. Any
    malformed shape, bad/non-urlsafe base64, truncated part or MAC mismatch
    yields ``None`` (never an exception), so callers can fall back to the legacy
    stored-token lookup.
    """
    salt_part, separator, remainder = token.partition(_SEPARATOR)
    if not separator:
        return None
    cipher_part, separator, mac_part = remainder.partition(_SEPARATOR)
    if not separator or not salt_part or not mac_part:
        return None
    # A fourth dot would mean an unexpected shape; reject it.
    if _SEPARATOR in mac_part:
        return None

    salt = _b64decode(salt_part)
    cipher = _b64decode(cipher_part)
    mac = _b64decode(mac_part)
    if salt is None or cipher is None or mac is None:
        return None

    current = _day_bucket(now)
    for bucket in (current, current - 1):
        if hmac.compare_digest(mac, _mac(key, bucket, indexer, salt, cipher)):
            guid_bytes = _xor(cipher, _keystream(key, salt, len(cipher)))
            try:
                return guid_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None
