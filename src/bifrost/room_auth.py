"""Password hashing and signed browser sessions for protected rooms."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import time

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_LOGIN_FORM_DOMAIN = b"bifrost-room-login-form-v1\0"
_SESSION_DOMAIN = b"bifrost-room-session-v1\0"
_ROOM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_RESERVED_ROOMS = frozenset({"signal", "server-healthz"})


def validate_room_name(room: str) -> str:
    """Validate room names used in public URL path segments."""
    if not isinstance(room, str) or not _ROOM_NAME.fullmatch(room):
        raise ValueError(
            "room must be 1-64 ASCII letters, digits, dots, underscores, or hyphens"
        )
    if room in _RESERVED_ROOMS:
        raise ValueError(f"room name is reserved: {room}")
    return room


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64 in password hash") from exc


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_HASH_BYTES,
        maxmem=_SCRYPT_MAXMEM,
    )


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a self-contained scrypt password hash suitable for TOML."""
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    if not password:
        raise ValueError("an empty password means an unprotected room")
    salt = os.urandom(_SALT_BYTES) if salt is None else salt
    if len(salt) != _SALT_BYTES:
        raise ValueError(f"password salt must be {_SALT_BYTES} bytes")
    digest = _derive(password, salt)
    return (
        f"$scrypt$v=1$n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P}"
        f"${_b64encode(salt)}${_b64encode(digest)}"
    )


def parse_password_hash(encoded: str) -> tuple[bytes, bytes]:
    """Validate an encoded hash and return its salt and derived key."""
    if not isinstance(encoded, str):
        raise TypeError("password_hash must be a string")
    fields = encoded.split("$")
    expected_parameters = f"n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P}"
    if (
        len(fields) != 6
        or fields[:4] != ["", "scrypt", "v=1", expected_parameters]
    ):
        raise ValueError("password_hash is not a supported Bifrost scrypt hash")
    salt, digest = _b64decode(fields[4]), _b64decode(fields[5])
    if len(salt) != _SALT_BYTES or len(digest) != _HASH_BYTES:
        raise ValueError("password_hash has an invalid salt or digest length")
    return salt, digest


def verify_password(encoded: str, password: str) -> bool:
    """Verify a password without leaking comparison timing."""
    try:
        salt, expected = parse_password_hash(encoded)
        actual = _derive(password, salt)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def credential_id(encoded: str) -> str:
    """Return a non-secret identifier that changes when a room password changes."""
    return _b64encode(hashlib.sha256(encoded.encode("utf-8")).digest()[:18])


def session_cookie_name(room: str, *, secure: bool = True) -> str:
    """Use one host-only cookie per room so several rooms can stay logged in."""
    room_id = hashlib.sha256(room.encode("utf-8")).hexdigest()[:24]
    prefix = "__Host-bifrost-" if secure else "bifrost-"
    return prefix + room_id


def create_login_token(
    secret: bytes,
    room: str,
    next_url: str,
    ttl: int = 10 * 60,
    *,
    now: float | None = None,
) -> str:
    """Create a short-lived token binding a login form to its room and return URL."""
    expires = int(time.time() if now is None else now) + ttl
    payload = json.dumps(
        {"r": room, "n": next_url, "e": expires},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    encoded_payload = _b64encode(payload)
    signature = hmac.new(
        secret, _LOGIN_FORM_DOMAIN + encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{_b64encode(signature)}"


def verify_login_token(
    secret: bytes,
    token: str,
    room: str,
    next_url: str,
    *,
    now: float | None = None,
) -> bool:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        signature = _b64decode(encoded_signature)
        expected_signature = hmac.new(
            secret,
            _LOGIN_FORM_DOMAIN + encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            return False
        payload = json.loads(_b64decode(encoded_payload))
        expires = payload["e"]
        current_time = int(time.time() if now is None else now)
        return (
            isinstance(expires, int)
            and expires > current_time
            and payload.get("r") == room
            and payload.get("n") == next_url
        )
    except (KeyError, TypeError, ValueError, UnicodeEncodeError, json.JSONDecodeError):
        return False


def create_session_token(
    secret: bytes,
    room: str,
    password_hash: str,
    ttl: int,
    *,
    now: float | None = None,
) -> tuple[str, int]:
    expires = int(time.time() if now is None else now) + ttl
    payload = json.dumps(
        {"r": room, "c": credential_id(password_hash), "e": expires},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    encoded_payload = _b64encode(payload)
    signature = hmac.new(
        secret, _SESSION_DOMAIN + encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{_b64encode(signature)}", expires


def verify_session_token(
    secret: bytes,
    token: str,
    room: str,
    password_hash: str,
    *,
    now: float | None = None,
) -> int | None:
    """Return the expiry timestamp for a valid room-bound session."""
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        signature = _b64decode(encoded_signature)
        expected_signature = hmac.new(
            secret,
            _SESSION_DOMAIN + encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            return None
        payload = json.loads(_b64decode(encoded_payload))
        expires = payload["e"]
        current_time = int(time.time() if now is None else now)
        if (
            not isinstance(expires, int)
            or expires <= current_time
            or payload.get("r") != room
            or payload.get("c") != credential_id(password_hash)
        ):
            return None
        return expires
    except (KeyError, TypeError, ValueError, UnicodeEncodeError, json.JSONDecodeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a scrypt password hash for a Bifrost room"
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read one password line from stdin instead of prompting",
    )
    args = parser.parse_args()
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Room password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if not hmac.compare_digest(password, confirmation):
            parser.error("passwords do not match")
    if not password:
        parser.error(
            "password cannot be empty; use password_hash = \"\" for a public room"
        )
    print(hash_password(password))


if __name__ == "__main__":
    main()
