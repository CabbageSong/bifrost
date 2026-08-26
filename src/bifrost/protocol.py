import base64
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


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


def validate_config_table(value, field, required, optional=()):
    """Require an exact set of explicit keys in a configuration table."""
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be a table")
    required = set(required)
    allowed = required | set(optional)
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"{field} is missing required field: {missing[0]}")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{field} contains unsupported field: {unknown[0]}")
    return value


def _validate_ice_url(url, field):
    if not isinstance(url, str):
        raise TypeError(f"{field} must be a string")
    if any(char.isspace() for char in url):
        raise ValueError(f"{field} is not a valid ICE URL")

    scheme, separator, remainder = url.partition(":")
    if separator != ":" or scheme not in {"stun", "stuns", "turn", "turns"}:
        raise ValueError(f"{field} must use stun:, stuns:, turn:, or turns:")
    try:
        parsed = urlsplit(f"{scheme}://{remainder}")
        port = parsed.port
        parsed.hostname.encode("ascii")
    except (AttributeError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{field} is not a valid ICE URL") from exc

    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(f"{field} is not a valid ICE URL")
    if scheme in {"stun", "stuns"} and parsed.query:
        raise ValueError(f"{field} cannot specify a transport for STUN")
    if scheme in {"turn", "turns"}:
        allowed_queries = {"", "transport=tcp"}
        if scheme == "turn":
            allowed_queries.add("transport=udp")
        if parsed.query not in allowed_queries:
            raise ValueError(f"{field} has an unsupported TURN transport")
    return url


def validate_ice_servers(value, field="webrtc.ice_servers"):
    """Validate and normalize browser/aiortc RTCIceServer dictionaries."""
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array of tables")
    if len(value) > 16:
        raise ValueError(f"{field} cannot contain more than 16 entries")

    result = []
    seen_urls = set()
    for index, server in enumerate(value):
        item_field = f"{field}[{index}]"
        validate_config_table(
            server, item_field, {"urls"}, {"username", "credential"}
        )

        raw_urls = server["urls"]
        urls = [raw_urls] if isinstance(raw_urls, str) else raw_urls
        if not isinstance(urls, list) or not urls:
            raise TypeError(f"{item_field}.urls must be a string or non-empty array")
        if len(urls) > 16:
            raise ValueError(f"{item_field}.urls cannot contain more than 16 entries")
        normalized_urls = []
        has_turn = False
        for url_index, url in enumerate(urls):
            url_field = f"{item_field}.urls[{url_index}]"
            normalized = _validate_ice_url(url, url_field)
            if normalized in seen_urls:
                raise ValueError(f"{field} contains a duplicate URL: {normalized}")
            seen_urls.add(normalized)
            normalized_urls.append(normalized)
            has_turn = has_turn or normalized.startswith(("turn:", "turns:"))

        if has_turn:
            missing = {"username", "credential"} - set(server)
            if missing:
                raise ValueError(
                    f"{item_field} is missing required TURN field: {min(missing)}"
                )
            username = server["username"]
            credential = server["credential"]
            if not isinstance(username, str) or not isinstance(credential, str):
                raise TypeError(
                    f"{item_field} username and credential must be strings"
                )
            if not username:
                raise ValueError(f"{item_field}.username is required for TURN")
            if not credential:
                raise ValueError(f"{item_field}.credential is required for TURN")
        elif "username" in server or "credential" in server:
            raise ValueError(f"{item_field} credentials are only valid for TURN")

        normalized_server = {"urls": normalized_urls}
        if has_turn:
            normalized_server.update({
                "username": username,
                "credential": credential,
            })
        result.append(normalized_server)
    return result


def validate_ice_port(value, field="ice_port"):
    """Validate a fixed local UDP port used for one agent ICE transport."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    if not 1 <= value <= 65535:
        raise ValueError(f"{field} must be between 1 and 65535")
    return value


def http_response(
    request_id,
    status,
    headers,
    body_base64,
    error=None,
    status_text="",
    response_url="",
):
    result = {
        "type": "http_response",
        "id": request_id,
        "status": status,
        "headers": headers,
        "body_base64": body_base64,
    }
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
    """Decode a protocol body from its base64 representation."""
    return base64.b64decode(value, validate=True)
