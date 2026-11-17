"""Page images and crops, made by the transcribe-design-scan skill's own render script and kept within the image limits
of the Converse API.

The skill's scripts are loaded from its folder (`scripts`), so the route renders a page and crops a region exactly as
the skill's method does: a page at render_pages.DPI (200), a crop at render_pages.CROP_DPI (400) or a given
resolution, its region as fractions of the page, turned clockwise by 0, 90, 180 or 270 degrees.

The limits assumed, per image sent to Converse.  They are the documented ones as recalled when this was written,
not checked against the service:
    MAX_BYTES   3.75 MB
    MAX_SIDE    8,000 px, each side
and, per request, MAX_IMAGES: past 20 images in one request the model's API caps each side at 2,000 px, so the
settings keep a page's image and its crops within 20.

`fit` sends a picture as it was rendered when it is within both.  A side over MAX_SIDE is scaled down to it; a PNG
over MAX_BYTES is re-encoded as JPEG, at a lower quality and then smaller until it fits, and saved beside the PNG.
"""

from __future__ import annotations

import importlib.util
import io
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

MAX_BYTES = 3_750_000
MAX_SIDE = 8_000
MAX_IMAGES = 20
ROTATIONS = (0, 90, 180, 270)
MIN_DPI, MAX_DPI = 72, 600        # a crop's resolution: the skill's method goes to 600
JPEG_QUALITIES = (90, 75, 60)

_PDFIUM = threading.Lock()        # pdfium is not thread-safe, and pages are read in parallel


@lru_cache(maxsize=None)
def scripts(skill_dir: Path):
    """The skill's render_pages module, loaded from its folder; its `B` is the skill's build_transcription.
    ponytail: render_pages imports build_transcription by name, so a second skill folder loaded in the same process
    would share the first one's build module.  One process reads with one skill."""
    path = Path(skill_dir) / "scripts" / "render_pages.py"
    spec = importlib.util.spec_from_file_location("image_route_render_pages", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Picture:
    format: str        # "png" or "jpeg", as Converse names them
    data: bytes
    width: int
    height: int
    path: Path         # the file holding exactly `data`

    def block(self) -> dict:
        return {"image": {"format": self.format, "source": {"bytes": self.data}}}

    def facts(self, kind: str) -> dict:
        return {"kind": kind, "file": self.path.name, "format": self.format, "width": self.width,
                "height": self.height, "bytes": len(self.data)}


def fit(path: Path) -> Picture:
    """The picture at `path`, as it is sent: within MAX_SIDE and MAX_BYTES."""
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.copy()
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        data = buffer.getvalue()
        path.write_bytes(data)                       # the file is what was sent
    if len(data) <= MAX_BYTES:
        return Picture("png", data, *image.size, path)
    image, target = image.convert("RGB"), path.with_suffix(".jpg")
    while True:
        for quality in JPEG_QUALITIES:
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality)
            if buffer.tell() <= MAX_BYTES:
                target.write_bytes(buffer.getvalue())
                return Picture("jpeg", buffer.getvalue(), *image.size, target)
        image = image.resize((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)))


def page_picture(skill_dir: Path, pdf: Path, page: int, folder: Path) -> Picture:
    """Page `page` (from 1) at the skill's page resolution, saved in `folder` as page-NNN.png."""
    render = scripts(skill_dir)
    target = folder / f"page-{page:03d}.png"
    with _PDFIUM:
        image = render.page_image(pdf, page, render.DPI)
        if image is None:
            raise RuntimeError("pypdfium2 or Pillow is missing: the page cannot be rendered")
        image.save(target)
    return fit(target)


def crop_picture(skill_dir: Path, pdf: Path, page: int, box, dpi: int | None, rotate: int, folder: Path) -> Picture:
    """The region `box` (left, top, right, bottom, as fractions of the page) of page `page`, at `dpi` (default: the
    skill's crop resolution), turned clockwise by `rotate`; saved in `folder` under the skill's name for it."""
    render = scripts(skill_dir)
    with _PDFIUM:
        target = render.crop(pdf, page, tuple(box), dpi or render.CROP_DPI, rotate, folder)
    return fit(target)
