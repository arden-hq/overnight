"""Model provider routes that native connectors and case scripts share.

A connector's `provider` option is either `"openrouter"` or an OpenAI-compatible endpoint the
caller configures at run time:

    {"base_url": "https://api.example.test/v1", "api_key_env": "EXAMPLE_API_KEY",
     "reasoning_effort": "none"}

`api_key_env` names the variable that holds the key; the key itself never enters configuration.
`reasoning_effort`, when set, is sent on every request in place of the harness's own reasoning
controls, for endpoints that serve tool calls only at a fixed effort.
"""

import os

from pydantic import JsonValue, TypeAdapter

from overnight.domain.contracts import CompatibleProvider, Provider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"

__all__ = ["CompatibleProvider", "Provider", "api_key", "endpoint", "provider_record"]


def api_key(provider: CompatibleProvider) -> str:
    key = os.environ.get(provider.api_key_env)
    if key is None:
        raise ValueError(f"set {provider.api_key_env} for the provider at {provider.base_url}")
    return key


provider_contract: TypeAdapter[Provider] = TypeAdapter(Provider)


def endpoint(provider: Provider) -> CompatibleProvider:
    """The endpoint behind a provider option; OpenRouter is a preset compatible endpoint."""
    if provider == "openrouter":
        return CompatibleProvider(base_url=OPENROUTER_BASE_URL, api_key_env=OPENROUTER_API_KEY_ENV)
    return provider


def provider_record(provider: Provider) -> JsonValue:
    """The provider as recorded in model provenance: its name or its non-secret settings."""
    return provider_contract.dump_python(provider, mode="json", exclude_none=True)
