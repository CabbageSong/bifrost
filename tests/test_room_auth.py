import pytest

from bifrost.room_auth import (
    create_login_token,
    create_session_token,
    hash_password,
    parse_password_hash,
    session_cookie_name,
    validate_room_name,
    verify_login_token,
    verify_password,
    verify_session_token,
)


def test_scrypt_hash_round_trip_and_validation():
    encoded = hash_password("correct horse", salt=b"s" * 16)

    assert encoded.startswith("$scrypt$v=1$n=32768,r=8,p=1$")
    assert verify_password(encoded, "correct horse")
    assert not verify_password(encoded, "wrong")
    assert len(parse_password_hash(encoded)[1]) == 32
    with pytest.raises(ValueError, match="supported Bifrost scrypt"):
        parse_password_hash("sha256:not-a-password-hash")


def test_session_is_signed_and_bound_to_room_password_and_expiry():
    secret = b"k" * 32
    password_hash = hash_password("room secret", salt=b"a" * 16)
    token, expires = create_session_token(
        secret, "home", password_hash, 300, now=1_000
    )

    assert expires == 1_300
    assert verify_session_token(
        secret, token, "home", password_hash, now=1_100
    ) == expires
    assert verify_session_token(secret, token, "office", password_hash, now=1_100) is None
    assert verify_session_token(secret, token, "home", password_hash + "x", now=1_100) is None
    assert verify_session_token(secret, token + "x", "home", password_hash, now=1_100) is None
    assert verify_session_token(secret, token, "home", password_hash, now=1_300) is None


def test_login_form_token_is_bound_and_short_lived():
    secret = b"l" * 32
    token = create_login_token(secret, "home", "/home/admin", ttl=60, now=100)

    assert verify_login_token(secret, token, "home", "/home/admin", now=120)
    assert not verify_login_token(secret, token, "home", "/home/other", now=120)
    assert not verify_login_token(secret, token, "office", "/home/admin", now=120)
    assert not verify_login_token(secret, token, "home", "/home/admin", now=160)


def test_room_names_and_cookie_names_are_safe():
    assert validate_room_name("home-1.example") == "home-1.example"
    assert session_cookie_name("home").startswith("__Host-bifrost-")
    assert session_cookie_name("home", secure=False).startswith("bifrost-")
    for value in ("", "../home", "home/path", "signal", "房间"):
        with pytest.raises(ValueError):
            validate_room_name(value)
