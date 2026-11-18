"""Read one image-only PDF for real, and keep the evidence.

    python -m image_route.trial <pdf> --out <dir> [--pages 2,5] [--profile <name>] [--cache]

It calls the model on Bedrock, so it costs money.  `--out` must be empty or not there yet.  Into it go:

    images/                    each page image sent, and every crop
    <name> - pages/            the page files, page-001.txt, ...
    <name> - transcription.pdf the transcription, when every page was read
    metrics.json               the account: per page and in total, time, calls, crops, tokens, images, cost
    calls.jsonl                one line per call: page, turn, tools and their arguments, tokens, latency, stop
                               reason, retries.  No text the model wrote.

With `--pages`, a PDF of just those pages is written into `--out` first and read in place of the document, so that
the build's rule holds: one page file per page of the scan.  Its pages are numbered from 1; metrics.json keeps which
pages of the document they are.  `--profile` names the AWS profile the call is made with; `--cache` turns the cache
points on.  Every other setting comes from the environment (image_route/settings.py).

When a page fails, everything read is still written, the failure is in metrics.json, and nothing is built.  Exit
code: 0 when the transcription was built, 1 when it was not, 2 when the command could not start.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .reader import read_document
from .settings import SettingsError, load


def page_list(text: str) -> list[int]:
    try:
        pages = [int(part) for part in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("pages are whole numbers with commas between them, like 2,5") from None
    if not pages or min(pages) < 1 or len(set(pages)) != len(pages):
        raise argparse.ArgumentTypeError("pages count from 1, each named once")
    return pages


def subset(pdf: Path, pages: list[int], out: Path) -> Path:
    """A PDF of just `pages` of `pdf`, in that order, written into `out`."""
    from pypdf import PdfReader, PdfWriter

    reader, writer = PdfReader(pdf), PdfWriter()
    if max(pages) > len(reader.pages):
        raise ValueError(f"there is no page {max(pages)}: {pdf.name} has {len(reader.pages)} pages")
    for n in pages:
        writer.add_page(reader.pages[n - 1])
    target = out / f"{pdf.stem} - pages {'-'.join(map(str, pages))}.pdf"
    with open(target, "wb") as f:
        writer.write(f)
    return target


def totals(result: dict) -> str:
    t = result["total"]
    cost = "unknown (no price for this profile)" if t["cost_usd"] is None else f"${t['cost_usd']:.4f}"
    lines = [
        f"pages: {t['pages'] - t['pages_failed']} of {t['pages']} read, in {t['wall_s']:.1f} s "
        f"({t['pages_wall_s']:.1f} s of page time)",
        f"calls: {t['calls']}, crops: {t['crops']}, retries: {t['retries']}, images sent: {t['images']} "
        f"({t['image_bytes']:,} bytes)",
        f"tokens: {t['input_tokens']:,} in, {t['output_tokens']:,} out; cache: {t['cache_read_tokens']:,} read, "
        f"{t['cache_write_tokens']:,} written",
        f"cost: {cost}",
        f"transcription: {result['transcription'] or 'not built'}",
    ]
    if result["error"]:
        lines.append(f"error: {result['error']}")
    return "\n".join(lines)


def main(argv=None, *, client=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m image_route.trial", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path, help="the image-only design document")
    ap.add_argument("--out", type=Path, required=True, help="the folder for the evidence: empty, or not there yet")
    ap.add_argument("--pages", type=page_list, help="read only these pages, like 2,5")
    ap.add_argument("--profile", help="the AWS profile to call Bedrock with")
    ap.add_argument("--cache", action="store_true", help="put a cache point after the system prompt and the page image")
    args = ap.parse_args(argv)

    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        print(f"refused: {args.out} is not empty")
        return 2
    if not args.pdf.is_file():
        print(f"refused: there is no file {args.pdf}")
        return 2
    try:
        settings = load()
    except SettingsError as err:
        print(f"refused: {err}")
        return 2
    if args.cache:
        settings = dataclasses.replace(settings, cache=True)
    args.out.mkdir(parents=True, exist_ok=True)
    pdf = args.pdf
    if args.pages:
        try:
            pdf = subset(args.pdf, args.pages, args.out)
        except ValueError as err:
            print(f"refused: {err}")
            return 2
    session = None
    if client is None and args.profile:
        import boto3

        session = boto3.Session(profile_name=args.profile)

    result = read_document(pdf, args.out, settings=settings, client=client, session=session)
    calls = result.pop("calls")
    result["source"] = {"name": args.pdf.name, "pages_read": args.pages or "all"}
    (args.out / "metrics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.out / "calls.jsonl").write_text("".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8")
    print(totals(result))
    return 0 if result["transcription"] else 1


if __name__ == "__main__":
    sys.exit(main())
