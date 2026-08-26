"""Ed25519 challenge-response authentication for Bifrost agents."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_AUTH_DOMAIN = b"bifrost-agent-auth-v2\0"
_KEY_TYPE = "ssh-ed25519"
_ROOM_OPTION = re.compile(r'(?:^|,)\s*room\s*=\s*(?:"([^"]+)"|([^,\s]+))')


@dataclass(frozen=True)
class AuthorizedKey:
    public_key: Ed25519PublicKey
    rooms: frozenset[str] | None
    fingerprint: str

    def permits(self, room: str) -> bool:
        return self.rooms is None or room in self.rooms


def public_key_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def public_key_text(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")


def fingerprint(public_key: Ed25519PublicKey) -> str:
    digest = hashlib.sha256(public_key_bytes(public_key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def auth_payload(room: str, challenge: bytes, password_hash: str) -> bytes:
    """Build a signed agent registration, including the room access policy."""
    room_bytes = room.encode("utf-8")
    password_hash_bytes = password_hash.encode("ascii")
    if len(room_bytes) > 65535:
        raise ValueError("room name is too long")
    if len(password_hash_bytes) > 65535:
        raise ValueError("room password hash is too long")
    if len(challenge) != 32:
        raise ValueError("authentication challenge must be 32 bytes")
    return (
        _AUTH_DOMAIN
        + len(room_bytes).to_bytes(2, "big")
        + room_bytes
        + len(password_hash_bytes).to_bytes(2, "big")
        + password_hash_bytes
        + challenge
    )


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    data = Path(path).expanduser().read_bytes()
    errors: list[Exception] = []
    for loader in (serialization.load_ssh_private_key, serialization.load_pem_private_key):
        try:
            key = loader(data, password=None)
            break
        except (TypeError, ValueError) as exc:
            errors.append(exc)
    else:
        raise ValueError(f"could not load private key {path!s}") from errors[-1]
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"private key {path!s} is not an Ed25519 key")
    return key


def parse_public_key(text: str) -> Ed25519PublicKey:
    fields = text.strip().split()
    try:
        index = fields.index(_KEY_TYPE)
        encoded = fields[index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("expected an ssh-ed25519 public key") from exc
    try:
        key = serialization.load_ssh_public_key(f"{_KEY_TYPE} {encoded}".encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("invalid ssh-ed25519 public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("public key is not Ed25519")
    return key


def _authorized_key_lines(source: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(source, (list, tuple)):
        return tuple(
            line for item in source for line in str(item).splitlines()
        )
    return tuple(str(source).splitlines())


def load_authorized_keys(
    source: str | list[str] | tuple[str, ...],
) -> dict[bytes, AuthorizedKey]:
    """Load inline OpenSSH-style authorized key lines from configuration."""
    result: dict[bytes, AuthorizedKey] = {}
    for line_number, raw_line in enumerate(_authorized_key_lines(source), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        try:
            key_index = fields.index(_KEY_TYPE)
        except ValueError as exc:
            raise ValueError(
                f"public_keys entry {line_number}: only ssh-ed25519 keys are supported"
            ) from exc
        key = parse_public_key(" ".join(fields[key_index:key_index + 2]))
        raw = public_key_bytes(key)
        prefix = " ".join(fields[:key_index])
        room_match = _ROOM_OPTION.search(prefix)
        rooms = frozenset((room_match.group(1) or room_match.group(2),)) if room_match else None

        previous = result.get(raw)
        if previous is not None:
            if previous.rooms is None or rooms is None:
                rooms = None
            else:
                rooms = previous.rooms | rooms
        result[raw] = AuthorizedKey(key, rooms, fingerprint(key))
    if not result:
        raise ValueError("public_keys contains no Ed25519 keys")
    return result


def verify_signature(
    entry: AuthorizedKey,
    room: str,
    challenge: bytes,
    signature: bytes,
    password_hash: str,
) -> bool:
    if not entry.permits(room):
        return False
    try:
        entry.public_key.verify(
            signature, auth_payload(room, challenge, password_hash)
        )
    except (InvalidSignature, UnicodeEncodeError, ValueError):
        return False
    return True
