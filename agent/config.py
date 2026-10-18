"""Assistant settings use the API's existing settings and environment boundary.

No SDK objects or credentials are loaded here. Readiness can read the configured
identity without enabling generation. ASSISTANT_VERSION is a release label, not
a provider model default (09-CONTEXT D-28).
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from api import config as api_config
from data.config import ConfigError

ASSISTANT_VERSION = "assistant-5"


@dataclass(frozen=True)
class Settings:
    model_id: str
    location: str
    project: str | None
    max_output_tokens: int
    max_request_tokens: int
    max_tool_calls: int
    max_llm_calls: int
    seconds: float


def _text(name: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name}: must be nonempty text")
    return value.strip()


def _positive(name: str, value, *, integer=True, ceiling=None):
    valid = (isinstance(value, int if integer else (int, float))
             and not isinstance(value, bool) and value > 0
             and (isinstance(value, int) or math.isfinite(value)))
    if not valid or (ceiling is not None and value > ceiling):
        bound = f" at most {ceiling}" if ceiling is not None else ""
        raise ConfigError(f"settings.assistant.{name}: must be a positive finite "
                          f"{'whole number' if integer else 'number'}{bound}")
    return value


def load() -> Settings:
    """Validate required values without a second settings store or provider ping."""
    try:
        s = api_config.SETTINGS["assistant"]
        model = _text("GEMINI_MODEL", api_config.env("GEMINI_MODEL", s["model_id"]))
        location = _text("VERTEX_LOCATION", api_config.env("VERTEX_LOCATION", s["location"]))
        output = _positive("max_output_tokens", s["max_output_tokens"])
        budget = _positive("max_request_tokens", s["max_request_tokens"])
        if output > budget:
            raise ConfigError("settings.assistant.max_output_tokens: exceeds max_request_tokens")
        return Settings(model, location, api_config.env("PROJECT") or api_config.env("GOOGLE_CLOUD_PROJECT") or None,
                        output, budget,
                        _positive("max_tool_calls", s["max_tool_calls"], ceiling=8),
                        _positive("max_llm_calls", s["max_llm_calls"], ceiling=10),
                        _positive("seconds", s["seconds"], integer=False, ceiling=40))
    except KeyError as exc:
        raise ConfigError(f"settings.assistant: required value {exc.args[0]!r} is missing") from None


def model_id() -> str:
    return load().model_id


def location() -> str:
    return load().location


def baseline_matches(output: dict, ctx: dict) -> bool:
    """Release-job reuse is stricter than display freshness (D-28).

    CURRENT represents checked text; admission additionally verifies its terminal
    usage record. A release revision alone never invalidates visible live text.
    """
    return (output["status"] == "CURRENT" and output["provenance"] == "PRECOMPUTED"
            and all(output.get(key) == ctx.get(key) for key in
                    ("case_id", "rec_version", "plan_key", "model_id", "assistant_version")))
