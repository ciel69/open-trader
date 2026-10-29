"""Адаптер DeepSeek: chat-completions + особенности (WP11-C1; детали не подтверждены, см. docs/ref/deepseek_api.md п. 8).

- usage: prompt_cache_hit_tokens / prompt_cache_miss_tokens (кэш-попадания), completion_tokens_details.reasoning_tokens;
- thinking: поля из thinking_map модели (по умолчанию thinking{type}, reasoning_effort); при включённом thinking temperature не шлётся;
- reasoning_content возвращается в следующих запросах С инструментами (иначе HTTP 400), без инструментов не нужен;
- strict-инструменты уходят на beta_url (только схемы, прошедшие strict_compatible);
- список моделей GET {base_url}/models.
"""
from tb.llm.adapters.openai_compat import OpenAICompat
from tb.llm.types import Usage


class DeepSeekAdapter(OpenAICompat):
    kind = "deepseek"

    def parse_usage(self, u):
        if not isinstance(u, dict):
            return Usage()
        if "prompt_cache_hit_tokens" in u or "prompt_cache_miss_tokens" in u:
            hit, miss = int(u.get("prompt_cache_hit_tokens") or 0), int(u.get("prompt_cache_miss_tokens") or 0)
            pt = int(u.get("prompt_tokens") or 0)
            if pt > hit + miss:
                miss += pt - hit - miss      # поля кэша не покрывают весь промпт: остаток считаем промахом
            cdet = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
            return Usage(miss=miss, hit=hit, out=int(u.get("completion_tokens") or 0), reasoning=int(cdet.get("reasoning_tokens") or 0))
        return OpenAICompat.parse_usage(self, u)
