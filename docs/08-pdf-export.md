# Building PDFs

**Rule: construct PDFs with a low-level builder — never convert HTML.** There is
no HTML→PDF path in this product and no "export report to PDF" button, by design.
Do not reach for any HTML-to-PDF converter, `wkhtmltopdf`, a headless browser, or
fpdf2's own `write_html`: those approaches were removed on purpose (no JS charts,
native-library/font breakage in the sandbox). Build the document directly with
**fpdf2** — place text, tables, images, and vector drawing with explicit calls
(`pdf.cell`, `pdf.image`, `pdf.table`, `pdf.draw_path`).

When you need a PDF as a **deliverable file**, that's the only supported route.

Saving a report as a web page needs none of this: the report pane's download
button exports it as a `.zip` (see [`06-reports.md`](06-reports.md)).

> **Exporting an existing report to PDF?** See the field-tested recipe in
> [`recipes/pdf-report.md`](recipes/pdf-report.md) — vector SVG embedding
> (gradients included), which engines actually work in the sandbox
> (weasyprint/cairosvg do **not** — missing native libs), and the
> rasterize-and-verify loop.

## fpdf2

`fpdf2` is installed by default. Its source and docs are indexed in the `code`
RAG corpus under `lib-sources/fpdf2/` — use
`rag_query(..., corpus="code")` for the `FPDF` API, `table.py`, `svg.py`, and the
`docs/`/`tutorial/` guides when you need the exact call.

You write Python that lays the document out page by page — text, tables, images,
vector drawing. It executes real code, so nothing depends on HTML/CSS quirks, and
the resulting text is selectable/copy-pasteable.

```python
def build(ctx):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=16)
    pdf.cell(0, 10, "Quarterly Report", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", size=11)
    with pdf.table() as table:
        for row in (("Region", "Revenue"), ("EMEA", "$1.2M"), ("APAC", "$0.9M")):
            r = table.row()
            for cell in row:
                r.cell(cell)

    pdf_bytes = bytes(pdf.output())   # in-memory PDF bytes
    ctx.log(f"built {len(pdf_bytes)} byte PDF")
    return None
```

### Non-Latin text (Cyrillic, Greek, CJK, accented characters)

fpdf2's built-in fonts (Helvetica/Times/Courier) are **Latin-only** — non-Latin
text renders as blank boxes. Register a Unicode TrueType font first. **DejaVu**
ships with matplotlib (a core dep) and covers Latin/Cyrillic/Greek:

```python
def build(ctx):
    from pathlib import Path
    import matplotlib
    from fpdf import FPDF

    ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"

    pdf = FPDF()
    pdf.add_font("DejaVu", "", str(ttf))   # register the Unicode font
    pdf.add_page()
    pdf.set_font("DejaVu", size=14)
    pdf.cell(0, 10, "Онегин — этюд", new_x="LMARGIN", new_y="NEXT")
    pdf.output("/tmp/out.pdf")
    return None
```

For bold/italic, register the matching DejaVu files
(`DejaVuSans-Bold.ttf`, `DejaVuSans-Oblique.ttf`) under the same family name with
the `style` argument.

## Charts and images

fpdf2 places **images**, so render any chart to a static image first and embed
it:

```python
import io, matplotlib
matplotlib.use("Agg")                      # already the default in report scripts
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
ax.plot([1, 2, 3], [4, 1, 3])
buf = io.BytesIO()
fig.savefig(buf, format="png", bbox_inches="tight")
buf.seek(0)
pdf.image(buf, w=120)                        # embed the PNG
```

matplotlib PNGs embed perfectly. Plotly can emit a static PNG via
`fig.to_image(format="png")` (needs `kaleido`); a matplotlib fallback is the
reliable path. Interactive/JS-drawn visuals (Bokeh, three.js, live Plotly) have
no place in a statically-built PDF — render a static image instead.

## ReportLab

**ReportLab** is also available (it's a transitive dependency) if you need its
heavier typesetting — Platypus flowables, precise flow control. Its source is
*not* in `lib-sources`, so you're working from prior knowledge, not RAG. Prefer
`fpdf2` unless you specifically need ReportLab's advanced layout.

## Delivering the file

`bytes(pdf.output())` gives you the PDF in memory; `pdf.output("/path.pdf")`
writes it to disk. To hand a PDF to the user, write it into the workspace /
python_storage area (see [`07-workspace.md`](07-workspace.md)) so it shows up as a
downloadable artefact.
