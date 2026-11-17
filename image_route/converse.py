"""One call to the model on Bedrock, for the image route: Converse or ConverseStream, retried on a throttle or a
transient error.

extraction/bedrock.py sends one text message with one attempt; this sends images and tools, and retries.  It shares
that file's region, its connect timeout and its thinking fields (`request_fields`), so thinking is set as it is there.

A call is retried, with exponential backoff and full jitter, when Bedrock throttles it, fails on its own side (a 5xx,
a model timeout, a model not ready, an error inside the stream) or the connection fails or drops.  A validation or
access error is never retried, nor is any other 4xx.  botocore's own retries are off, so every attempt is counted
here.  With a deadline, no attempt starts after it; one already running is left to finish.

`call` returns the response in Converse's shape whichever API ran.  From ConverseStream the message is put together
again from its events (`assemble`); from Converse it is the service's own object, untouched.
"""

from __future__ import annotations

import json
import random
import time

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError
from urllib3.exceptions import HTTPError as StreamHTTPError

from extraction.bedrock import CONNECT_TIMEOUT_S, REGION, request_fields

BACKOFF_BASE_S, BACKOFF_CAP_S = 2.0, 60.0
NEVER = {"validationexception", "accessdeniedexception"}
RETRY = {"throttlingexception", "toomanyrequestsexception", "serviceunavailableexception", "internalserverexception",
         "modeltimeoutexception", "modelnotreadyexception", "modelstreamerrorexception"}


class StreamDropped(Exception):
    """ConverseStream closed without a stop reason: the service dropped the response."""


class CallFailed(Exception):
    """The call did not succeed.  `code` is the last error's, `failed` the code of every failed attempt, and
    `retries` the attempts started after the first."""

    def __init__(self, reason: str, code: str, failed: list[str], retries: int):
        super().__init__(reason)
        self.code, self.failed, self.retries = code, failed, retries


def make_client(settings, session=None):
    """A bedrock-runtime client in extraction/bedrock.py's region, from `session` when one is given."""
    import boto3

    return (session or boto3.Session()).client("bedrock-runtime", region_name=REGION, config=Config(
        retries={"total_max_attempts": 1, "mode": "standard"},      # retries are made, and counted, in `call`
        connect_timeout=CONNECT_TIMEOUT_S,
        read_timeout=settings.read_timeout_s,       # a page's last call can think, then type, for minutes
        tcp_keepalive=True,                         # a silent wait must not look idle to a gateway
        max_pool_connections=max(10, settings.concurrency),
    ))


def request(settings, system: list, messages: list, tools: dict) -> dict:
    return {"modelId": settings.model_id, "system": system, "messages": messages, "toolConfig": tools,
            "inferenceConfig": {"maxTokens": settings.max_tokens},
            "additionalModelRequestFields": request_fields(settings.effort)}


def error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") or "Unknown"
    return type(exc).__name__


def retryable(exc: Exception) -> bool:
    if isinstance(exc, ClientError):            # an error inside the stream is one too (EventStreamError)
        code = error_code(exc).lower()
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
        return code not in NEVER and (code in RETRY or status == 429 or status >= 500)
    return isinstance(exc, (BotoConnectionError, HTTPClientError, StreamHTTPError, StreamDropped))


def assemble(stream) -> dict:
    """ConverseStream's events as Converse's response: the message's blocks in order, each put together from its
    deltas; a tool's input parsed from its JSON, or left as the text received when that does not parse."""
    blocks, stop, usage, metrics = {}, None, {}, {}
    for event in stream:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"]
            if "toolUse" in start.get("start", {}):
                tool = start["start"]["toolUse"]
                blocks[start["contentBlockIndex"]] = {"toolUse": {"toolUseId": tool["toolUseId"],
                                                                  "name": tool["name"], "input": ""}}
        elif "contentBlockDelta" in event:
            index, delta = event["contentBlockDelta"]["contentBlockIndex"], event["contentBlockDelta"]["delta"]
            if "text" in delta:
                block = blocks.setdefault(index, {"text": ""})
                block["text"] += delta["text"]
            elif "toolUse" in delta:
                blocks[index]["toolUse"]["input"] += delta["toolUse"]["input"]
            elif "reasoningContent" in delta:
                part = delta["reasoningContent"]
                if "redactedContent" in part:
                    block = blocks.setdefault(index, {"reasoningContent": {"redactedContent": b""}})
                    block["reasoningContent"]["redactedContent"] += part["redactedContent"]
                else:
                    block = blocks.setdefault(index, {"reasoningContent": {"reasoningText": {"text": ""}}})
                    text = block["reasoningContent"]["reasoningText"]
                    text["text"] += part.get("text", "")
                    if "signature" in part:
                        text["signature"] = text.get("signature", "") + part["signature"]
        elif "messageStop" in event:
            stop = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            usage, metrics = event["metadata"].get("usage", {}), event["metadata"].get("metrics", {})
    if stop is None:
        raise StreamDropped("the stream ended without a stop reason")
    content = [blocks[index] for index in sorted(blocks)]
    for block in content:
        if "toolUse" in block:
            try:
                block["toolUse"]["input"] = json.loads(block["toolUse"]["input"] or "{}")
            except ValueError:
                pass                                # cut off, or not JSON: the tool's argument check says so
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop, "usage": usage,
            "metrics": metrics}


def call(client, kwargs: dict, *, stream: bool = False, retries: int = 5, deadline: float | None = None,
         sleep=time.sleep, clock=time.monotonic, jitter=random.uniform) -> dict:
    """The response to one request, in Converse's shape, with `attempt_s` (the successful attempt's wall time),
    `retries` (the attempts started after the first) and `failed` (the error code of each attempt before the one
    that succeeded).  CallFailed when it cannot be had."""
    failed: list[str] = []
    for attempt in range(retries + 1):
        if deadline is not None and clock() >= deadline:
            raise CallFailed("the deadline passed before the call could start", "Deadline", failed, max(0, attempt - 1))
        started = clock()
        try:
            if stream:
                response = assemble(client.converse_stream(**kwargs)["stream"])
            else:
                response = client.converse(**kwargs)
            return {**response, "attempt_s": round(clock() - started, 3), "retries": attempt, "failed": failed}
        except (ClientError, BotoCoreError, StreamHTTPError, StreamDropped) as exc:
            code = error_code(exc)
            if not retryable(exc):
                raise CallFailed(f"Bedrock error {code}, not retried: {exc}", code, failed, attempt) from exc
            failed.append(code)
            if attempt == retries:
                raise CallFailed(f"gave up after {len(failed)} attempts, the last failing with {code}: {exc}",
                                 code, failed, attempt) from exc
            sleep(jitter(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** attempt)))
    raise AssertionError("unreachable")
