"""The image route's reader (image_route/), with Bedrock stubbed: no AWS, no model, no network.  `Stub` stands in for
the bedrock-runtime client and answers each request from the page and turn it is for; a test that reached for a real
client would fail in `no_client`.  The scans are synthetic, drawn here, with no text layer.
"""

import copy
import json
import os
import random
import re
import threading
import time

import botocore.session
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from botocore.validate import validate_parameters
from PIL import Image
from pypdf import PdfReader

from extraction.bedrock import EFFORT, MODEL_ID, REGION, request_fields
from image_route import converse, prompt, reader, render, trial
from image_route.settings import PREFIX, PROFILE_ENV, SKILL_DIR, VARIABLES, Settings, SettingsError, load

MAKE_CLIENT = converse.make_client          # the real one; `no_client` puts a refusal in its place for every test
PRICES = json.loads(reader.PRICING.read_text(encoding="utf-8"))["models"][MODEL_ID]
REASONING = {"reasoningContent": {"reasoningText": {"text": "", "signature": "c2lnbmF0dXJl"}}}
REDACTED = {"reasoningContent": {"redactedContent": b"\x00\x01opaque"}}
TYPED = ("PUMP DATA\n"
         "  Model          SF2700\n"
         "  Stages         128 [?]\n"
         "  Setting depth  9,850 ft")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in [n for n in os.environ if n.startswith(PREFIX)] + [PROFILE_ENV]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_client(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a test reached for a real Bedrock client")
    monkeypatch.setattr(converse, "make_client", refuse)


def scan(folder, pages=1, name="Design scan.pdf"):
    """A scan with no text layer: Letter pages, each a picture with a black block in its top left corner."""
    images = []
    for _ in range(pages):
        image = Image.new("L", (612, 792), 255)
        image.paste(0, (0, 0, 61, 79))
        images.append(image)
    path = folder / name
    images[0].save(path, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return path


def reply(*blocks, stop="tool_use", tokens=(1000, 200), cache=(0, 0)):
    return {"output": {"message": {"role": "assistant", "content": list(blocks)}}, "stopReason": stop,
            "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1], "totalTokens": sum(tokens),
                      "cacheReadInputTokens": cache[0], "cacheWriteInputTokens": cache[1]},
            "metrics": {"latencyMs": 1234}}


def crop(use_id="crop-1", **args):
    return {"toolUse": {"toolUseId": use_id, "name": "crop",
                        "input": {"left": 0, "top": 0, "right": 0.5, "bottom": 0.25, **args}}}


def write(text=TYPED, use_id="write-1"):
    return {"toolUse": {"toolUseId": use_id, "name": "write_page", "input": {"text": text}}}


def failure(code, status=400):
    return ClientError({"Error": {"Code": code, "Message": "the service said no"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")


def where(kwargs):
    """(page, turn) a request is for: the page from the system prompt, the turn from the messages so far."""
    page = int(re.search(r"This is page (\d+) of \d+", kwargs["system"][0]["text"])[1])
    return page, (len(kwargs["messages"]) + 1) // 2


class Stub:
    """A bedrock-runtime client.  `answer(page, turn, attempt)` gives the response, or an exception to raise;
    `attempt` counts the requests made for that page and turn, from 1.  `calls` holds a copy of each request as it
    was sent, `sent` the requests themselves."""

    def __init__(self, answer):
        self.answer, self.calls, self.sent, self.attempts, self.lock = answer, [], [], {}, threading.Lock()

    def converse(self, **kwargs):
        with self.lock:
            self.calls.append(copy.deepcopy(kwargs))
            self.sent.append(kwargs)
            key = where(kwargs)
            self.attempts[key] = attempt = self.attempts.get(key, 0) + 1
        out = self.answer(*key, attempt)
        if isinstance(out, Exception):
            raise out
        return out


def crop_then_write(page, turn, attempt):
    return reply(REASONING, crop(rotate=90)) if turn == 1 else reply(REASONING, write())


def read(tmp_path, answer, pages=1, sleeps=None, **settings):
    """(the account, the stub) of a synthetic scan read into tmp_path/out."""
    stub = Stub(answer)
    sleeps = [] if sleeps is None else sleeps
    result = reader.read_document(scan(tmp_path, pages), tmp_path / "out", settings=Settings(**settings), client=stub,
                                  sleep=sleeps.append)
    return result, stub


# ---- settings ----

def test_settings_default_and_come_from_the_environment_under_one_prefix():
    assert load({}) == Settings() and Settings().model_id == MODEL_ID and Settings().effort == EFFORT
    assert (Settings().concurrency, Settings().cache, Settings().stream) == (4, False, False)
    assert Settings().deadline_s is None
    assert Settings().skill_dir == SKILL_DIR and (SKILL_DIR / "SKILL.md").is_file()
    assert all(name.startswith(PREFIX) for field, (name, _) in VARIABLES.items() if field != "model_id")
    assert VARIABLES["model_id"][0] == "ESP_INFERENCE_PROFILE_ID"
    assert set(VARIABLES) == {f.name for f in Settings.__dataclass_fields__.values()}
    given = load({PREFIX + "EFFORT": "high", PREFIX + "CONCURRENCY": "2", PREFIX + "CACHE": "1",
                  PREFIX + "DEADLINE_S": "600", PREFIX + "MAX_CROPS": " ", PROFILE_ENV: "us.anthropic.other"})
    assert given == Settings(effort="high", concurrency=2, cache=True, deadline_s=600.0, model_id="us.anthropic.other")
    with pytest.raises(SettingsError, match="ESP_IMAGE_ROUTE_RETRIES='many' cannot be read"):
        load({PREFIX + "RETRIES": "many"})
    with pytest.raises(SettingsError, match="max_crops must stay under 20"):
        Settings(max_crops=20)


@pytest.mark.parametrize("profile", ["global.anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5", "eu.anthropic.x"])
def test_a_profile_that_is_not_a_us_one_is_refused(profile):
    with pytest.raises(SettingsError, match="does not start 'us.'"):
        load({PROFILE_ENV: profile})


# ---- rendering ----

def test_a_page_renders_at_200_dpi_and_a_crop_is_its_region_turned_clockwise(tmp_path):
    pdf = scan(tmp_path, 2)
    page = render.page_picture(SKILL_DIR, pdf, 2, tmp_path)
    assert (page.format, page.width, page.height) == ("png", 1700, 2200)              # Letter at 200 dpi
    assert page.path == tmp_path / "page-002.png" and page.path.read_bytes() == page.data
    assert page.block() == {"image": {"format": "png", "source": {"bytes": page.data}}}
    assert page.facts("page") == {"kind": "page", "file": "page-002.png", "format": "png", "width": 1700,
                                  "height": 2200, "bytes": len(page.data)}

    plain = render.crop_picture(SKILL_DIR, pdf, 1, (0, 0, 0.5, 0.25), None, 0, tmp_path)
    assert (plain.width, plain.height) == (1700, 1100)                                # half by a quarter, at 400 dpi
    image = Image.open(plain.path).convert("L")
    assert image.getpixel((20, 20)) == 0 and image.getpixel((1680, 20)) == 255        # the block is top left

    turned = render.crop_picture(SKILL_DIR, pdf, 1, (0, 0, 0.5, 0.25), None, 90, tmp_path)
    assert (turned.width, turned.height) == (1100, 1700)
    image = Image.open(turned.path).convert("L")
    assert image.getpixel((1080, 20)) == 0 and image.getpixel((20, 20)) == 255        # clockwise: now top right
    assert turned.path.name == "crop-p001-0-0-0.5-0.25-400dpi-r90.png"                # the skill's own name for it

    low = render.crop_picture(SKILL_DIR, pdf, 1, (0.5, 0.5, 1, 1), 200, 180, tmp_path)
    assert (low.width, low.height) == (850, 1100)                                     # a given resolution


def test_a_picture_over_the_limits_is_scaled_down_and_sent_as_jpeg_when_its_png_is_too_big(tmp_path, monkeypatch):
    assert (render.MAX_BYTES, render.MAX_SIDE, render.MAX_IMAGES) == (3_750_000, 8_000, 20)
    noise = tmp_path / "noise.png"
    Image.frombytes("L", (600, 600), random.Random(0).randbytes(360_000)).save(noise)
    monkeypatch.setattr(render, "MAX_BYTES", 60_000)
    assert noise.stat().st_size > render.MAX_BYTES
    sent = render.fit(noise)
    assert sent.format == "jpeg" and len(sent.data) <= 60_000 and sent.width == sent.height <= 600
    assert sent.path == tmp_path / "noise.jpg" and sent.path.read_bytes() == sent.data
    assert Image.open(sent.path).format == "JPEG" and Image.open(sent.path).size == (sent.width, sent.height)

    wide = tmp_path / "wide.png"
    Image.new("L", (600, 400), 255).save(wide)
    monkeypatch.setattr(render, "MAX_SIDE", 300)
    sent = render.fit(wide)
    assert (sent.format, sent.width, sent.height) == ("png", 300, 200) and wide.read_bytes() == sent.data


# ---- the tools' argument checks ----

def test_the_crop_tool_takes_a_region_a_turn_and_a_resolution():
    assert reader.crop_args({"left": 0, "top": 0.25, "right": 1, "bottom": 0.5}) == ((0.0, 0.25, 1.0, 0.5), None, 0)
    assert reader.crop_args({"left": 0.1, "top": 0, "right": 0.2, "bottom": 1, "rotate": 270, "dpi": 600.0}) == \
        ((0.1, 0.0, 0.2, 1.0), 600, 270)


@pytest.mark.parametrize("args,wrong", [
    ("0 0 1 1", "not an object"),
    ({"left": 0, "top": 0, "right": 1}, "missing: bottom"),
    ({"left": 0, "top": 0, "right": 1, "bottom": 1, "page": 2}, "unknown argument: page"),
    ({"left": "0", "top": 0, "right": 1, "bottom": 1}, "not a number: left"),
    ({"left": True, "top": 0, "right": 1, "bottom": 1}, "not a number: left"),
    ({"left": 0.6, "top": 0, "right": 0.5, "bottom": 1}, "left below right"),
    ({"left": 0, "top": 0, "right": 1.2, "bottom": 1}, "fractions from 0 to 1"),
    ({"left": 0, "top": 0.5, "right": 1, "bottom": 0.5}, "top below bottom"),
    ({"left": 0, "top": 0, "right": 1, "bottom": 1, "rotate": 45}, "rotate is 0, 90, 180 or 270"),
    ({"left": 0, "top": 0, "right": 1, "bottom": 1, "dpi": 1200}, "dpi is a whole number from 72 to 600"),
    ({"left": 0, "top": 0, "right": 1, "bottom": 1, "dpi": 300.5}, "dpi is a whole number"),
])
def test_the_crop_tool_says_what_is_wrong_with_its_arguments(args, wrong):
    with pytest.raises(reader.ToolArgs, match=wrong):
        reader.crop_args(args)


@pytest.mark.parametrize("args", [{}, {"text": ""}, {"text": "  \n"}, {"text": 12}, {"text": "a", "note": "b"}, "a"])
def test_write_page_takes_one_text_and_nothing_else(args):
    assert reader.page_text({"text": " kept as typed \n"}) == " kept as typed \n"
    with pytest.raises(reader.ToolArgs, match="write_page"):
        reader.page_text(args)


def test_the_tools_are_crop_and_write_page_with_the_skills_resolutions():
    crop_spec, write_spec = (tool["toolSpec"] for tool in reader.tool_config(Settings(max_crops=7))["tools"])
    assert (crop_spec["name"], write_spec["name"]) == ("crop", "write_page")
    assert "rendered at 400 dpi, against the page image's 200" in crop_spec["description"]
    assert "This page has 7 crops" in crop_spec["description"]
    assert crop_spec["inputSchema"]["json"]["required"] == ["left", "top", "right", "bottom"]
    assert write_spec["inputSchema"]["json"]["properties"] == {"text": {"type": "string"}}     # its one argument
    assert write_spec["inputSchema"]["json"]["required"] == ["text"]


# ---- the system prompt ----

def test_the_prompt_takes_the_skills_pinned_sections():
    """The headings are pinned: renaming one in the skill fails here, before it changes what the model is told."""
    rules = prompt.rules(SKILL_DIR)
    assert prompt.headings(rules) == ["What to type", "The values read twice",                       # SKILL.md
                                      "Vendor layouts", "Tables", "Drawings", "Charts", "Marks",      # reading-aids.md
                                      "Units and pressures"]
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    aids = (SKILL_DIR / "reading-aids.md").read_text(encoding="utf-8")
    assert prompt.SKILL_SECTIONS == ("What to type", "The values read twice")
    for heading in prompt.SKILL_SECTIONS:
        part = prompt.section(skill, heading)
        assert part.startswith(f"## {heading}\n") and part in skill and part in rules and "\n## " not in part
    assert "Type only what the page prints." in rules and "the top perforation depth;" in rules
    assert rules.endswith(aids.strip() + "\n") and aids.startswith("# Reading aids")
    assert "## 3. Type each page" not in rules and "## 1. Tools" not in rules      # the method's steps stay out


def test_the_framing_names_the_page_and_how_to_work():
    text = prompt.system_prompt(SKILL_DIR, 3, 10)
    framing = text[:text.index("## What to type")]
    assert text == prompt.FRAMING.format(n=3, m=10) + "\n" + prompt.rules(SKILL_DIR)
    for said in ("This is page 3 of 10", "ESP (electrical submersible pump) design", "type only what this page prints",
                 "Look at the whole page", "small, dense, faint or sideways", "`crop` tool",
                 "read again, from a zoomed crop, every value on the page that is in \"The values read twice\"",
                 "put ` [?]` after it", "Call `write_page` once",
                 "Do not type the notice line or a page number: the build adds the notice"):
        assert said in framing, said
    assert not prompt.headings(framing)


def test_a_skill_without_a_pinned_section_is_an_error(tmp_path):
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    (tmp_path / "SKILL.md").write_text(skill.replace("## The values read twice", "## Values to check"))
    (tmp_path / "reading-aids.md").write_text("# Reading aids\n")
    with pytest.raises(prompt.PromptError, match="no '## The values read twice' section"):
        prompt.system_prompt(tmp_path, 1, 1)
    with pytest.raises(prompt.PromptError, match="cannot be read"):
        prompt.system_prompt(tmp_path / "nowhere", 1, 1)


# ---- one page's conversation ----

def test_a_page_that_crops_then_writes(tmp_path):
    result, stub = read(tmp_path, crop_then_write)
    out = tmp_path / "out"
    assert result["error"] is None and result["transcription"] == str(out / "Design scan - transcription.pdf")
    assert (out / "Design scan - pages" / "page-001.txt").read_text(encoding="utf-8") == TYPED

    first, second = stub.calls
    page_png = (out / "images" / "page-001.png").read_bytes()
    assert first == {
        "modelId": MODEL_ID,
        "system": [{"text": prompt.system_prompt(SKILL_DIR, 1, 1)}],
        "messages": [{"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": page_png}}},
                                                  {"text": "Page 1 of 1."}]}],
        "toolConfig": reader.tool_config(Settings()),
        "inferenceConfig": {"maxTokens": 32_000},
        "additionalModelRequestFields": {"thinking": {"type": "adaptive"}, "output_config": {"effort": EFFORT}},
    }
    assert first["additionalModelRequestFields"] == request_fields(EFFORT)      # thinking, as extraction sets it
    assert "toolChoice" not in first["toolConfig"]                                # this model refuses a forced tool
    crop_png = (out / "images" / "crop-p001-0-0-0.5-0.25-400dpi-r90.png").read_bytes()
    assert second["messages"] == first["messages"] + [
        {"role": "assistant", "content": [REASONING, crop(rotate=90)]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "crop-1", "status": "success", "content": [
            {"text": "The region, 1100x1700 px."}, {"image": {"format": "png", "source": {"bytes": crop_png}}}]}}]},
    ]
    assert {k: v for k, v in second.items() if k != "messages"} == {k: v for k, v in first.items() if k != "messages"}

    page, = result["pages"]
    assert page == {
        "page": 1, "status": "ok", "error": None, "wall_s": page["wall_s"], "calls": 2, "crops": 1, "retries": 0,
        "input_tokens": 2000, "output_tokens": 400, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cost_usd": page["cost_usd"], "stop_reasons": ["tool_use", "tool_use"],
        "images": [{"kind": "page", "file": "page-001.png", "format": "png", "width": 1700, "height": 2200,
                    "bytes": len(page_png)},
                   {"kind": "crop", "file": "crop-p001-0-0-0.5-0.25-400dpi-r90.png", "format": "png", "width": 1100,
                    "height": 1700, "bytes": len(crop_png)}],
    }
    assert result["calls"] == [
        {"page": 1, "turn": 1, "tools": [{"name": "crop", "args": crop(rotate=90)["toolUse"]["input"]}],
         "input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "latency_s": result["calls"][0]["latency_s"], "server_latency_ms": 1234, "stop_reason": "tool_use",
         "retries": 0, "failed": []},
        {"page": 1, "turn": 2, "tools": [{"name": "write_page", "args": {"text_chars": len(TYPED), "text_lines": 4}}],
         "input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "latency_s": result["calls"][1]["latency_s"], "server_latency_ms": 1234, "stop_reason": "tool_use",
         "retries": 0, "failed": []},
    ]
    assert result["total"]["stop_reasons"] == {"tool_use": 2} and result["total"]["images"] == 2
    assert result["total"]["image_bytes"] == len(page_png) + len(crop_png)


def test_reasoning_blocks_go_back_unchanged(tmp_path):
    turn_one = reply(REASONING, {"text": "The table is sideways."}, REDACTED, crop(rotate=270), crop("crop-2", dpi=600))
    result, stub = read(tmp_path, lambda page, turn, attempt: turn_one if turn == 1 else reply(REASONING, write()))
    assert result["error"] is None
    assert stub.sent[1]["messages"][1] is turn_one["output"]["message"]         # the very object received
    assert stub.calls[1]["messages"][1] == {"role": "assistant", "content": [
        REASONING, {"text": "The table is sideways."}, REDACTED, crop(rotate=270), crop("crop-2", dpi=600)]}
    results = stub.calls[1]["messages"][2]["content"]                           # one user message, a result per crop
    assert [r["toolResult"]["toolUseId"] for r in results] == ["crop-1", "crop-2"] and len(stub.calls) == 2
    assert result["pages"][0]["crops"] == 2
    assert [i["width"] for i in result["pages"][0]["images"]] == [1700, 1100, 2550]   # page; turned; at 600 dpi


def test_a_crop_with_wrong_arguments_is_answered_and_counted(tmp_path):
    result, stub = read(tmp_path, lambda page, turn, attempt:
                        reply(crop(left=0.9)) if turn == 1 else reply(write()))
    assert result["error"] is None and result["pages"][0]["crops"] == 1 and len(result["pages"][0]["images"]) == 1
    assert stub.calls[1]["messages"][2]["content"] == [{"toolResult": {
        "toolUseId": "crop-1", "status": "error", "content": [{"text": (
            "No crop was made: left, top, right and bottom are fractions from 0 to 1, left below right and top "
            "below bottom.")}]}}]


@pytest.mark.parametrize("answer,reason", [
    (reply({"text": "Here is the page: PUMP DATA"}, stop="end_turn"),
     "ended without write_page (stop reason end_turn)"),
    (reply(stop="tool_use"), "ended without write_page"),
    (reply({"text": "PUMP"}, stop="max_tokens"), "hit the output limit of 32000 tokens on call 1"),
    (reply(write("PUMP DA"), stop="max_tokens"), "hit the output limit"),            # a cut-off page is not kept
    (reply(write("a"), write("b", "write-2")), "write_page was called 2 times in one turn"),
    (reply(write("  ")), "write_page's text is empty"),
    (reply({"toolUse": {"toolUseId": "x", "name": "search", "input": {}}}), "a tool it was not given: 'search'"),
    (reply(stop="content_filtered"), "stop reason content_filtered"),
], ids=["text, no tool", "no tool call", "output limit", "output limit in write_page", "two writes", "empty text",
        "unknown tool", "another stop reason"])
def test_a_page_that_does_not_end_at_write_page_fails_with_its_reason(tmp_path, answer, reason):
    result, stub = read(tmp_path, lambda page, turn, attempt: answer)
    page, = result["pages"]
    assert page["status"] == "failed" and reason in page["error"] and len(stub.calls) == 1
    assert result["transcription"] is None and result["error"] == f"nothing built: page 1: {page['error']}"
    assert not list((tmp_path / "out").rglob("*.txt")) and not list((tmp_path / "out").glob("*transcription*"))
    assert page["calls"] == 1 and page["input_tokens"] == 1000                  # what it cost is still counted


def test_passing_the_crop_cap_fails_the_page(tmp_path):
    result, stub = read(tmp_path, lambda page, turn, attempt: reply(crop(f"a{turn}"), crop(f"b{turn}", rotate=90)),
                        max_crops=3)
    page, = result["pages"]
    assert page["status"] == "failed" and page["error"] == "passed the cap of 3 crops"
    assert len(stub.calls) == 2 and page["crops"] == 4 and len(page["images"]) == 1 + 3     # the fourth is not made


def test_every_request_is_one_converse_takes_by_botocores_own_model(tmp_path):
    """The installed botocore's model of Converse checks each request's shape, here and before any call is paid for:
    the image blocks, the tool results with a picture or an error, the reasoning sent back, the cache points."""
    model = botocore.session.get_session().get_service_model("bedrock-runtime")      # read from disk; no call
    turn_one = reply(REASONING, {"text": "Sideways."}, REDACTED, crop(rotate=90), crop("crop-2", left=2))
    for cache in (False, True):
        (tmp_path / str(cache)).mkdir()
        result, stub = read(tmp_path / str(cache), cache=cache,
                            answer=lambda page, turn, attempt: turn_one if turn == 1 else reply(REASONING, write()))
        assert result["error"] is None and len(stub.calls) == 2
        for name in ("Converse", "ConverseStream"):
            for call in stub.calls:
                validate_parameters(call, model.operation_model(name).input_shape)   # raises on a wrong shape
    with pytest.raises(Exception, match="Unknown parameter"):
        validate_parameters({**stub.calls[0], "tool_choice": {}}, model.operation_model("Converse").input_shape)


# ---- retries and the deadline ----

def test_a_throttle_then_success(tmp_path):
    sleeps = []
    result, stub = read(tmp_path, lambda page, turn, attempt:
                        failure("ThrottlingException", 429) if attempt == 1 else reply(write()), sleeps=sleeps)
    assert result["error"] is None and len(stub.calls) == 2 and stub.calls[0] == stub.calls[1]
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= converse.BACKOFF_BASE_S               # backoff, with jitter
    assert result["pages"][0]["retries"] == 1 and result["pages"][0]["calls"] == 1 and result["total"]["retries"] == 1
    assert (result["calls"][0]["retries"], result["calls"][0]["failed"]) == (1, ["ThrottlingException"])


def test_retries_exhausted_fail_the_page(tmp_path):
    sleeps = []
    result, stub = read(tmp_path, lambda page, turn, attempt: failure("ServiceUnavailableException", 503),
                        sleeps=sleeps, retries=3)
    page, = result["pages"]
    assert len(stub.calls) == 4 and len(sleeps) == 3 and page["status"] == "failed"
    assert all(0 <= s <= min(converse.BACKOFF_CAP_S, converse.BACKOFF_BASE_S * 2 ** k) for k, s in enumerate(sleeps))
    assert page["error"].startswith("gave up after 4 attempts, the last failing with ServiceUnavailableException")
    assert (page["retries"], page["calls"]) == (3, 0) and result["transcription"] is None
    assert result["calls"] == [{"page": 1, "turn": 1, "error": "ServiceUnavailableException", "retries": 3,
                                "failed": ["ServiceUnavailableException"] * 4}]


@pytest.mark.parametrize("error", [failure("ValidationException"), failure("AccessDeniedException", 403),
                                   failure("ResourceNotFoundException", 404), NoCredentialsError()],
                         ids=["validation", "access", "another 4xx", "no credentials"])
def test_validation_and_access_errors_are_never_retried(tmp_path, error):
    sleeps = []
    result, stub = read(tmp_path, lambda page, turn, attempt: error, sleeps=sleeps)
    assert len(stub.calls) == 1 and sleeps == [] and result["pages"][0]["retries"] == 0
    assert f"Bedrock error {converse.error_code(error)}, not retried" in result["pages"][0]["error"]


def test_what_is_retried():
    assert all(converse.retryable(failure(code)) for code in ("ThrottlingException", "ModelTimeoutException",
                                                              "ModelNotReadyException", "InternalServerException",
                                                              "modelStreamErrorException", "throttlingException"))
    assert converse.retryable(failure("SomethingNew", 500)) and converse.retryable(failure("SomethingNew", 429))
    assert converse.retryable(EndpointConnectionError(endpoint_url="https://bedrock-runtime.invalid"))
    assert converse.retryable(converse.StreamDropped("dropped"))
    assert not converse.retryable(failure("ValidationException", 500))            # never, whatever its status
    assert not converse.retryable(failure("ModelErrorException", 424)) and not converse.retryable(NoCredentialsError())


def test_no_call_starts_after_the_deadline(tmp_path):
    def answer(page, turn, attempt):
        time.sleep(0.3)                                                          # the first call outlasts the deadline
        return reply(crop())
    result, stub = read(tmp_path, answer, deadline_s=0.1)
    page, = result["pages"]
    assert len(stub.calls) == 1 and page["status"] == "failed"
    assert page["error"] == "the deadline passed before the call could start" and page["calls"] == 1
    assert result["calls"][-1] == {"page": 1, "turn": 2, "error": "Deadline", "retries": 0, "failed": []}

    (tmp_path / "already").mkdir()
    result, stub = read(tmp_path / "already", answer, deadline_s=0)
    assert len(stub.calls) == 0 and result["pages"][0]["error"] == "the deadline passed before the call could start"


# ---- the cache points and the two APIs ----

def test_cache_points_follow_the_system_prompt_and_the_page_image_when_on(tmp_path):
    result, stub = read(tmp_path, lambda page, turn, attempt:
                        reply(crop(), cache=(0, 5000)) if turn == 1 else reply(write(), cache=(5000, 0)), cache=True)
    point = {"cachePoint": {"type": "default"}}
    for call in stub.calls:
        assert call["system"] == [{"text": prompt.system_prompt(SKILL_DIR, 1, 1)}, point]
        assert [list(block) for block in call["messages"][0]["content"]] == [["image"], ["text"], ["cachePoint"]]
        assert json.dumps(call["messages"][1:], default=str).count("cachePoint") == 0
    page, = result["pages"]
    assert (page["cache_read_tokens"], page["cache_write_tokens"]) == (5000, 5000)
    assert page["cost_usd"] == pytest.approx(2000 / 1000 * PRICES["input_usd_per_1k_tokens"]
                                             + 400 / 1000 * PRICES["output_usd_per_1k_tokens"])   # reported, not priced


def events(*blocks, stop="tool_use"):
    """A message as ConverseStream sends it: each block in pieces."""
    yield {"messageStart": {"role": "assistant"}}
    for index, block in enumerate(blocks):
        if "toolUse" in block:
            tool, text = block["toolUse"], json.dumps(block["toolUse"]["input"])
            yield {"contentBlockStart": {"contentBlockIndex": index, "start": {"toolUse": {
                "toolUseId": tool["toolUseId"], "name": tool["name"]}}}}
            for piece in (text[:7], text[7:]):
                yield {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"toolUse": {"input": piece}}}}
        elif "text" in block:
            for piece in (block["text"][:3], block["text"][3:]):
                yield {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"text": piece}}}
        elif "redactedContent" in block["reasoningContent"]:
            yield {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"reasoningContent": {
                "redactedContent": block["reasoningContent"]["redactedContent"]}}}}
        else:
            reasoning = block["reasoningContent"]["reasoningText"]
            yield {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"reasoningContent": {
                "text": reasoning["text"]}}}}
            yield {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"reasoningContent": {
                "signature": reasoning["signature"]}}}}
        yield {"contentBlockStop": {"contentBlockIndex": index}}
    if stop:
        yield {"messageStop": {"stopReason": stop}}
        yield {"metadata": {"usage": {"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200},
                            "metrics": {"latencyMs": 1234}}}


class StreamStub(Stub):
    def converse_stream(self, **kwargs):
        return {"stream": self.converse(**kwargs)}


def test_converse_stream_gives_the_same_message_put_together_from_its_events(tmp_path):
    thought = {"reasoningContent": {"reasoningText": {"text": "Reading the table.", "signature": "c2ln"}}}
    blocks = [thought, {"text": "Cropping."}, REDACTED, crop(rotate=90)]
    assert converse.assemble(events(*blocks)) == {
        "output": {"message": {"role": "assistant", "content": blocks}}, "stopReason": "tool_use",
        "usage": {"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200}, "metrics": {"latencyMs": 1234}}

    stub = StreamStub(lambda page, turn, attempt: events(*blocks) if turn == 1 else events(thought, write()))
    result = reader.read_document(scan(tmp_path), tmp_path / "out", settings=Settings(stream=True), client=stub)
    assert result["error"] is None and stub.calls[1]["messages"][1] == {"role": "assistant", "content": blocks}
    assert result["pages"][0]["input_tokens"] == 2000 and result["pages"][0]["stop_reasons"] == ["tool_use"] * 2

    sleeps = []
    dropped = StreamStub(lambda page, turn, attempt: events(crop(), stop=None))        # no stop reason: retried
    result = reader.read_document(scan(tmp_path, name="Other scan.pdf"), tmp_path / "other",
                                  settings=Settings(stream=True, retries=1), client=dropped, sleep=sleeps.append)
    assert len(dropped.calls) == 2 and len(sleeps) == 1
    assert "the last failing with StreamDropped" in result["pages"][0]["error"]


def test_the_client_is_made_in_the_extraction_region_from_a_given_session():
    made = {}

    class Session:
        def client(self, name, **kwargs):
            made.update(name=name, **kwargs)
            return "the client"

    assert MAKE_CLIENT(Settings(concurrency=16, read_timeout_s=300), Session()) == "the client"
    config = made["config"]
    assert (made["name"], made["region_name"]) == ("bedrock-runtime", REGION) and REGION == "us-east-1"
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"} and config.read_timeout == 300
    assert config.max_pool_connections == 16 and config.tcp_keepalive is True


# ---- cost ----

def test_cost_is_priced_from_pricing_json_and_null_where_a_price_is_unknown(tmp_path):
    answer = lambda page, turn, attempt: reply(write(), tokens=(123_456, 7_890), cache=(1_000, 2_000))   # noqa: E731
    result, _ = read(tmp_path, answer, pages=2)
    one = 123_456 / 1000 * PRICES["input_usd_per_1k_tokens"] + 7_890 / 1000 * PRICES["output_usd_per_1k_tokens"]
    assert (PRICES["input_usd_per_1k_tokens"], PRICES["output_usd_per_1k_tokens"]) == (0.004, 0.02)
    assert [p["cost_usd"] for p in result["pages"]] == [round(one, 6)] * 2 == [0.651624] * 2
    assert result["total"]["cost_usd"] == round(2 * one, 6)
    assert (result["total"]["input_tokens"], result["total"]["output_tokens"]) == (246_912, 15_780)
    assert (result["total"]["cache_read_tokens"], result["total"]["cache_write_tokens"]) == (2_000, 4_000)

    (tmp_path / "unpriced").mkdir()
    result, _ = read(tmp_path / "unpriced", answer, model_id="us.anthropic.claude-sonnet-5-5")      # prices are null
    assert result["pages"][0]["cost_usd"] is None and result["total"]["cost_usd"] is None
    assert result["total"]["input_tokens"] == 123_456
    (tmp_path / "unlisted").mkdir()
    result, _ = read(tmp_path / "unlisted", answer, model_id="us.anthropic.not-in-the-file")
    assert result["total"]["cost_usd"] is None


# ---- the trial command, end to end ----

def test_the_trial_reads_the_pages_it_is_given_and_builds_the_transcription(tmp_path, capsys):
    pdf, out = scan(tmp_path, 3), tmp_path / "trial"
    secret = "THINKING ALOUD ABOUT THE TABLE"

    def answer(page, turn, attempt):
        if page == 1 and turn == 1:
            return reply(REASONING, {"text": secret}, crop(rotate=90), cache=(0, 4000))
        return reply(REASONING, write(TYPED if page == 1 else "[Blank page]"), cache=(4000, 0))
    stub = Stub(answer)
    assert trial.main([str(pdf), "--out", str(out), "--pages", "1,3", "--cache"], client=stub) == 0

    subset = out / "Design scan - pages 1-3.pdf"
    assert len(PdfReader(subset).pages) == 2 and len(stub.calls) == 3
    assert sorted(p.name for p in (out / "images").iterdir()) == [
        "crop-p001-0-0-0.5-0.25-400dpi-r90.png", "page-001.png", "page-002.png"]
    pages = out / "Design scan - pages 1-3 - pages"
    assert (pages / "page-001.txt").read_text(encoding="utf-8") == TYPED
    assert (pages / "page-002.txt").read_text(encoding="utf-8") == "[Blank page]"
    assert all(call["system"][-1] == {"cachePoint": {"type": "default"}} for call in stub.calls)      # --cache
    assert sorted(where(call) for call in stub.calls) == [(1, 1), (1, 2), (2, 1)]  # the subset's pages, from 1
    assert all(re.search(r"This is page [12] of 2,", call["system"][0]["text"]) for call in stub.calls)

    built = out / "Design scan - pages 1-3 - transcription.pdf"
    reader_ = PdfReader(built)
    mark = json.loads(reader_.metadata["/RoamTranscription"])
    assert mark["model"] == MODEL_ID == "us.anthropic.claude-opus-5-5"            # the profile id called
    assert mark["original"]["pages"] == 2 and mark["unsure"] == [{"page": 1, "line": 3}]
    assert reader_.attachments["Design scan - pages 1-3.pdf"] == [subset.read_bytes()]
    assert "SF2700" in reader_.pages[0].extract_text() and "Blank page" in reader_.pages[1].extract_text()
    assert "page 1 of 2 - check every value" in reader_.pages[0].extract_text()   # the build's notice, not the model's

    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["transcription"] == str(built) and metrics["error"] is None and "calls" not in metrics
    assert metrics["source"] == {"name": "Design scan.pdf", "pages_read": [1, 3]}
    assert metrics["document"]["name"] == subset.name and metrics["document"]["pages"] == 2
    assert metrics["settings"]["model_id"] == MODEL_ID and metrics["settings"]["cache"] is True
    assert [(p["page"], p["status"], p["calls"], p["crops"]) for p in metrics["pages"]] == [(1, "ok", 2, 1),
                                                                                           (2, "ok", 1, 0)]
    total = metrics["total"]
    assert (total["pages"], total["pages_failed"], total["calls"], total["crops"], total["retries"]) == (2, 0, 3, 1, 0)
    assert (total["input_tokens"], total["output_tokens"]) == (3000, 600)
    assert (total["cache_read_tokens"], total["cache_write_tokens"]) == (8000, 4000) and total["images"] == 3
    assert total["cost_usd"] == pytest.approx(3 * 0.004 + 0.6 * 0.02)

    logged = (out / "calls.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in logged.splitlines()]
    assert [(c["page"], c["turn"], [t["name"] for t in c["tools"]]) for c in lines] == [
        (1, 1, ["crop"]), (1, 2, ["write_page"]), (2, 1, ["write_page"])]
    assert set(lines[0]) == {"page", "turn", "tools", "input_tokens", "output_tokens", "cache_read_tokens",
                             "cache_write_tokens", "latency_s", "server_latency_ms", "stop_reason", "retries", "failed"}
    assert lines[0]["tools"][0]["args"] == {"left": 0, "top": 0, "right": 0.5, "bottom": 0.25, "rotate": 90}
    assert secret not in logged and "SF2700" not in logged and "Blank page" not in logged     # no model text
    assert secret not in json.dumps(metrics) and "SF2700" not in json.dumps(metrics)

    said = capsys.readouterr().out
    assert "pages: 2 of 2 read" in said and "calls: 3, crops: 1, retries: 0, images sent: 3" in said
    assert "tokens: 3,000 in, 600 out; cache: 8,000 read, 4,000 written" in said and "cost: $0.0240" in said
    assert f"transcription: {built}" in said

    assert trial.main([str(pdf), "--out", str(out)], client=stub) == 2            # not empty any more
    assert capsys.readouterr().out == f"refused: {out} is not empty\n" and len(stub.calls) == 3


def test_the_trial_writes_what_it_has_when_a_page_fails_and_builds_nothing(tmp_path, capsys):
    pdf, out = scan(tmp_path, 2), tmp_path / "trial"
    stub = Stub(lambda page, turn, attempt:
                reply(write()) if page == 1 else reply({"text": "I could not read it."}, stop="end_turn"))
    assert trial.main([str(pdf), "--out", str(out)], client=stub) == 1
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    reason = "the model ended without write_page (stop reason end_turn)"
    assert metrics["transcription"] is None and metrics["error"] == f"nothing built: page 2: {reason}"
    assert [(p["status"], p["error"]) for p in metrics["pages"]] == [("ok", None), ("failed", reason)]
    assert metrics["total"]["pages_failed"] == 1 and metrics["source"]["pages_read"] == "all"
    assert (out / "Design scan - pages" / "page-001.txt").is_file() and not list(out.glob("*transcription*"))
    assert len((out / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert sorted(p.name for p in (out / "images").iterdir()) == ["page-001.png", "page-002.png"]
    said = capsys.readouterr().out
    assert "pages: 1 of 2 read" in said and "transcription: not built" in said and f"page 2: {reason}" in said


def test_the_trial_refuses_what_it_cannot_start(tmp_path, capsys, monkeypatch):
    pdf = scan(tmp_path, 2)
    never = Stub(lambda *_: pytest.fail("a call was made"))
    assert trial.main([str(pdf), "--out", str(tmp_path / "a"), "--pages", "3"], client=never) == 2
    assert "there is no page 3: Design scan.pdf has 2 pages" in capsys.readouterr().out
    assert trial.main([str(tmp_path / "missing.pdf"), "--out", str(tmp_path / "b")], client=never) == 2
    monkeypatch.setenv(PROFILE_ENV, "global.anthropic.claude-opus-5-5")
    assert trial.main([str(pdf), "--out", str(tmp_path / "c")], client=never) == 2
    assert "does not start 'us.'" in capsys.readouterr().out and not (tmp_path / "c").exists()
    for bad in ("2,2", "0", "two"):
        with pytest.raises(SystemExit):
            trial.main([str(pdf), "--out", str(tmp_path / "d"), "--pages", bad], client=never)
