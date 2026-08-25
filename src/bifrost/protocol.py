import base64
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_STUN_URLS = (
    "stun:stun.miwifi.com:3478",
    "stun:stun.chat.bilibili.com:3478",
    "stun:stun.cloudflare.com:3478",
    "stun:stun.l.google.com:19302",
)


def default_config_path(component, config_dir=None):
    """Return the required per-component config path under ~/.config/bifrost."""
    if component not in {"server", "client"}:
        raise ValueError(f"unsupported config component: {component}")
    directory = (
        Path(config_dir).expanduser()
        if config_dir is not None
        else Path("~/.config/bifrost").expanduser()
    )
    path = directory / f"{component}.toml"
    if not path.is_file():
        raise FileNotFoundError(
            f"{component} config not found at {path}; "
            f"create it or pass --config PATH"
        )
    return path


def resolve_config_path(path, component, config_dir=None):
    """Resolve an explicit config path or the component's default path."""
    return (
        Path(path).expanduser()
        if path
        else default_config_path(component, config_dir)
    )


def load_config(path):
    with Path(path).open("rb") as f:
        return tomllib.load(f)


def validate_stun_urls(value, field="webrtc.stun_urls"):
    """Validate a TOML/JSON list of UDP STUN URLs."""
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array of strings")
    if len(value) > 16:
        raise ValueError(f"{field} cannot contain more than 16 entries")

    result = []
    for index, url in enumerate(value):
        if not isinstance(url, str):
            raise TypeError(f"{field}[{index}] must be a string")
        if url != url.strip() or any(char.isspace() for char in url):
            raise ValueError(f"{field}[{index}] is not a valid STUN URL")
        if not url.startswith("stun:"):
            raise ValueError(f"{field}[{index}] must use the stun: scheme")

        try:
            parsed = urlsplit("stun://" + url.removeprefix("stun:"))
            port = parsed.port
            parsed.hostname.encode("ascii")
        except (AttributeError, UnicodeError, ValueError) as exc:
            raise ValueError(f"{field}[{index}] is not a valid STUN URL") from exc
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError(f"{field}[{index}] is not a valid STUN URL")
        if url in result:
            raise ValueError(f"{field} contains a duplicate URL: {url}")
        result.append(url)
    return result


def http_request(request_id, method, path, headers=None, body="", body_base64=None):
    result = {
        "type": "http_request",
        "id": request_id,
        "method": method,
        "path": path or "/",
        "headers": headers or {},
        "body": body,
    }
    if body_base64 is not None:
        result["body_base64"] = body_base64
    return result


def http_response(
    request_id,
    status,
    headers=None,
    body="",
    error=None,
    body_base64=None,
    status_text="",
    response_url="",
):
    result = {
        "type": "http_response",
        "id": request_id,
        "status": status,
        "headers": headers or {},
        "body": body,
    }
    if body_base64 is not None:
        result["body_base64"] = body_base64
    if status_text:
        result["status_text"] = status_text
    if response_url:
        result["response_url"] = response_url
    if error:
        result["error"] = error
    return result


def encode_body(data):
    """Return a JSON-safe base64 representation of arbitrary HTTP bytes."""
    return base64.b64encode(data).decode("ascii")


def decode_body(value):
    """Decode a protocol body, accepting the legacy text field as a fallback."""
    if value is None:
        return b""
    return base64.b64decode(value, validate=True)
