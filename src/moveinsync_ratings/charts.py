"""Inline-SVG charts: no JavaScript libraries, so the report is one file that works offline,
prints cleanly and survives being emailed. Colours are CSS variables (light + dark themes) taken
from a palette validated for colour-blind readers; every chart also has a visible label or table.

Every mark that carries data has a `data-tip` attribute; a 20-line script in the page turns
those into hover tooltips.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

Fmt = Callable[[float], str]


def pct(v: float, digits: int = 0) -> str:
    return f"{v * 100:.{digits}f}%"


def _tip(text: str) -> str:
    return f' data-tip="{escape(text, quote=True)}" tabindex="0"'


def _nice_max(v: float, steps: Sequence[float] = (0.1, 0.2, 0.25, 0.5, 1.0)) -> float:
    if v <= 0:
        return 1.0
    scale = 1.0
    while v > scale:
        scale *= 10
    while v <= scale / 10:
        scale /= 10
    for s in steps:
        if v <= s * scale:
            return s * scale
    return scale


@dataclass
class Point:
    label: str
    value: float
    lo: float | None = None
    hi: float | None = None
    tip: str = ""
    muted: bool = False


# ── line with confidence band ─────────────────────────────────────────────────────────────────
def line_chart(
    points: Sequence[Point],
    *,
    fmt: Fmt = pct,
    y_max: float | None = None,
    y_min: float = 0.0,
    ticks: int = 4,
    height: int = 220,
    width: int = 720,
    label_every: int = 3,
    series_var: str = "--series-1",
    reference: tuple[float, str] | None = None,
    title: str = "",
) -> str:
    if not points:
        return ""
    left, right, top, bottom = 44, 64, 14, 28
    pw, ph = width - left - right, height - top - bottom
    hi_vals = [p.hi if p.hi is not None else p.value for p in points]
    ymax = y_max or _nice_max(max(hi_vals) * 1.08)
    n = len(points)

    def x(i: int) -> float:
        return left + (pw * i / (n - 1) if n > 1 else pw / 2)

    def y(v: float) -> float:
        return top + ph - ph * (min(max(v, y_min), ymax) - y_min) / (ymax - y_min)

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(title)}">']
    for t in range(ticks + 1):
        v = y_min + (ymax - y_min) * t / ticks
        out.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="tick" x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
    band = [p for p in points if p.lo is not None and p.hi is not None]
    if len(band) == n:
        upper = " ".join(f"{x(i):.1f},{y(p.hi or 0):.1f}" for i, p in enumerate(points))
        lower = " ".join(f"{x(i):.1f},{y(p.lo or 0):.1f}" for i, p in reversed(list(enumerate(points))))
        out.append(f'<polygon class="band" style="fill:var({series_var})" points="{upper} {lower}"/>')
    if reference:
        rv, rl = reference
        out.append(f'<line class="ref" x1="{left}" x2="{left + pw}" y1="{y(rv):.1f}" y2="{y(rv):.1f}"/>')
        out.append(f'<text class="tick" x="{left + pw + 4}" y="{y(rv) + 4:.1f}">{escape(rl)}</text>')
    solid = [p for p in points if not p.muted]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(p.value):.1f}" for i, p in enumerate(solid))
    out.append(f'<path class="line" style="stroke:var({series_var})" d="{path}"/>')
    if len(solid) < n:  # partial last period drawn dashed-free but faded
        i0 = len(solid) - 1
        d = f"M{x(i0):.1f},{y(solid[-1].value):.1f} " + " ".join(
            f"L{x(i):.1f},{y(points[i].value):.1f}" for i in range(len(solid), n)
        )
        out.append(f'<path class="line faded" style="stroke:var({series_var})" d="{d}"/>')
    for i, p in enumerate(points):
        if i % label_every == 0:  # the end label already names the latest period
            out.append(
                f'<text class="tick" x="{x(i):.1f}" y="{height - 8}" text-anchor="middle">{escape(p.label)}</text>'
            )
        out.append(
            f'<circle class="hit" cx="{x(i):.1f}" cy="{y(p.value):.1f}" r="9"{_tip(p.tip or f"{p.label}: {fmt(p.value)}")}/>'
        )
    # The end label marks the last *complete* period; a partial one is drawn faded, unlabelled.
    li = len(solid) - 1
    last = points[li]
    out.append(
        f'<circle class="dot" style="fill:var({series_var})" cx="{x(li):.1f}" cy="{y(last.value):.1f}" r="4.5"/>'
    )
    lx = x(li) + 9 if li == n - 1 else x(li) - 9
    anchor = "start" if li == n - 1 else "end"
    out.append(
        f'<text class="end" x="{lx:.1f}" y="{y(last.value) - 8:.1f}" text-anchor="{anchor}">{escape(last.label)}: {fmt(last.value)}</text>'
    )
    out.append("</svg>")
    return "".join(out)


# ── vertical columns ─────────────────────────────────────────────────────────────────────────
def column_chart(
    points: Sequence[Point],
    *,
    fmt: Fmt = pct,
    y_max: float | None = None,
    height: int = 220,
    width: int = 720,
    label_every: int = 1,
    highlight: Callable[[Point], bool] | None = None,
    title: str = "",
) -> str:
    if not points:
        return ""
    left, right, top, bottom = 44, 12, 18, 28
    pw, ph = width - left - right, height - top - bottom
    ymax = y_max or _nice_max(max(p.value for p in points) * 1.1)
    n = len(points)
    slot = pw / n
    bw = min(24.0, slot * 0.6)

    def y(v: float) -> float:
        return top + ph - ph * min(v, ymax) / ymax

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(title)}">']
    for t in range(5):
        v = ymax * t / 4
        out.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="tick" x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
    for i, p in enumerate(points):
        cx = left + slot * i + slot / 2
        h = max(ph * min(p.value, ymax) / ymax, 0.0)
        var = "--series-2" if highlight and highlight(p) else "--series-1"
        r = min(4.0, h / 2, bw / 2)
        x0, y0 = cx - bw / 2, top + ph - h
        d = (
            f"M{x0:.1f},{top + ph:.1f} L{x0:.1f},{y0 + r:.1f} Q{x0:.1f},{y0:.1f} {x0 + r:.1f},{y0:.1f} "
            f"L{x0 + bw - r:.1f},{y0:.1f} Q{x0 + bw:.1f},{y0:.1f} {x0 + bw:.1f},{y0 + r:.1f} L{x0 + bw:.1f},{top + ph:.1f} Z"
        )
        out.append(f'<path class="bar" style="fill:var({var})" d="{d}"/>')
        out.append(
            f'<rect class="hit" x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{ph}"'
            f"{_tip(p.tip or f'{p.label}: {fmt(p.value)}')}/>"
        )
        if i % label_every == 0:
            out.append(
                f'<text class="tick" x="{cx:.1f}" y="{height - 8}" text-anchor="middle">{escape(p.label)}</text>'
            )
    out.append("</svg>")
    return "".join(out)


# ── horizontal ranked bars with CI whiskers ──────────────────────────────────────────────────
@dataclass
class Row:
    label: str
    value: float
    lo: float | None = None
    hi: float | None = None
    color_var: str = "--series-1"
    tip: str = ""
    note: str = ""


def hbar_chart(
    rows: Sequence[Row], *, fmt: Fmt = pct, width: int = 720, x_max: float | None = None, title: str = ""
) -> str:
    if not rows:
        return ""
    label_w, right, row_h, top = 250, 70, 30, 6
    pw = width - label_w - right
    xmax = x_max or _nice_max(max((r.hi if r.hi is not None else r.value) for r in rows) * 1.05)
    height = top + row_h * len(rows) + 22

    def x(v: float) -> float:
        return label_w + pw * min(max(v, 0), xmax) / xmax

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(title)}">']
    for t in range(5):
        v = xmax * t / 4
        out.append(f'<line class="grid" x1="{x(v):.1f}" x2="{x(v):.1f}" y1="{top}" y2="{height - 20}"/>')
        out.append(f'<text class="tick" x="{x(v):.1f}" y="{height - 6}" text-anchor="middle">{fmt(v)}</text>')
    for i, r in enumerate(rows):
        cy = top + row_h * i + row_h / 2
        bh = 14.0
        w = max(x(r.value) - label_w, 0.0)
        rad = min(4.0, w / 2)
        x0, y0 = label_w, cy - bh / 2
        d = (
            f"M{x0:.1f},{y0:.1f} L{x0 + w - rad:.1f},{y0:.1f} Q{x0 + w:.1f},{y0:.1f} {x0 + w:.1f},{y0 + rad:.1f} "
            f"L{x0 + w:.1f},{y0 + bh - rad:.1f} Q{x0 + w:.1f},{y0 + bh:.1f} {x0 + w - rad:.1f},{y0 + bh:.1f} L{x0:.1f},{y0 + bh:.1f} Z"
        )
        out.append(
            f'<text class="rowlabel" x="{label_w - 10}" y="{cy + 4:.1f}" text-anchor="end">{escape(r.label)}</text>'
        )
        out.append(f'<path class="bar" style="fill:var({r.color_var})" d="{d}"/>')
        end = x(r.value)
        if r.lo is not None and r.hi is not None:
            out.append(f'<line class="whisker" x1="{x(r.lo):.1f}" x2="{x(r.hi):.1f}" y1="{cy:.1f}" y2="{cy:.1f}"/>')
            out.append(
                f'<line class="whisker" x1="{x(r.lo):.1f}" x2="{x(r.lo):.1f}" y1="{cy - 4:.1f}" y2="{cy + 4:.1f}"/>'
            )
            out.append(
                f'<line class="whisker" x1="{x(r.hi):.1f}" x2="{x(r.hi):.1f}" y1="{cy - 4:.1f}" y2="{cy + 4:.1f}"/>'
            )
            end = max(end, x(r.hi))
        out.append(f'<text class="value" x="{end + 6:.1f}" y="{cy + 4:.1f}">{fmt(r.value)}{escape(r.note)}</text>')
        out.append(
            f'<rect class="hit" x="0" y="{cy - row_h / 2:.1f}" width="{width}" height="{row_h}"'
            f"{_tip(r.tip or f'{r.label}: {fmt(r.value)}')}/>"
        )
    out.append("</svg>")
    return "".join(out)


# ── 100% stacked bars (star mix) ─────────────────────────────────────────────────────────────
STAR_VARS = ["--div-neg-2", "--div-neg-1", "--div-mid", "--div-pos-1", "--div-pos-2"]


def star_mix_chart(groups: Sequence[tuple[str, Sequence[int]]], *, width: int = 720, title: str = "") -> str:
    label_w, right, row_h, top = 170, 10, 40, 4
    pw = width - label_w - right
    height = top + row_h * len(groups) + 4
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(title)}">']
    for gi, (name, counts) in enumerate(groups):
        total = sum(counts) or 1
        cy = top + row_h * gi
        out.append(f'<text class="rowlabel" x="{label_w - 10}" y="{cy + 22}" text-anchor="end">{escape(name)}</text>')
        x0 = float(label_w)
        for s, c in enumerate(counts):
            w = pw * c / total
            if w <= 0:
                continue
            seg_w = max(w - 2, 0.5)
            out.append(
                f'<rect class="seg" x="{x0:.1f}" y="{cy + 8}" width="{seg_w:.1f}" height="22" rx="2" style="fill:var({STAR_VARS[s]})"'
                f"{_tip(f'{name}: {s + 1}★ = {c:,} ({c / total:.0%})')}/>"
            )
            if w > 44:
                ink = "on-dark" if s in (0, 4) else "on-light"
                out.append(
                    f'<text class="seglabel {ink}" x="{x0 + w / 2:.1f}" y="{cy + 23}" text-anchor="middle">{s + 1}★ {c / total:.0%}</text>'
                )
            x0 += w
    out.append("</svg>")
    return "".join(out)


# ── release dot plot ─────────────────────────────────────────────────────────────────────────
def release_chart(rows: Sequence[dict[str, object]], *, baseline: float, width: int = 720, title: str = "") -> str:
    if not rows:
        return ""
    left, right, top, bottom = 44, 12, 14, 46
    height = 250
    pw, ph = width - left - right, height - top - bottom
    vals = [float(r["hi"]) for r in rows]  # type: ignore[arg-type]
    ymax = _nice_max(max(vals) * 1.05)
    n = len(rows)
    slot = pw / n

    def y(v: float) -> float:
        return top + ph - ph * min(v, ymax) / ymax

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(title)}">']
    for t in range(5):
        v = ymax * t / 4
        out.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="tick" x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{pct(v)}</text>')
    out.append(f'<line class="ref" x1="{left}" x2="{left + pw}" y1="{y(baseline):.1f}" y2="{y(baseline):.1f}"/>')
    for i, r in enumerate(rows):
        cx = left + slot * i + slot / 2
        p, lo, hi = float(r["p"]), float(r["lo"]), float(r["hi"])  # type: ignore[arg-type]
        flag = str(r.get("flag") or "")
        cls = {"worse": "critical", "watch": "serious", "better": "good"}.get(flag, "neutral")
        out.append(f'<line class="whisker" x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(lo):.1f}" y2="{y(hi):.1f}"/>')
        out.append(f'<circle class="pt {cls}" cx="{cx:.1f}" cy="{y(p):.1f}" r="5"/>')
        out.append(
            f'<text class="tick vlabel" transform="translate({cx + 3:.1f},{height - bottom + 10}) rotate(55)">{escape(str(r["version"]))}</text>'
        )
        out.append(
            f'<rect class="hit" x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{ph}"{_tip(str(r["tip"]))}/>'
        )
    out.append("</svg>")
    return "".join(out)


# ── sparkline ────────────────────────────────────────────────────────────────────────────────
def sparkline(
    values: Sequence[float], *, width: int = 110, height: int = 26, tips: Sequence[str] = (), cls: str = "spark"
) -> str:
    if not values:
        return ""
    vmax = max(max(values), 1e-9)
    n = len(values)

    def x(i: int) -> float:
        return 3 + (width - 6) * i / max(n - 1, 1)

    def y(v: float) -> float:
        return height - 4 - (height - 8) * v / vmax

    d = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    out = [f'<svg viewBox="0 0 {width} {height}" class="{cls}" aria-hidden="true">', f'<path d="{d}"/>']
    out.append(f'<circle cx="{x(n - 1):.1f}" cy="{y(values[-1]):.1f}" r="2.6"/>')
    for i, t in enumerate(tips):
        out.append(f'<rect class="hit" x="{x(i) - 5:.1f}" y="0" width="10" height="{height}"{_tip(t)}/>')
    out.append("</svg>")
    return "".join(out)
