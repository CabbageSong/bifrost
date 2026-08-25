import base64
import tomllib
from pathlib import Path


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
