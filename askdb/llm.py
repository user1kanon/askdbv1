"""LLM access: protocol, Anthropic implementation, per-question call cap, and a test stub."""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

MAX_OUTPUT_TOKENS = 16_000  # thinking is always on for the default model and counts against this


class LLMError(Exception):
    """The model could not be used. `message` is safe to show the user."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class LLMResult:
    data: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def complete_json(self, system: list[str], user: str, schema: dict[str, Any]) -> LLMResult:
        """One call returning a JSON object that matches `schema`.

        `system` is a list of text blocks; the last one is the large, stable one (schema and
        glossary) and is marked for prompt caching.
        """
        ...


class AnthropicLLM:
    """Claude via the Messages API with structured JSON output.

    Retries once with a pause on rate limits, overload and connection errors, then raises
    LLMError. Never returns a partial answer.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        backoff_s: float = 2.0,
        client: Any = None,
    ):
        # SDK retries are off: this class owns the single documented retry.
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=0)
        self._model = model
        self._backoff_s = backoff_s

    def complete_json(self, system: list[str], user: str, schema: dict[str, Any]) -> LLMResult:
        blocks = [{"type": "text", "text": text} for text in system]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        request = {
            "model": self._model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": blocks,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": "medium",
                "format": {"type": "json_schema", "schema": schema},
            },
            # Safety classifiers can decline a request; let the API re-run it on a fallback model.
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        response = self._create_with_retry(request)
        if response.stop_reason == "refusal":
            raise LLMError("The model declined to answer this question. Try rephrasing it.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The model ran out of room before finishing. Try a simpler question.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("The model returned an unreadable answer. Try asking again.") from exc
        if not isinstance(data, dict):
            raise LLMError("The model returned an unreadable answer. Try asking again.")
        usage = response.usage
        return LLMResult(data, usage.input_tokens, usage.output_tokens)

    def _create_with_retry(self, request: dict[str, Any]) -> Any:
        retryable = (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        )
        for attempt in (1, 2):
            try:
                return self._client.beta.messages.create(**request)
            except retryable as exc:
                if attempt == 1:
                    time.sleep(self._backoff_s)
                    continue
                raise LLMError(
                    "The AI service is busy or unreachable right now. Please try again shortly."
                ) from exc
            except anthropic.AuthenticationError as exc:
                raise LLMError(
                    "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY."
                ) from exc
            except anthropic.APIStatusError as exc:
                raise LLMError(f"The AI service returned an error ({exc.status_code}).") from exc
        raise AssertionError("unreachable")


class BudgetedLLM:
    """Wraps a client for one question: at most `max_calls` calls, then LLMError."""

    def __init__(self, inner: LLMClient, max_calls: int):
        self._inner = inner
        self._max_calls = max_calls
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def complete_json(self, system: list[str], user: str, schema: dict[str, Any]) -> LLMResult:
        if self.calls >= self._max_calls:
            raise LLMError("This question needed too many AI calls. Try a simpler question.")
        self.calls += 1
        result = self._inner.complete_json(system, user, schema)
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        return result


@dataclass
class StubCall:
    system: list[str]
    user: str
    schema: dict[str, Any]


@dataclass
class StubLLM:
    """Test double: returns queued responses in order; an Exception in the queue is raised."""

    responses: list[dict[str, Any] | Exception] = field(default_factory=list)
    calls: list[StubCall] = field(default_factory=list)

    def complete_json(self, system: list[str], user: str, schema: dict[str, Any]) -> LLMResult:
        self.calls.append(StubCall(system, user, schema))
        if not self.responses:
            raise AssertionError("StubLLM has no response queued")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(item, input_tokens=1, output_tokens=1)
