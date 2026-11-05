"""Render a scanned design document for reading: every page as a PNG at 200 dpi, or one region of a page at a higher
resolution.

    python3 scripts/render_pages.py SCAN.pdf
    python3 scripts/render_pages.py SCAN.pdf --crop PAGE LEFT TOP RIGHT BOTTOM [--dpi 400] [--rotate 90]

The first form prints the scan's SHA-256 and page count, renders every page (page-001.png, ...) into a folder in the
temporary directory, and prints the pages folder the page files go in, with the pages typed there already, so that a
later chat resumes where an earlier one stopped.  When page 1 already has a text layer it stops and says so: such a
document needs no transcription (add --anyway to render it all the same).

--crop renders one region: LEFT TOP RIGHT BOTTOM are fractions of the page as its page image shows it, 0 0 1 1 the
whole page and 0.5 0 1 0.5 its top right quarter.  --rotate turns the crop clockwise by 90, 180 or 270 degrees, so
that a table printed sideways reads upright.  pypdfium2 renders; without it, pdftoppm (which cannot rotate).

Packages (pypdf, and pypdfium2 with Pillow unless pdftoppm is there) as for build_transcription.py: installed with pip
only in claude.ai's sandbox; anywhere else it names what is missing with the commands that set up a virtual
environment in the working folder, and stops with exit 4.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_transcription as B  # noqa: E402

DPI, CROP_DPI = 200, 400
TEXT_FLOOR = 20               # non-space characters on page 1: the design-extraction app reads such a document as it is
NEEDS = (("pypdf", "pypdf"), ("pypdfium2", "pypdfium2"), ("PIL", "pillow"))


def images_dir(sha: str) -> Path:
    folder = Path(tempfile.gettempdir()) / f"transcribe-{sha[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def page_image(scan: Path, page: int, dpi: int):
    """One page as a PIL image (pypdfium2), or None when pypdfium2 or Pillow is not here."""
    if importlib.util.find_spec("pypdfium2") is None or importlib.util.find_spec("PIL") is None:
        return None
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(scan)) as pdf:
        return pdf[page - 1].render(scale=dpi / 72).to_pil()


def pdftoppm(scan: Path, page: int, dpi: int, target: Path, box=None) -> Path:
    """One page, or a region of it in pixels (x, y, w, h) at `dpi`, rendered by pdftoppm into `target`."""
    crop = [] if box is None else ["-x", str(box[0]), "-y", str(box[1]), "-W", str(box[2]), "-H", str(box[3])]
    subprocess.run(["pdftoppm", "-png", "-singlefile", "-r", str(dpi), "-f", str(page), "-l", str(page), *crop,
                    str(scan), str(target.with_suffix(""))], check=True)
    return target


def render_all(scan: Path, count: int, folder: Path) -> list[Path]:
    out = []
    for n in range(1, count + 1):
        target = folder / f"page-{n:03d}.png"
        image = page_image(scan, n, DPI)
        if image is None:
            pdftoppm(scan, n, DPI, target)
        else:
            image.save(target)
        out.append(target)
    return out


def crop(scan: Path, page: int, box, dpi: int, rotate: int, folder: Path) -> Path:
    """A region of one page, LEFT TOP RIGHT BOTTOM as fractions of the page, turned clockwise by `rotate`."""
    left, top, right, bottom = box
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("LEFT TOP RIGHT BOTTOM are fractions from 0 to 1, LEFT below RIGHT and TOP below BOTTOM")
    target = folder / f"crop-p{page:03d}-{left:g}-{top:g}-{right:g}-{bottom:g}-{dpi}dpi-r{rotate}.png"
    image = page_image(scan, page, dpi)
    if image is None:
        if rotate:
            raise ValueError("--rotate needs pypdfium2, which is not installed here")
        from pypdf import PdfReader
        media = PdfReader(str(scan)).pages[page - 1].mediabox
        w, h = float(media.width) * dpi / 72, float(media.height) * dpi / 72
        return pdftoppm(scan, page, dpi, target, (round(left * w), round(top * h), round((right - left) * w),
                                                  round((bottom - top) * h)))
    w, h = image.size
    image = image.crop((round(left * w), round(top * h), round(right * w), round(bottom * h)))
    (image.rotate(-rotate, expand=True) if rotate else image).save(target)
    return target


def typed_already(folder: Path, count: int) -> list[int]:
    return [n for n in range(1, count + 1) if (folder / B.page_file(n)).is_file()
            and (folder / B.page_file(n)).read_text(encoding="utf-8").strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scan", type=Path, help="the scanned design document (PDF)")
    ap.add_argument("--crop", nargs=5, type=float, metavar=("PAGE", "LEFT", "TOP", "RIGHT", "BOTTOM"))
    ap.add_argument("--dpi", type=int, default=CROP_DPI, help=f"the crop's resolution (default {CROP_DPI})")
    ap.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0, help="turn the crop clockwise")
    ap.add_argument("--anyway", action="store_true", help="render although page 1 already has a text layer")
    args = ap.parse_args(argv)
    missing = B.ensure(NEEDS)
    if "pypdf" in missing or (missing and not shutil.which("pdftoppm")):
        return B.say_missing(missing)
    from pypdf import PdfReader

    data = args.scan.read_bytes()
    sha, reader = hashlib.sha256(data).hexdigest(), PdfReader(args.scan)
    count = len(reader.pages)
    if args.crop:
        page = int(args.crop[0])
        if not 1 <= page <= count:
            print(f"there is no page {page}: the scan has {count} pages")
            return 1
        print(f"crop: {crop(args.scan, page, args.crop[1:], args.dpi, args.rotate, images_dir(sha))}")
        return 0
    print(f"scan: {args.scan.name}\nsha256: {sha}\npages: {count}")
    text = len("".join((reader.pages[0].extract_text() or "").split())) if count else 0
    if text >= TEXT_FLOOR and not args.anyway:
        print(f"page 1 already has a text layer ({text} characters): the design-extraction app can read this document "
              "as it is, so it needs no transcription.  Nothing rendered.")
        return 3
    for path in render_all(args.scan, count, images_dir(sha)):
        print(f"image: {path}")
    pages = B.pages_dir(args.scan)
    pages.mkdir(parents=True, exist_ok=True)
    done = typed_already(pages, count)
    print(f"pages folder: {pages}")
    print(f"typed already: {', '.join(map(str, done)) if done else 'none'}; "
          + (f"next: page {next(n for n in range(1, count + 1) if n not in done)}" if len(done) < count
             else "every page is typed: run build_transcription.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
