"""Extract, part 2: one bounded, streaming call to Opus on Bedrock.

The prompt goes as a single user message: no system prompt and no sampling
parameters. What is sent is exactly

    modelId=model_id,
    messages=[{"role": "user", "content": [{"text": prompt.rstrip("\n")}]}],
    inferenceConfig={"maxTokens": max_tokens},
    additionalModelRequestFields={"thinking": {"type": "adaptive"},
                                  "output_config": {"effort": effort}}

Reasoning arrives as `reasoningContent` deltas. It is collected separately and
never enters the report text.

Trailing newlines are dropped because the batch runners sent `$(cat prompt)`,
which drops them; `assemble_prompt` keeps them, as the runners' saved files do.

The call is never retried. The binding limit is `budget_s`, enforced by a
deadline checked on every stream event. The socket timeouts are backstops:
connect at most 10 s, read at most `budget_s`. Nothing is written to disk.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError
from urllib3.exceptions import HTTPError as StreamHTTPError
from urllib3.exceptions import ReadTimeoutError as StreamReadTimeoutError

from .errors import ExtractionError

# The global profile is denied by organization policy; the US profile keeps requests in US regions.
MODEL_ID = "us.anthropic.claude-opus-5-5"
REGION = "us-east-1"
CONNECT_TIMEOUT_S = 10

# The largest existing report is 61,420 bytes. At a conservative 2 bytes per
# output token (the reports are dense with digits and punctuation, which
# tokenise worse than prose), that is 61,420 / 2 = 30,710 tokens. Doubling it
# for documents larger than any in the corpus gives 61,420 tokens; rounded up,
# 64,000. This is a bound on output, not a claim about the model's maximum: a
# value the model rejects comes back as MODEL_ERROR (ValidationException).
MAX_TOKENS = 64_000

# Thinking effort. The field is `output_config.effort`; Opus 5.5 accepts
# low | medium | high | xhigh | max and defaults to medium, and Amazon Bedrock
# supports it: https://platform.claude.com/docs/en/build-with-claude/effort
# On Converse it goes in `additionalModelRequestFields`, beside adaptive
# thinking, as AWS documents for Claude (the page lists anthropic.claude-opus-5-5):
# https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html#claude-messages-adaptive-thinking-converse
EFFORT = "medium"

_FOOTER = re.compile(r"^## row_counts", re.M)
_FRONTMATTER = re.compile(r"^---[ \t\r\f\v]*$")


@dataclass(frozen=True)
class ExtractResult:
    report: str
    reasoning_text: str             # reasoning deltas, if the stream carried any text; never in `report`
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None    # None: ConverseStream's usage reports no separate reasoning count
    cache_read_input_tokens: int | None
    cache_write_input_tokens: int | None
    time_to_first_token_s: float | None  # first delta of any kind, reasoning included
    time_to_first_text_s: float | None   # first report-text delta
    duration_s: float               # total model time: request to end of stream
    server_latency_ms: int | None   # Bedrock's own metadata.metrics.latencyMs
    model_id: str
    effort: str
    max_tokens: int
    prompt_sha256: str              # of the prompt as passed in, before trailing newlines are dropped
    trailing_newlines_stripped: int  # dropped from the prompt before sending
    preamble_stripped_chars: int     # dropped before the first `---` line of the response; 0 if none


def make_client(budget_s: float, region: str = REGION):
    """A bedrock-runtime client with no retries and timeouts inside the budget."""
    import boto3

    return boto3.client("bedrock-runtime", region_name=region, config=Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=min(CONNECT_TIMEOUT_S, budget_s),
        # Adaptive thinking with its text omitted can leave the stream silent for
        # minutes, so the read timeout is the whole budget, not a short stall cap.
        # ponytail: a stall with no events can overrun the deadline by up to
        # read_timeout; a watchdog that closes the stream at the deadline is the
        # upgrade if that ever matters.
        read_timeout=budget_s,
    ))


def strip_preamble(text: str) -> tuple[str, int]:
    """Drop everything before the first `^---\\s*$` line, as the runners' awk did."""
    offset = 0
    for line in text.split("\n"):
        if _FRONTMATTER.fullmatch(line):
            report = text[offset:]
            return (report if report.endswith("\n") else report + "\n"), offset
        offset += len(line) + 1
    return text, 0


def request_fields(effort: str) -> dict:
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


def extract(prompt: str, *, budget_s: float, max_tokens: int = MAX_TOKENS,
            model_id: str = MODEL_ID, effort: str = EFFORT, client=None) -> ExtractResult:
    t0 = time.monotonic()
    deadline = t0 + budget_s
    client = client or make_client(budget_s)
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    sent = prompt.rstrip("\n")

    def timeout(why: str):
        return ExtractionError("MODEL_TIMEOUT", why, {
            "budget_s": budget_s, "elapsed_s": round(time.monotonic() - t0, 3), "prompt_sha256": sha,
        })

    parts, reasoning, stop_reason, usage, metrics = [], [], None, {}, {}
    first_any = first_text = None
    seen = Counter()  # event types received, deltas by kind: the record of what Bedrock sent
    try:
        response = client.converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": sent}]}],
            inferenceConfig={"maxTokens": max_tokens},
            additionalModelRequestFields=request_fields(effort),
        )
        for event in response["stream"]:
            now = time.monotonic()
            if now > deadline:
                raise timeout(f"time budget of {budget_s}s ran out mid-stream")
            for kind in event:
                seen[kind] += 1
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                for kind in delta:
                    seen[f"contentBlockDelta.{kind}"] += 1
                first_any = first_any or now
                if "reasoningContent" in delta:
                    reasoning.append(delta["reasoningContent"].get("text", ""))
                elif "text" in delta:
                    first_text = first_text or now
                    parts.append(delta["text"])
            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason")
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                metrics = event["metadata"].get("metrics", {})
        if time.monotonic() > deadline:
            raise timeout(f"time budget of {budget_s}s ran out as the stream closed")
    except (ReadTimeoutError, StreamReadTimeoutError) as exc:
        # botocore raises its own class while sending the request; reading the
        # event stream afterwards surfaces urllib3's directly.
        raise timeout(f"socket read timed out: {exc}") from exc
    except ClientError as exc:  # includes errors raised from inside the stream
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        raise ExtractionError("MODEL_ERROR", f"Bedrock error {code}: {exc}",
                              {"aws_error_code": code, "prompt_sha256": sha}) from exc
    except (BotoCoreError, StreamHTTPError) as exc:  # no credentials, unreachable, connection dropped mid-stream
        code = type(exc).__name__
        raise ExtractionError("MODEL_ERROR", f"Bedrock call failed: {exc}",
                              {"aws_error_code": code, "prompt_sha256": sha}) from exc

    text = "".join(parts)
    details = {"stop_reason": stop_reason, "output_tokens": usage.get("outputTokens"),
               "chars": len(text), "reasoning_chars": len("".join(reasoning)),
               "elapsed_s": round(time.monotonic() - t0, 3), "events_seen": dict(seen), "prompt_sha256": sha}
    if stop_reason is None and not _FOOTER.search(text):
        # No messageStop: the stream closed without finishing the message. That is the
        # service dropping the response, not the model running out of room.
        raise ExtractionError("MODEL_ERROR", "stream ended without a stop reason",
                              details | {"aws_error_code": "StreamEndedWithoutStop"})
    if stop_reason == "max_tokens":
        raise ExtractionError("MODEL_TRUNCATED", f"stopped at max_tokens={max_tokens}", details)
    if not _FOOTER.search(text):
        raise ExtractionError("MODEL_TRUNCATED", "report has no '## row_counts' footer", details)

    report, stripped = strip_preamble(text)

    def since(t):
        return None if t is None else round(t - t0, 3)

    return ExtractResult(
        report=report,
        reasoning_text="".join(reasoning),
        stop_reason=stop_reason,
        input_tokens=usage.get("inputTokens"),
        output_tokens=usage.get("outputTokens"),
        reasoning_tokens=None,
        cache_read_input_tokens=usage.get("cacheReadInputTokens"),
        cache_write_input_tokens=usage.get("cacheWriteInputTokens"),
        time_to_first_token_s=since(first_any),
        time_to_first_text_s=since(first_text),
        duration_s=round(time.monotonic() - t0, 3),
        server_latency_ms=metrics.get("latencyMs"),
        model_id=model_id,
        effort=effort,
        max_tokens=max_tokens,
        prompt_sha256=sha,
        trailing_newlines_stripped=len(prompt) - len(sent),
        preamble_stripped_chars=stripped,
    )
