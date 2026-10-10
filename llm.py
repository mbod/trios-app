

from langchain.chat_models import init_chat_model

from config import ModelSpec
import os

# map provider names to LangChain names
PROVIDERS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google_genai": "google_genai",
    "ollama": "ollama",
    "openai_compatible": "openai"
}


def build_chat_model(spec: ModelSpec):
    kwargs = {}
    if spec.provider != "ollama":
        kwargs.update(timeout=spec.timeout, max_retries=spec.max_retries)
        
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature

    if spec.max_tokens is not None:
        kwargs["max_tokens"] = spec.max_tokens

    if spec.provider == "openai_compatible":
        kwargs["base_url"] = spec.base_url
        kwargs["api_key"] = os.environ[spec.api_key_env] if spec.api_key_env else "not-needed"
    elif spec.provider == "ollama" and spec.base_url:
        kwargs["base_url"] = spec.base_url

    kwargs.update(spec.params)
    return init_chat_model(spec.model, model_provider=PROVIDERS[spec.provider], **kwargs)


def structured(chat_model, schema, spec: ModelSpec):
    """
    Wrap a model so it returns a validated instance of the `schema` (in schemas.py)
    """
    kwargs = {"include_raw": True}
    if spec.structured_output:
        kwargs["method"] = spec.structured_output

    return chat_model.with_structured_output(schema, **kwargs)



