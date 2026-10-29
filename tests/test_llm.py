import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from askdb.llm import AnthropicLLM, BudgetedLLM, LLMError, StubLLM

SCHEMA = {"type": "object"}


def reply(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


class FakeClient:
    """Stands in for anthropic.Anthropic: beta.messages.create pops queued replies or errors."""

    def __init__(self, *queue):
        self.queue = list(queue)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls("boom", response=response, body=None)


def make(client, **kw):
    return AnthropicLLM("sk-test", "claude-opus-5-5", backoff_s=0, client=client, **kw)


def test_parses_json_and_reports_tokens():
    client = FakeClient(reply(json.dumps({"status": "ok"})))
    result = make(client).complete_json(["rules", "schema"], "q", SCHEMA)
    assert result.data == {"status": "ok"}
    assert (result.input_tokens, result.output_tokens) == (11, 7)


def test_request_shape():
    client = FakeClient(reply("{}"))
    make(client).complete_json(["rules", "schema"], "the question", SCHEMA)
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["messages"] == [{"role": "user", "content": "the question"}]
    assert "cache_control" not in req["system"][0]
    assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert req["fallbacks"] == "default"
    assert "temperature" not in req and "tool_choice" not in req


def test_retries_once_then_succeeds():
    rate = status_error(anthropic.RateLimitError, 429)
    client = FakeClient(rate, reply("{}"))
    assert make(client).complete_json(["s"], "q", SCHEMA).data == {}
    assert len(client.requests) == 2


def test_gives_up_after_one_retry():
    errors = [status_error(anthropic.InternalServerError, 500) for _ in range(3)]
    client = FakeClient(*errors)
    with pytest.raises(LLMError, match="busy or unreachable"):
        make(client).complete_json(["s"], "q", SCHEMA)
    assert len(client.requests) == 2


def test_connection_error_is_retried():
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    client = FakeClient(err, reply("{}"))
    make(client).complete_json(["s"], "q", SCHEMA)
    assert len(client.requests) == 2


def test_bad_key_is_not_retried():
    client = FakeClient(status_error(anthropic.AuthenticationError, 401))
    with pytest.raises(LLMError, match="API key"):
        make(client).complete_json(["s"], "q", SCHEMA)
    assert len(client.requests) == 1


def test_other_client_errors_are_not_retried():
    client = FakeClient(status_error(anthropic.BadRequestError, 400))
    with pytest.raises(LLMError, match="400"):
        make(client).complete_json(["s"], "q", SCHEMA)
    assert len(client.requests) == 1


def test_refusal_and_truncation_are_errors():
    with pytest.raises(LLMError, match="declined"):
        make(FakeClient(reply("", "refusal"))).complete_json(["s"], "q", SCHEMA)
    with pytest.raises(LLMError, match="ran out of room"):
        make(FakeClient(reply("{", "max_tokens"))).complete_json(["s"], "q", SCHEMA)


@pytest.mark.parametrize("text", ["not json", "[1, 2]", ""])
def test_unreadable_output_is_an_error(text):
    with pytest.raises(LLMError, match="unreadable"):
        make(FakeClient(reply(text))).complete_json(["s"], "q", SCHEMA)


def test_budget_caps_calls_per_question():
    stub = StubLLM([{"a": 1}, {"a": 2}, {"a": 3}])
    llm = BudgetedLLM(stub, max_calls=2)
    llm.complete_json(["s"], "q", SCHEMA)
    llm.complete_json(["s"], "q", SCHEMA)
    with pytest.raises(LLMError, match="too many"):
        llm.complete_json(["s"], "q", SCHEMA)
    assert len(stub.calls) == 2
    assert (llm.calls, llm.input_tokens, llm.output_tokens) == (2, 2, 2)


def test_stub_raises_queued_errors():
    stub = StubLLM([LLMError("down")])
    with pytest.raises(LLMError):
        stub.complete_json(["s"], "q", SCHEMA)
