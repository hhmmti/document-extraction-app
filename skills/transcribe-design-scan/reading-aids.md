# Reading aids

For the transcribe-design-scan skill: how to type the parts of a design document that are not plain lines of text.
The rule over all of them is the one in SKILL.md: type only what the page prints, exactly as printed, with `[?]` for
what cannot be read and the reading then `[?]` for what is in doubt.

## Vendor layouts

- **XSize** (Extract Production Services' XSize Design Program) prints landscape tables on portrait pages: the table
  runs up the page, its text turned sideways. Read it upright. Crop the table with `--rotate 90`, or `--rotate 270`
  if the text then reads upside down, and type it as the table it is, its rows as rows.
- Any other page printed sideways is read the same way: turned upright first, then typed.

## Tables

- One printed row per line, its cells in their columns, the columns aligned with spaces.
- Every column is kept, every scenario's or case's column among them, even where the columns repeat a value.
- A column's header goes above it, in as many lines as it is printed; its unit stays where it is printed, in the
  header or beside the value.
- A cell printed empty stays empty: leave its column blank, and do not move the next value into it.
- A cell whose text wraps onto a second printed line keeps the second line, under the first, in the same column.
- A table that runs on to the next page is typed on each page as that page prints it, headers and all.

## Drawings

A wellbore or pump schematic, a completion diagram or a string drawing:

```
[Drawing: labels from top to bottom]
<the first label, as printed>
<the next label, as printed>
```

- The labels go in the order they are drawn, from the top of the drawing to the bottom, one per line.
- A label is typed exactly as printed, its numbers and units with it: a depth, a model, a stage count.
- Nothing is described: no shapes, colours, arrows or what the drawing seems to show.

## Charts

A pump curve, a tornado chart, any plot:

- its title, its legend, its axis titles and the numbers printed on its axes and beside its points or lines, each as
  printed;
- nothing read off a plotted line or bar, however clear: a value the chart does not print as text is not typed.

## Marks

| On the page | Typed as |
|---|---|
| handwriting | `[Handwritten: <its text>]`, with `[?]` inside for what cannot be read |
| a stamp | `[Stamp: <its text>]` |
| a signature | `[Signature]` |
| a logo | `[Logo: <its name as printed>]` |
| a blank page | `[Blank page]` |

## Units and pressures

- Every unit is typed as printed: `ft`, `FT`, `ft TVD`, `°F`, `deg F`, `bbl/d`, `BPD`, `scf/stb`.
- A pressure keeps its basis as printed: `psia`, `psig`, or a bare `psi`. A pressure printed with no unit stays with
  none.
- A specific gravity and an API gravity are different things: type whichever the page prints, under its own label.
