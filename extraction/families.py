"""Vendor families: page-1 signatures, page maps, and the glyph map.

These are the constants of the corpus slicing step, carried over verbatim for
F1-F5 and F7 with one change: F1's signature is `SIZING REPORT` alone. The
dropped marker, `Summit ESP Representative`, sits in a page-1 disclaimer that
some genuine F1 documents do not print; the required-page check carries the
rest of the identification.

F6 (XSize) has no entry. Its documents have no text layer, so detection
reports them as image-only rather than as a family.
"""

from __future__ import annotations

# Yield is counted in NON-WHITESPACE characters. Layout-preserving extraction
# pads every page out to a rectangle, so raw length would report a blank page
# as thousands of characters and no page would ever look thin.

# Below this many characters a page has no usable text layer.
IMAGE_ROUTE_CHARS = 20

# Below this many characters a page is flagged low-yield: it still routes to
# text, but the thinness is recorded rather than silently accepted. The
# thinnest page known to carry design data is an F1 `SIZING REPORT` cover at
# 154 characters, so anything under 150 is below every page whose text layer
# is known to have worked.
LOW_YIELD_CHARS = 150

# --------------------------------------------------------------------------
# Page-1 signatures
#
# Every marker must be present in page 1's default-extraction lines for a
# family to match.
# --------------------------------------------------------------------------

PAGE1_SIGNATURE = {
    "F1": ["SIZING REPORT"],
    "F2": ["EQUIPMENT AND PERFORMANCE REPORT", "ChampionX Representative"],
    "F3": ["Schematics Report", "SLB Engineer"],
    "F4": ["Well Name :", "Powered by LiftXP", "Manufacture"],
    "F5": ["ProLift Summary Report", "Surface Electrical"],
    "F7": ["Summary Sheet", "GENERAL"],
}

# --------------------------------------------------------------------------
# Page maps, from each family's extraction contract
#
# Each entry is (role, status, matcher). Order is significant: the first entry
# that matches a page supplies that page's primary role.
#
#   status "required"  — contract spine; absence fails detection
#   status "optional"  — contract-named optional group; absence is normal
#   status "plot_only" — contract explicitly says read no numbers from it;
#                        this is the ONLY basis on which a page is excluded
#
# A page matching no entry gets no role. Per the over-inclusion rule it is
# still included: under-inclusion silently loses a field, over-inclusion only
# costs tokens.
#
# Matchers:
#   ("line",   s) — some line, stripped, equals s
#   ("start",  s) — some line, stripped, starts with s
#   ("title",  s) — some *title* line starts with s. A title line is one whose
#                   largest glyph is bigger than the page's modal glyph size,
#                   or is the page's first line. F1 sets several equipment
#                   blocks on one page and repeats words like "Motor" inside
#                   data rows, so plain line matching is not safe there.
#   ("re",     p) — some line matches regex p
# --------------------------------------------------------------------------

PAGE_MAP = {
    # F1 SpyGlass — required spine plus nine optional groups. Page numbers move
    # with scenario count, so every role is resolved by title.
    "F1": [
        ("SIZING REPORT", "required", ("title", "SIZING REPORT")),
        ("Design Schematic", "required", ("title", "Design Schematic")),
        ("Design Overview", "required", ("title", "Design Overview")),
        ("Theoretical Production Data", "required", ("title", "Theoretical Production Data")),
        ("Pumps", "required", ("title", "Pumps")),
        ("Motor Performance", "optional", ("title", "Motor Performance")),
        ("Motor", "required", ("title", "Motor")),
        ("Multi-Frequency Head Curve", "required", ("title", "Multi-Frequency Head Curve")),
        ("Multi-Frequency BHP Curve", "required", ("title", "Multi-Frequency BHP Curve")),
        ("Single Pump Charts", "required", ("title", "Single Pump Charts")),
        ("Multiscenario Pump Curve", "optional", ("title", "Multiscenario Pump Curve")),
        ("Multifrequency Tapered Pumps", "optional", ("title", "Multifrequency Tapered Pumps")),
        ("Cable Temperature", "optional", ("title", "Cable Temperature")),
        ("Cross Section Analysis", "optional", ("title", "Cross Section Analysis")),
        ("Pressure Traverse", "optional", ("title", "Pressure Traverse")),
        ("IPR", "optional", ("title", "IPR")),
        ("Correlations List", "optional", ("title", "Correlations List")),
        ("VSD", "optional", ("title", "VSD")),
        # Printed by the family, named by no page-map entry. Kept visible as
        # its own role so the manifest does not pretend they were unreadable.
        ("Intakes", "unmapped", ("title", "Intakes")),
        ("Seals", "unmapped", ("title", "Seals")),
        ("Gas Curve", "unmapped", ("title", "Gas Curve")),
        ("Deviation Surveys", "unmapped", ("title", "Deviation Surveys")),
    ],
    # F2 ChampionX — 9 pages, fixed, every one named. No plot-only exclusion:
    # the contract's p8-p9 "remaining plots" are still contract-named pages.
    "F2": [
        ("Equipment and Performance Report", "required", ("line", "EQUIPMENT AND PERFORMANCE REPORT")),
        ("Summary / Pump Series", "required", ("line", "Summary")),
        ("Performance Curve", "required", ("start", "Performance Curve:")),
        ("Gas Separator / BOI · Gas Handler · Main Pump", "required", ("line", "Gas Separator / BOI")),
        ("Surface Equipment", "required", ("line", "Surface Equipment")),
        # The motor page leads with `Motor Rating 100%`, not a bare `Motor`;
        # the bare form appears on the Surface Equipment page, which is why
        # Surface Equipment is matched first.
        ("Motor", "required", ("re", r"^Motor( Rating\b|$)")),
        # A data table the contract's page map describes as one of the
        # "remaining plots". Kept as its own role so the mismatch is visible.
        ("ESP Setting Depth Survey", "unmapped", ("line", "ESP Setting Depth Survey")),
    ],
    # F3 SLB — report boundaries plus seven contract-named chart-only pages.
    "F3": [
        ("Schematics Report", "required", ("line", "Schematics Report")),
        ("Case Comparison Report", "optional", ("line", "Case Comparison Report")),
        ("General Report", "required", ("line", "General Report")),
        ("Inflow Performance", "plot_only", ("start", "Inflow Performance Curve")),
        ("Inflow / Outflow", "plot_only", ("start", "Inflow / Outflow Performance Curve")),
        ("TDH Curve", "plot_only", ("line", "TDH Curve")),
        ("VSD H-Q Curve", "plot_only", ("line", "VSD H-Q Curve")),
        ("Actual Bottom Pump Curve", "plot_only", ("line", "Actual Bottom Pump Curve")),
        ("Actual Top Pump Curve", "plot_only", ("line", "Actual Top Pump Curve")),
        ("Catalog Top Pump Curve", "plot_only", ("line", "Catalog Top Pump Curve")),
        # Printed by single-pump F3 documents; the contract names only the
        # Top/Bottom taper variants. Not treated as the same role — that would
        # be a judgement call — so they fall through to included-unresolved.
    ],
    # F4 ELS — p1 and p2 carry everything; p3+ are trailing plots, of which the
    # contract names two.
    "F4": [
        ("p1 header · Pumps · Well Completion · Operating Performance · Fluid Properties · Motor M1",
         "required", ("start", "Pumps Top Bottom")),
        ("p2 OPERATING PARAMETERS · FLUID PROPERTIES · OPERATING PERFORMANCE",
         "required", ("start", "OPERATING PARAMETERS")),
        ("Frequency Head chart", "plot_only", ("start", "Frequency Head chart")),
        ("Frequency Power chart", "plot_only", ("start", "Frequency Power chart")),
    ],
    # F5 Baker ProLift — the Sensitivity *table* (p7) and the Sensitivity
    # *chart* (p6) are different pages with the same word on them. The table is
    # keyed on its `Parameter …` header row, never on the title. The case
    # labels differ between documents (`Case #1 Target …` vs
    # `Target / High End / Low End`), so the header row is matched on the row
    # label alone.
    "F5": [
        ("ProLift Summary Report", "required", ("line", "ProLift Summary Report")),
        ("ProLift Detailed Report", "required", ("line", "ProLift Detailed Report")),
        ("Sensitivity case table", "required", ("re", r"^Parameter\s")),
        ("String diagram", "required", ("line", "String diagram")),
        ("Pump Performance", "plot_only", ("line", "Pump Performance")),
        ("Motor", "required", ("line", "Motor")),
    ],
    # F7 Valiant — ~20 pages; the contract names eleven and calls four
    # plot-only.
    "F7": [
        ("GENERAL · System Information · Well Information · Design Properties",
         "required", ("start", "Valiant ZONE")),
        ("Production Summary", "required", ("line", "Production Summary")),
        ("Equipment Summary", "required", ("line", "Equipment Summary")),
        ("PRODUCTION FLUIDS · WELL PRODUCTIVITY · WELL CHARACTERISTICS · DESIGN SEPARATION",
         "required", ("line", "PRODUCTION FLUIDS")),
        ("PVT Correlations", "required", ("line", "PVT Correlations")),
        ("Pump Detail", "required", ("line", "Pump Detail")),
        ("Pump Summary", "required", ("start", "Pump Summary")),
        ("Intake/Separator Detail", "required", ("start", "Intake/Separator Detail")),
        ("Motor Detail", "required", ("line", "Motor Detail")),
        ("Electrical Detail", "required", ("line", "Electrical Detail")),
        ("Equipment List", "required", ("line", "Equipment List")),
        ("VSD Tornado Curve", "plot_only", ("line", "VSD Tornado Curve")),
        ("Freq Charts", "plot_only", ("line", "Freq Charts")),
        ("Inflow Curve", "plot_only", ("line", "Inflow Curve")),
        ("Motor Chart", "plot_only", ("line", "Motor Chart")),
    ],
}

MANIFEST_COLUMNS = [
    "source_file", "family", "family_source", "source_page_count",
    "original_page_no", "sliced_page_no", "page_title_matched", "page_role",
    "required_or_optional", "included", "text_extracted", "n_chars", "route",
    "low_yield_flag", "notes",
]

# PDF fonts in these templates map six characters into the Unicode Private Use
# Area, so no extractor can resolve them — pypdf and pdfplumber both return the
# raw codepoint. Identified from surrounding context; ~6,150 occurrences in the
# reference corpus. All six are separators or label punctuation sitting BESIDE
# numbers, never inside them, so no numeric value is affected.
PUA_MAP = {
    "": "(",   # "TS4 XR (HS Shaft)"  ·  "51.59 (3095.5 rpm)"
    "": ")",   # "(air=1)"  ·  "(STB/D)"
    "": "@",   # "Q @ INT"  ·  "Operating HP @ Design Hz"
    "": "-",   # "SELF-HLB"  ·  "ESP B 400-1750"
    "": ":",   # "Oil Gravity, °API:"  ·  "Water Cut, %:"
    "": "×",  # "5 1/2 × 20 (lb/ft)"  ·  "139.7 × 29.76"
}
