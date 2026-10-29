"""Адаптеры провайдеров: нейтральный формат слоя llm <-> формат API провайдера."""
from tb.llm.adapters.anthropic import AnthropicAdapter
from tb.llm.adapters.deepseek import DeepSeekAdapter
from tb.llm.adapters.openai_compat import OpenAICompat
from tb.llm.types import LLMError

ADAPTERS = {"deepseek": DeepSeekAdapter, "anthropic": AnthropicAdapter, "openai-compatible": OpenAICompat}


def make(provider, model, transport=None, environ=None):
    cls = ADAPTERS.get(provider.adapter)
    if cls is None:
        raise LLMError("config", "неизвестный адаптер")
    return cls(provider, model, transport=transport, environ=environ)
