"""
Twilio helpers for gptme-voice.
"""

from dataclasses import dataclass, field
from html import escape as _html_escape
from urllib.parse import urlsplit, urlunsplit

from gptme.config import get_config

_DEFAULT_STREAM_PATH = "/twilio"
_PUBLIC_BASE_URL_ENV_NAMES = (
    "GPTME_VOICE_PUBLIC_BASE_URL",
    "TWILIO_PUBLIC_BASE_URL",
)


class ConfigurationError(ValueError):
    """Raised when required Twilio settings are missing."""


@dataclass(frozen=True)
class OutboundCallSettings:
    """Resolved settings for placing an outbound Twilio call."""

    account_sid: str
    auth_token: str
    from_number: str
    stream_url: str
    custom_params: dict[str, str] | None = field(default=None, hash=False)


def _get_config_env(name: str) -> str | None:
    """Resolve a setting from env or gptme config."""
    return get_config().get_env(name)


def _require_config_value(name: str) -> str:
    value = _get_config_env(name)
    if value:
        return value
    raise ConfigurationError(
        f"{name} not configured. Set it in the environment or gptme config."
    )


def _resolve_public_base_url(explicit_public_base_url: str | None) -> str:
    if explicit_public_base_url:
        return explicit_public_base_url

    for name in _PUBLIC_BASE_URL_ENV_NAMES:
        value = _get_config_env(name)
        if value:
            return value

    names = ", ".join(_PUBLIC_BASE_URL_ENV_NAMES)
    raise ConfigurationError(f"Public voice URL not configured. Set one of: {names}.")


def build_stream_url(
    public_base_url: str, stream_path: str = _DEFAULT_STREAM_PATH
) -> str:
    """Convert a public base URL into the Twilio Media Streams WebSocket URL."""
    candidate = public_base_url.strip()
    if not candidate:
        raise ValueError("Public base URL cannot be empty.")

    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlsplit(candidate)
    if not parsed.netloc:
        raise ValueError(f"Invalid public base URL: {public_base_url}")

    scheme_map = {
        "http": "ws",
        "https": "wss",
        "ws": "ws",
        "wss": "wss",
    }
    scheme = scheme_map.get(parsed.scheme.lower())
    if not scheme:
        raise ValueError("Public base URL must use http, https, ws, or wss.")

    prefix = parsed.path.rstrip("/")
    suffix = "/" + stream_path.lstrip("/")
    path = f"{prefix}{suffix}" if prefix else suffix
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def build_connect_stream_twiml(
    stream_url: str, custom_params: dict[str, str] | None = None
) -> str:
    """Build the TwiML needed to attach a call to a Media Stream.

    custom_params are forwarded to the WebSocket handler via Twilio's
    <Parameter> elements, arriving in the "start.customParameters" payload.
    """
    safe_url = _html_escape(stream_url, quote=True)
    param_lines = ""
    if custom_params:
        for key, value in custom_params.items():
            safe_key = _html_escape(key, quote=True)
            safe_value = _html_escape(value, quote=True)
            param_lines += (
                f'\n            <Parameter name="{safe_key}" value="{safe_value}" />'
            )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{safe_url}">{param_lines}
        </Stream>
    </Connect>
</Response>"""


def resolve_outbound_call_settings(
    *,
    from_number: str | None = None,
    public_base_url: str | None = None,
    custom_params: dict[str, str] | None = None,
) -> OutboundCallSettings:
    """Resolve Twilio credentials and stream target for outbound calls."""
    resolved_from_number = from_number or _require_config_value("TWILIO_PHONE_NUMBER")
    resolved_public_base_url = _resolve_public_base_url(public_base_url)

    return OutboundCallSettings(
        account_sid=_require_config_value("TWILIO_ACCOUNT_SID"),
        auth_token=_require_config_value("TWILIO_AUTH_TOKEN"),
        from_number=resolved_from_number,
        stream_url=build_stream_url(resolved_public_base_url),
        custom_params=custom_params,
    )


def create_outbound_call(
    to_number: str,
    settings: OutboundCallSettings,
    *,
    client_cls=None,
) -> str:
    """Place an outbound call that streams audio into the voice server."""
    if client_cls is None:
        try:
            from twilio.rest import Client as client_cls
        except ImportError as exc:
            raise RuntimeError(
                "twilio dependency not installed. Install gptme-voice with its dependencies."
            ) from exc

    client = client_cls(settings.account_sid, settings.auth_token)
    call = client.calls.create(
        to=to_number,
        from_=settings.from_number,
        twiml=build_connect_stream_twiml(settings.stream_url, settings.custom_params),
    )
    return call.sid
