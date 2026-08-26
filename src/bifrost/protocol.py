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
        if any(char.isspace() for char in url):
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
        if not isinstance(server, dict):
            raise TypeError(f"{item_field} must be a table")
        unknown = set(server) - {"urls", "username", "credential"}
        if unknown:
            name = min(unknown)
            raise ValueError(f"{item_field} contains unsupported field: {name}")

        raw_urls = server.get("urls")
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

        username = server.get("username")
        credential = server.get("credential")
        if has_turn:
            if not isinstance(username, str) or not username:
                raise ValueError(f"{item_field}.username is required for TURN")
            if not isinstance(credential, str) or not credential:
                raise ValueError(f"{item_field}.credential is required for TURN")
        elif username is not None or credential is not None:
            raise ValueError(f"{item_field} credentials require a TURN URL")

        normalized_server = {"urls": normalized_urls}
        if has_turn:
            normalized_server.update({"username": username, "credential": credential})
        result.append(normalized_server)
    return result


def webrtc_ice_servers(config, field="webrtc"):
    """Read new ICE server tables or convert the legacy STUN URL list."""
    if not isinstance(config, dict):
        raise TypeError(f"{field} must be a table")
    if "ice_servers" in config and "stun_urls" in config:
        raise ValueError(f"{field} cannot contain both ice_servers and stun_urls")
    if "ice_servers" in config:
        return validate_ice_servers(config["ice_servers"], f"{field}.ice_servers")
    stun_urls = validate_stun_urls(config.get("stun_urls", []), f"{field}.stun_urls")
    return [{"urls": stun_urls}] if stun_urls else []


def legacy_stun_urls(ice_servers):
    """Extract URLs an older STUN-only client can still consume."""
    return [
        url
        for server in ice_servers
        for url in server["urls"]
        if url.startswith("stun:")
    ]


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
