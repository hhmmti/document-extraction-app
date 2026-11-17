"""The system prompt of one page's conversation: a short framing of the route's own, then the transcribe-design-scan
skill's reading rules, read from the skill's folder at run time so that the skill and the route share one definition.

From SKILL.md it takes the `## ` sections named in SKILL_SECTIONS, each from its heading to the next; from
reading-aids.md, the whole file.  A section that is not there is an error, never a silent gap.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

SKILL_FILE, AIDS_FILE = "SKILL.md", "reading-aids.md"
SKILL_SECTIONS = ("What to type", "The values read twice")

FRAMING = """\
You are typing one page of a scanned ESP (electrical submersible pump) design document, so that software which cannot \
read pictures can read the page as text. An engineer will later check every value against the scan. This is page {n} \
of {m}, and its image is in the first message. The other pages are typed separately, so type only what this page \
prints.

Work in this order:

1. Look at the whole page, to see how it is laid out.
2. Wherever the print is small, dense, faint or sideways, look closer with the `crop` tool before you type that part. \
You can ask for several crops at once.
3. When you have the page's text, read again, from a zoomed crop, every value on the page that is in "The values read \
twice" below. Where the second reading differs from the first, keep the reading you are surer of and put ` [?]` after \
it.
4. Call `write_page` once, with the whole page's text. That call ends the page: nothing you write outside it is kept, \
and nothing can be added after it.

Do not type the notice line or a page number: the build adds the notice to every page.

What follows is the transcription method's own rules, word for word. They were written for someone running the \
method's scripts by hand, so where they mention the crop helper, `--rotate` or `--dpi`, use the `crop` tool, and where \
they mention a page file, that is the text you give `write_page`.
"""


class PromptError(Exception):
    """The skill's folder does not hold the rules the prompt takes; the message says what is missing."""


def headings(text: str) -> list[str]:
    return re.findall(r"^## (.+?)\s*$", text, re.M)


def section(text: str, heading: str) -> str:
    """The `## heading` section of `text`: its heading line and everything down to the next `## ` line."""
    found = re.search(rf"^## {re.escape(heading)}[ \t]*\n.*?(?=^## |\Z)", text, re.M | re.S)
    if not found:
        raise PromptError(f"{SKILL_FILE} has no '## {heading}' section; it has: {', '.join(headings(text)) or 'none'}")
    return found[0].strip() + "\n"


@lru_cache(maxsize=None)
def rules(skill_dir: Path) -> str:
    """The skill's reading rules: SKILL.md's sections, then reading-aids.md."""
    try:
        skill = (Path(skill_dir) / SKILL_FILE).read_text(encoding="utf-8")
        aids = (Path(skill_dir) / AIDS_FILE).read_text(encoding="utf-8")
    except OSError as err:
        raise PromptError(f"the skill's folder {skill_dir} cannot be read: {err}") from err
    return "\n".join([*(section(skill, heading) for heading in SKILL_SECTIONS), aids.strip() + "\n"])


def system_prompt(skill_dir: Path, n: int, m: int) -> str:
    return FRAMING.format(n=n, m=m) + "\n" + rules(skill_dir)
