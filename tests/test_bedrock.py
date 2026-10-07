"""The Bedrock call, against a fake client. No AWS."""

import hashlib
import time

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from urllib3.exceptions import ProtocolError
from urllib3.exceptions import ReadTimeoutError as StreamReadTimeoutError

from extraction import ExtractionError
from extraction.bedrock import EFFORT, MAX_TOKENS, MODEL_ID, extract, make_client

REPORT = "---\nfamily: F4\n---\n\n## stg_pump_config\n{}\n\n## row_counts\nstg_pump_config: 1\n"


def events(text: str, stop: str = "end_turn", chunk: int = 7):
    yield {"messageStart": {"role": "assistant"}}
    for i in range(0, len(text), chunk):
        yield {"contentBlockDelta": {"delta": {"text": text[i:i + chunk]}, "contentBlockIndex": 0}}
    yield {"contentBlockStop": {"contentBlockIndex": 0}}
    yield {"messageStop": {"stopReason": stop}}
    yield {"metadata": {"usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150}}}


class FakeClient:
    def __init__(self, stream=None, error=None):
        self.stream, self.error, self.calls = stream, error, []

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"stream": self.stream}


def run(client, budget_s=5.0):
    return extract("the prompt\n", budget_s=budget_s, client=client)


def error_of(client, budget_s=5.0) -> ExtractionError:
    with pytest.raises(ExtractionError) as err:
        run(client, budget_s)
    return err.value


def test_success_sends_one_user_message_and_nothing_else():
    client = FakeClient(events(REPORT))
    out = run(client)
    # The final newline is dropped at send time, as the runners' `$(cat ...)` did.
    assert client.calls == [{
        "modelId": MODEL_ID,
        "messages": [{"role": "user", "content": [{"text": "the prompt"}]}],
        "inferenceConfig": {"maxTokens": MAX_TOKENS},
        "additionalModelRequestFields": {"thinking": {"type": "adaptive"},
                                         "output_config": {"effort": "medium"}},
    }]
    assert EFFORT == "medium" and out.effort == "medium" and out.max_tokens == MAX_TOKENS
    assert out.report == REPORT
    assert (out.stop_reason, out.input_tokens, out.output_tokens) == ("end_turn", 120, 30)
    assert out.prompt_sha256 == hashlib.sha256(b"the prompt\n").hexdigest()
    assert out.trailing_newlines_stripped == 1
    assert out.model_id == MODEL_ID and out.preamble_stripped_chars == 0 and out.duration_s >= 0


def test_high_effort_reaches_the_request():
    client = FakeClient(events(REPORT))
    out = extract("the prompt\n", budget_s=5.0, effort="high", client=client)
    assert client.calls[0]["additionalModelRequestFields"]["output_config"] == {"effort": "high"}
    assert out.effort == "high"


def test_reasoning_is_kept_out_of_the_report_and_timed():
    def stream():
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "Weighing the "}}, "contentBlockIndex": 0}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "two tables."}}, "contentBlockIndex": 0}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig"}}, "contentBlockIndex": 0}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        for event in events(REPORT):
            if "contentBlockDelta" in event:
                event["contentBlockDelta"]["contentBlockIndex"] = 1
            yield event

    out = run(FakeClient(stream()))
    assert out.report == REPORT
    assert out.reasoning_text == "Weighing the two tables."
    assert "Weighing" not in out.report
    assert out.reasoning_tokens is None  # ConverseStream reports no separate count
    assert 0 <= out.time_to_first_token_s <= out.time_to_first_text_s <= out.duration_s


def test_stop_at_max_tokens_is_truncated():
    err = error_of(FakeClient(events(REPORT, stop="max_tokens")))
    assert err.code == "MODEL_TRUNCATED"
    assert err.details["stop_reason"] == "max_tokens"


def test_missing_footer_is_truncated():
    err = error_of(FakeClient(events(REPORT.replace("## row_counts", "## counts"))))
    assert err.code == "MODEL_TRUNCATED" and err.details["events_seen"]["messageStop"] == 1


def test_stream_that_closes_without_a_stop_reason_is_a_model_error():
    def dropped():
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig"}}, "contentBlockIndex": 0}}
        yield {"someNewEvent": {}}

    err = error_of(FakeClient(dropped()))
    assert err.code == "MODEL_ERROR" and err.details["aws_error_code"] == "StreamEndedWithoutStop"
    assert err.details["events_seen"] == {"messageStart": 1, "contentBlockDelta": 1,
                                          "contentBlockDelta.reasoningContent": 1, "someNewEvent": 1}
    assert err.details["chars"] == 0 and err.details["stop_reason"] is None


def test_preamble_before_frontmatter_is_dropped():
    out = run(FakeClient(events("Here is the report.\n\n" + REPORT)))
    assert out.report == REPORT
    assert out.preamble_stripped_chars == len("Here is the report.\n\n")


def test_deadline_exceeded_mid_stream():
    def slow():
        yield {"contentBlockDelta": {"delta": {"text": "---\n"}, "contentBlockIndex": 0}}
        time.sleep(0.2)
        yield from events(REPORT)

    assert error_of(FakeClient(slow()), budget_s=0.1).code == "MODEL_TIMEOUT"


def test_stream_that_keeps_emitting_past_the_deadline():
    def endless():
        yield {"contentBlockDelta": {"delta": {"text": "---\n"}, "contentBlockIndex": 0}}
        for _ in range(2000):  # finite, so a broken bound fails instead of hanging
            time.sleep(0.001)
            yield {"contentBlockDelta": {"delta": {"text": "x"}, "contentBlockIndex": 0}}
        yield from events(REPORT)

    t = time.monotonic()
    err = error_of(FakeClient(endless()), budget_s=0.05)
    assert err.code == "MODEL_TIMEOUT"
    assert time.monotonic() - t < 1.0


def test_read_timeout_is_a_timeout():
    def stalled():
        yield {"messageStart": {"role": "assistant"}}
        raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.example")

    assert error_of(FakeClient(stalled())).code == "MODEL_TIMEOUT"


def test_stream_read_timeout_is_a_timeout_not_a_crash():
    # Mid-stream, the timeout arrives as urllib3's class, not botocore's.
    def stalled():
        yield {"messageStart": {"role": "assistant"}}
        raise StreamReadTimeoutError(None, None, "Read timed out.")

    assert error_of(FakeClient(stalled())).code == "MODEL_TIMEOUT"


def test_dropped_connection_mid_stream_is_a_model_error():
    def dropped():
        yield {"messageStart": {"role": "assistant"}}
        raise ProtocolError("Connection broken")

    err = error_of(FakeClient(dropped()))
    assert err.code == "MODEL_ERROR" and err.details["aws_error_code"] == "ProtocolError"


def test_service_error_carries_the_aws_code():
    error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ConverseStream")
    err = error_of(FakeClient(error=error))
    assert err.code == "MODEL_ERROR"
    assert err.details["aws_error_code"] == "ThrottlingException"


def test_client_has_no_retries_and_budget_timeouts():
    cfg = make_client(budget_s=300).meta.config
    assert cfg.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert (cfg.connect_timeout, cfg.read_timeout) == (10, 300)
    cfg = make_client(budget_s=5).meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (5, 5)
