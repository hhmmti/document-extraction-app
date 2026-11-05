---
name: transcribe-design-scan
description: Types a scanned or image-only ESP design document (a PDF whose pages are pictures, with no text layer) page by page into one PDF that the design-extraction app can read, with the original scan attached and every unsure reading marked. Use when an engineer has a scanned ESP design document to bring into the design-extraction app, or when the app says a document has no text layer.
---

# Transcribe a design scan

This method is written for Claude Opus 5.5 reading page images. No OCR engine is used: you look at each page image
yourself and type what the page prints.

The result is one PDF, `<scan name without .pdf> - transcription.pdf`:

- one typed page per scan page, in order, so page N is page N of the scan;
- the scan itself attached inside it, byte for byte, under its own file name;
- a mark in its document information that the design-extraction app recognises: the scan's name, SHA-256 and page
  count, when and by which model it was typed, and every reading marked unsure.

It is written to `/mnt/user-data/outputs/` when that folder exists, else beside the scan. The engineer uploads it to
the app like any other PDF; the app shows the original scan beside the review.

`scripts/` holds the two scripts below; `reading-aids.md` holds the rules for drawings, charts, tables and the vendor
layouts worth knowing; `fonts/` holds the typeface the build script sets the pages in, with its licence.

## 1. Tools

The scripts need pypdf, reportlab and a renderer (pypdfium2 with Pillow, or pdftoppm).

- **On claude.ai** (the folder `/mnt/user-data/` exists), run them with `python3`, as below. Each script checks for
  what it needs and installs what is missing with pip. If one says something is missing and cannot be installed, tell
  the engineer what, and stop.
- **Anywhere else**, Claude Code on the engineer's machine among them, the scripts install nothing, and they are never
  run with the machine's own Python. Before the first one, make a virtual environment in the working folder, with the
  packages in it:

  ```bash
  python3 -m venv .venv
  .venv/bin/pip install pypdf reportlab pypdfium2 pillow
  ```

  Then run both scripts with its Python: `.venv/bin/python` in place of `python3` in every command below. A script
  that finds something missing names it, prints these two commands and stops (exit code 4). If the working folder
  already has a `.venv`, ask the engineer before you use it: it may be a project's own.

The commands below name the scripts from this skill's folder; from anywhere else, give their full path.

## 2. Render the scan

```bash
python3 scripts/render_pages.py "<scan>.pdf"
```

It prints the scan's SHA-256 and page count, renders every page to a PNG at 200 dpi, and prints the pages folder, where
the page files go, with the pages typed there already.

If it says **page 1 already has a text layer**, the document needs no transcription: the app reads it as it is. Say
so to the engineer and ask whether to go on. Only if they say yes, run it again with `--anyway`.

## 3. Type each page, in order

For page 1, then page 2, and so on, one page at a time:

1. Look at the whole page image first, to see how it is laid out.
2. Zoom in wherever the print is small, dense or faint, with the crop helper. A region is given as fractions of the
   page: `0 0 1 0.5` is the top half, `0.5 0 1 0.5` the top right quarter. It renders at 400 dpi; `--dpi 600` goes
   further. `--rotate 90` (or 270) turns a sideways table upright.

   ```bash
   python3 scripts/render_pages.py "<scan>.pdf" --crop 3 0 0.4 1 0.7
   ```

3. Type the page into `page-001.txt` (`page-002.txt`, ...) in the pages folder, following "What to type" below.
4. Read again, zoomed in, every value on the page that is in "The values read twice" below. Where the second reading
   differs from the first, type the reading you are surer of, then ` [?]`.
5. Save the page file before you move to the next page. A long document can take more than one chat: the page files
   are what carries over. In a later chat, put the earlier page files in the pages folder (the engineer can upload
   them), run the render script again, and go on from the page it names as next.

A blank page is typed as `[Blank page]`; a page file is never left empty.

## 4. Build the transcription

```bash
python3 scripts/build_transcription.py "<scan>.pdf" --model "<your model name, as you name yourself>"
```

It refuses on any gap: a page file missing or empty, or a count of page files that is not the scan's page count. Fix
what it names and run it again. It writes each page's first line itself, the notice that the page is a transcription
to be checked against the original scan; the page files hold only the page's content.

## 5. Reply

Reply with the file, its page count, and the readings marked unsure, by page (the build script prints them). Tell the
engineer to upload this one file to the design-extraction app, and to check every value against the original scan,
which the app shows beside the review.

## What to type

Type only what the page prints.

- Numbers, units and labels exactly as printed: the same digits, decimal places, commas, signs, letter case and
  spelling. Nothing is converted, corrected, completed, summarised or reconciled. When two pages print different
  values for the same thing, each page keeps what it prints.
- A reading you cannot make out at all is `[?]` alone. A reading you doubt is the reading, then `[?]`: `1,850 [?]`.
  Never guess.
- Tables as aligned columns, one printed row per line, with every column kept, every scenario's column among them.
  Keep a column's header above it, its unit with it.
- A drawing (a wellbore or pump schematic) under the line `[Drawing: labels from top to bottom]`, with its labels in
  the order they are drawn, one per line.
- A chart: its title, legend, axis labels and the numbers printed on it only. Nothing is read off a plotted line.
- `[Handwritten: …]` around handwriting, `[Stamp: …]` around a stamp's text, `[Signature]` for a signature,
  `[Logo: …]` with a logo's printed name, and `[Blank page]` for a blank page.
- No summary, heading, label, value or comment of your own, and no page number or notice: the build script adds the
  notice.

`reading-aids.md` has the detail for drawings, charts and tables, and the layouts of vendor programs worth knowing.

## The values read twice

Each of these, wherever a page prints it, is read a second time, zoomed in, with its printed unit:

- the pump string: each section's model as printed, its stages, its housings and stages per housing, its role, its
  order, and its top and bottom depths;
- the setting depth, measured (MD) and true vertical (TVD), and the intake depth;
- the motor's type, nameplate horsepower, power factor and efficiency;
- the specific gravities (fluid, oil, water and gas), the bubble point, the reservoir pressure, the bottomhole
  temperature and the solution gas-oil ratio;
- the top perforation depth;
- the design date;
- each one's unit as printed, a pressure's basis with it: psia, psig, or a bare psi.
