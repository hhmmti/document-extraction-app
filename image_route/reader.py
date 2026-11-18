"""The reader: one conversation with the model per page, the pages in parallel, then the skill's build.

A page's conversation starts with the page image.  The model has two tools:

    crop        a region of the page, larger, turned upright when asked; the picture comes back as the tool's result
    write_page  the page's text, its one argument; the conversation ends there, and only that text is kept

After a turn of crops, the assistant's message goes back exactly as it was received, reasoning blocks and all, then
one user message with every crop's result.  A page fails, with its reason, when the model stops without write_page,
asks for more crops than the cap, hits the output limit, calls write_page twice in a turn or with anything but a text,
or when a call fails for good.  A crop whose arguments are wrong is answered with what is wrong, and still counts
against the cap.

`read_document` writes the page images and crops into `<out>/images`, each page's text into the page file the build
script reads, and, when every page was read, builds the transcription with the skill's `build`, the profile id as its
model.  One failed page fails the transcription: the other pages are still read, and nothing is built.

Every call is counted: per page and in total, wall time, calls, crops, tokens (input, output, cache read and write),
each image sent, stop reasons, retries and cost.  Cost is priced from extraction/compare/pricing.json: input and
output tokens as extraction/run.py prices a run, and cache reads and writes at that file's own prices for them.
Bedrock counts cached tokens apart from input tokens, so the four are added.  Cost is None where a price it needs is
unknown.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import extraction.compare
from extraction.compare.parity import cost_usd, model_prices

from . import converse, render
from .prompt import PromptError, system_prompt
from .settings import Settings, load

PRICING = Path(extraction.compare.__file__).with_name("pricing.json")
CACHE_POINT = {"cachePoint": {"type": "default"}}
TOKENS = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
          "cache_read_tokens": "cacheReadInputTokens", "cache_write_tokens": "cacheWriteInputTokens"}
CACHE_PRICES = {"cache_read_tokens": "cache_read_usd_per_1k_tokens",       # pricing.json's lines for them
                "cache_write_tokens": "cache_write_usd_per_1k_tokens"}
BOX = ("left", "top", "right", "bottom")

CROP = """\
Render a region of this page larger, to read print that is small, dense, faint or sideways. Give the region as \
fractions of the page as its image shows it: left and right run from 0 at the left edge to 1 at the right edge, top \
and bottom from 0 at the top edge to 1 at the bottom edge, so left 0, top 0, right 1, bottom 0.5 is the top half. \
The crop is rendered at {crop_dpi} dpi, against the page image's {page_dpi}; `dpi` asks for another resolution, up to \
{max_dpi}. `rotate` turns the crop clockwise by 90, 180 or 270 degrees, so that sideways print reads upright. A \
region of half the page or less keeps the most detail, since a very large picture may be scaled down before you see \
it. This page has {max_crops} crops, and a request with a mistake in it counts as one."""

WRITE_PAGE = """\
Hand in the page's text, once, when the page is typed and its values are read twice. `text` is the whole page as the \
rules say to type it, and only that: no notice line, no page number and no comment of your own. A blank page is \
`[Blank page]`."""


class PageFailed(Exception):
    """The page could not be read; the message is the reason."""


class ToolArgs(ValueError):
    """A tool was called with arguments it cannot take; the message says what is wrong."""


# ---- the two tools ----

def tool_config(settings: Settings) -> dict:
    scripts = render.scripts(settings.skill_dir)
    fraction = {"type": "number", "minimum": 0, "maximum": 1}
    crop = {"type": "object", "additionalProperties": False, "required": list(BOX), "properties": {
        **{name: fraction for name in BOX},
        "rotate": {"type": "integer", "enum": list(render.ROTATIONS)},
        "dpi": {"type": "integer", "minimum": render.MIN_DPI, "maximum": render.MAX_DPI},
    }}
    write = {"type": "object", "additionalProperties": False, "required": ["text"],
             "properties": {"text": {"type": "string"}}}
    described = CROP.format(crop_dpi=scripts.CROP_DPI, page_dpi=scripts.DPI, max_dpi=render.MAX_DPI,
                            max_crops=settings.max_crops)
    return {"tools": [
        {"toolSpec": {"name": "crop", "description": described, "inputSchema": {"json": crop}}},
        {"toolSpec": {"name": "write_page", "description": WRITE_PAGE, "inputSchema": {"json": write}}},
    ]}


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def crop_args(args) -> tuple[tuple[float, float, float, float], int | None, int]:
    """(box, dpi or None for the default, rotate) from the crop tool's input; ToolArgs says what is wrong."""
    if not isinstance(args, dict):
        raise ToolArgs("the arguments are not an object")
    if extra := sorted(set(args) - {*BOX, "rotate", "dpi"}):
        raise ToolArgs(f"unknown argument: {', '.join(map(str, extra))}")
    if missing := [name for name in BOX if name not in args]:
        raise ToolArgs(f"missing: {', '.join(missing)}")
    if wrong := [name for name in BOX if not number(args[name])]:
        raise ToolArgs(f"not a number: {', '.join(wrong)}")
    left, top, right, bottom = (float(args[name]) for name in BOX)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ToolArgs("left, top, right and bottom are fractions from 0 to 1, left below right and top below bottom")
    rotate, dpi = args.get("rotate", 0), args.get("dpi")
    if not number(rotate) or rotate not in render.ROTATIONS:
        raise ToolArgs("rotate is 0, 90, 180 or 270")
    if dpi is not None and not (number(dpi) and dpi == int(dpi) and render.MIN_DPI <= dpi <= render.MAX_DPI):
        raise ToolArgs(f"dpi is a whole number from {render.MIN_DPI} to {render.MAX_DPI}")
    return (left, top, right, bottom), None if dpi is None else int(dpi), int(rotate)


def page_text(args) -> str:
    """The page's text from write_page's input: its one argument, a text that is not blank."""
    if not isinstance(args, dict) or set(args) != {"text"}:
        raise ToolArgs("write_page takes one argument, text")
    if not isinstance(args["text"], str) or not args["text"].strip():
        raise ToolArgs("write_page's text is empty or not a text")
    return args["text"]


def logged(use: dict) -> dict:
    """A tool call as the call log keeps it: a crop's numbers; of write_page, only its text's size."""
    args = use.get("input")
    if use.get("name") == "write_page":
        text = args.get("text") if isinstance(args, dict) else None
        kept = {"text_chars": len(text), "text_lines": len(text.splitlines())} if isinstance(text, str) else None
    else:
        kept = {k: v for k, v in args.items() if number(v)} if isinstance(args, dict) else None
    return {"name": use.get("name"), "args": kept}


# ---- one page ----

def converse_page(pdf: Path, n: int, m: int, page: dict, *, settings: Settings, client, images: Path,
                  deadline: float | None, log, sleep) -> str:
    """Page `n`'s text, from its conversation.  `page` is its account, filled as the calls are made."""
    picture = render.page_picture(settings.skill_dir, pdf, n, images)
    page["images"].append(picture.facts("page"))
    cache = [CACHE_POINT] if settings.cache else []
    system = [{"text": system_prompt(settings.skill_dir, n, m)}, *cache]
    messages = [{"role": "user", "content": [picture.block(), {"text": f"Page {n} of {m}."}, *cache]}]
    tools = tool_config(settings)
    turn = 0
    while True:
        turn += 1
        try:
            reply = converse.call(client, converse.request(settings, system, messages, tools), stream=settings.stream,
                                  retries=settings.retries, deadline=deadline, sleep=sleep)
        except converse.CallFailed as err:
            page["retries"] += err.retries
            log({"page": n, "turn": turn, "error": err.code, "retries": err.retries, "failed": err.failed})
            raise
        message, stop, usage = reply["output"]["message"], reply.get("stopReason"), reply.get("usage", {})
        uses = [block["toolUse"] for block in message.get("content", []) if "toolUse" in block]
        page["calls"] += 1
        page["retries"] += reply["retries"]
        page["stop_reasons"].append(stop)
        for name, key in TOKENS.items():
            page[name] += usage.get(key) or 0
        log({"page": n, "turn": turn, "tools": [logged(use) for use in uses],
             **{name: usage.get(key) for name, key in TOKENS.items()},
             "latency_s": reply["attempt_s"], "server_latency_ms": reply.get("metrics", {}).get("latencyMs"),
             "stop_reason": stop, "retries": reply["retries"], "failed": reply["failed"]})

        if stop == "max_tokens":
            raise PageFailed(f"hit the output limit of {settings.max_tokens} tokens on call {turn}")
        writes = [use for use in uses if use["name"] == "write_page"]
        if len(writes) > 1:
            raise PageFailed(f"write_page was called {len(writes)} times in one turn")
        if writes:
            try:
                return page_text(writes[0]["input"])
            except ToolArgs as err:
                raise PageFailed(str(err)) from err
        if stop != "tool_use" or not uses:
            raise PageFailed(f"the model ended without write_page (stop reason {stop})")

        messages.append(message)                    # exactly as received: reasoning blocks are bound to it
        results = []
        for use in uses:
            if use["name"] != "crop":
                raise PageFailed(f"the model called a tool it was not given: {use['name']!r}")
            page["crops"] += 1
            if page["crops"] > settings.max_crops:
                raise PageFailed(f"passed the cap of {settings.max_crops} crops")
            try:
                box, dpi, rotate = crop_args(use["input"])
                crop = render.crop_picture(settings.skill_dir, pdf, n, box, dpi, rotate, images)
            except ToolArgs as err:
                results.append({"toolResult": {"toolUseId": use["toolUseId"], "status": "error",
                                               "content": [{"text": f"No crop was made: {err}."}]}})
                continue
            page["images"].append(crop.facts("crop"))
            results.append({"toolResult": {"toolUseId": use["toolUseId"], "status": "success", "content": [
                {"text": f"The region, {crop.width}x{crop.height} px."}, crop.block()]}})
        messages.append({"role": "user", "content": results})


def read_page(pdf: Path, n: int, m: int, *, pages: Path, prices: dict, build, **how) -> dict:
    """Page `n`'s account; its text is in its page file when the status is ok."""
    page = {"page": n, "status": "failed", "error": None, "wall_s": None, "calls": 0, "crops": 0, "retries": 0,
            **dict.fromkeys(TOKENS, 0), "cost_usd": None, "stop_reasons": [], "images": []}
    started = time.monotonic()
    try:
        text = converse_page(pdf, n, m, page, **how)
        (pages / build.page_file(n)).write_text(text, encoding="utf-8")
        page["status"] = "ok"
    except (PageFailed, converse.CallFailed, PromptError) as err:
        page["error"] = str(err)
    except Exception as err:  # noqa: BLE001 - one page's failure is recorded with the rest, whatever it is
        page["error"] = f"{type(err).__name__}: {err}"
    page["wall_s"] = round(time.monotonic() - started, 3)
    page["cost_usd"] = price(page, prices)
    return page


# ---- the document ----

def price(counts: dict, prices: dict) -> float | None:
    """What the counted tokens cost: input, output, cache reads and cache writes, each at its own price.  None when
    a price is unknown for tokens that were used."""
    cost = cost_usd(counts["input_tokens"], counts["output_tokens"], prices)
    for tokens, rate in CACHE_PRICES.items():
        if cost is not None and counts[tokens]:
            cost = None if prices.get(rate) is None else cost + counts[tokens] / 1000 * prices[rate]
    return None if cost is None else round(cost, 6)


def total(pages: list[dict], wall_s: float, prices: dict) -> dict:
    out = {"wall_s": round(wall_s, 3), "pages": len(pages), "pages_failed": sum(p["status"] != "ok" for p in pages),
           "pages_wall_s": round(sum(p["wall_s"] for p in pages), 3),
           **{key: sum(p[key] for p in pages) for key in ("calls", "crops", "retries", *TOKENS)},
           "images": sum(len(p["images"]) for p in pages),
           "image_bytes": sum(image["bytes"] for p in pages for image in p["images"]),
           "stop_reasons": dict(Counter(str(stop) for p in pages for stop in p["stop_reasons"]))}
    out["cost_usd"] = price(out, prices)
    return out


def read_document(pdf: Path, out: Path, *, settings: Settings | None = None, client=None, session=None, now=None,
                  sleep=time.sleep) -> dict:
    """Read every page of `pdf` and, when all were read, build its transcription in `out`.  Returns the account:
    `pages`, `total`, `transcription` (its path, or None with `error`) and `calls`, one record per call."""
    from pypdf import PdfReader

    settings = settings or load()
    pdf, out = Path(pdf), Path(out)
    build = render.scripts(settings.skill_dir).B
    data = pdf.read_bytes()
    count = len(PdfReader(pdf).pages)
    images, pages = out / "images", build.pages_dir(pdf, out)
    images.mkdir(parents=True, exist_ok=True)
    pages.mkdir(parents=True, exist_ok=True)
    prices = model_prices(json.loads(PRICING.read_text(encoding="utf-8")), settings.model_id)
    client = client or converse.make_client(settings, session)
    calls, lock = [], threading.Lock()

    def log(record: dict):
        with lock:
            calls.append(record)

    started = time.monotonic()
    deadline = None if settings.deadline_s is None else started + settings.deadline_s
    with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
        read = list(pool.map(lambda n: read_page(pdf, n, count, pages=pages, prices=prices, build=build,
                                                 settings=settings, client=client, images=images, deadline=deadline,
                                                 log=log, sleep=sleep), range(1, count + 1)))
    wall = time.monotonic() - started

    transcription, error = None, None
    if failed := [p for p in read if p["status"] != "ok"]:
        error = "nothing built: " + "; ".join(f"page {p['page']}: {p['error']}" for p in failed)
    else:
        try:
            transcription = str(build.build(pdf, settings.model_id, pages=pages, out=out, now=now))
        except build.Refused as err:
            error = str(err)
    return {"document": {"name": pdf.name, "sha256": hashlib.sha256(data).hexdigest(), "pages": count,
                         "bytes": len(data)},
            "settings": {**dataclasses.asdict(settings), "skill_dir": str(settings.skill_dir)},
            "transcription": transcription, "error": error,
            "total": total(read, wall, prices), "pages": read,
            "calls": sorted(calls, key=lambda c: (c["page"], c["turn"]))}
