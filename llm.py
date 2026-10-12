
import os
from langchain.chat_models import init_chat_model

from config import AgentSettings


PROVIDER_DEFAULTS = {
    "ollama": {
        "num_ctx": 16384         # extend Ollama default context
    }
}

STRUCTURED_METHOD = {            
    "google_genai": "json_schema"  # native structured output for Google models
}



def build_chat_model(settings: AgentSettings):

    provider, _, name = settings.model.partition(":")
    kwargs = dict(PROVIDER_DEFAULTS.get(provider, {}))
    
    if provider != "ollama":
        kwargs["max_retries"] = 0
        
    if settings.temperature is not None:
        kwargs["temperature"] = settings.temperature

    if settings.base_url:
        kwargs["base_url"] = settings.base_url
        if provider == "openai":               # e.g. vLLM openai compatible provider
            kwargs["api_key"] = os.environ.get("OPENAI_COMPATIBLE_KEY", "not-needed")

    kwargs.update(settings.model_settings)

    
    return init_chat_model(name, model_provider=provider, **kwargs)


def structured(chat_model, schema, provider: str):
    kwargs = {"include_raw": True}
    if provider in STRUCTURED_METHOD:
        kwargs["method"] = STRUCTURED_METHOD[provider]

    return chat_model.with_structured_output(schema, **kwargs)



