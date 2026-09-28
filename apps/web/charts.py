"""
Chart geometry for inline SVG. Python does the math; templates draw it (see web/_chart_*.html).
Shapes and spacing follow the mock's CH.line / CH.bars / CH.hbars / CH.donut. The SVGs use a fixed viewBox and scale with CSS.
"""
import math
from collections.abc import Callable

W = 600


def nice_max(v: float) -> float:
    if v <= 0:
        return 1
    p = 10 ** math.floor(math.log10(v))
    f = v / p
    return (1 if f <= 1 else 2 if f <= 2 else 2.5 if f <= 2.5 else 5 if f <= 5 else 10) * p


def _grid(ys: list[float], labels: list[str]) -> list[dict]:
    """Horizontal gridlines with their axis labels (text sits 4px below the line's y to center on it)."""
    return [{"y": round(y, 1), "text_y": round(y + 4, 1), "label": label} for y, label in zip(ys, labels, strict=True)]


def line(labels: list[str], series: list[dict], y_min: float, y_max: float, fmt: Callable, target: float | None = None,
         target_label: str = "", h: int = 200) -> dict:
    """`series`: [{"name", "color", "dash": bool, "values": [float | None]}]. None leaves a gap."""
    pl, pr, pt, pb = 36, 10, 12, 24
    iw, ih, n = W - pl - pr, h - pt - pb, len(labels)
    if y_max == y_min:
        y_max = y_min + 1

    def x(i):
        return pl + (i / (n - 1) * iw if n > 1 else iw / 2)

    def y(v):
        return pt + ih - (v - y_min) / (y_max - y_min) * ih

    grid = _grid([y(v) for v in (y_min + (y_max - y_min) * t / 4 for t in range(5))], [fmt(y_min + (y_max - y_min) * t / 4) for t in range(5)])
    step = 2 if n > 8 else 1
    xlabels = [{"x": round(x(i), 1), "label": label} for i, label in enumerate(labels) if i % step == 0]
    paths = []
    for s in series:
        d, pen, dots = [], False, []
        for i, v in enumerate(s["values"]):
            if v is None:
                pen = False
                continue
            d.append(f"{'L' if pen else 'M'}{x(i):.1f} {y(v):.1f}")
            pen = True
            dots.append({"x": round(x(i), 1), "y": round(y(v), 1), "title": f"{labels[i]}: {fmt(v)}"})
        paths.append({**s, "d": " ".join(d), "dots": dots})
    return {"w": W, "h": h, "left": pl, "tick_x": pl - 6, "right": W - pr, "label_y": h - 6, "grid": grid, "xlabels": xlabels, "paths": paths,
            "target_y": round(y(target), 1) if target is not None else None, "target_label": target_label, "legend": series}


def stacked_bars(labels: list[str], series: list[dict], fmt: Callable = lambda v: f"{v:.0f}", h: int = 200) -> dict:
    """`series`: [{"name", "color", "values": [number]}], stacked bottom-up in list order."""
    pl, pr, pt, pb = 36, 8, 10, 24
    iw, ih, n = W - pl - pr, h - pt - pb, len(labels)
    totals = [sum(s["values"][i] for s in series) for i in range(n)]
    y_max = nice_max(max(totals, default=0))
    grid = _grid([pt + ih - t / 4 * ih for t in range(5)], [fmt(y_max * t / 4) for t in range(5)])
    bw = iw / n if n else iw
    w = min(bw * 0.62, 46)
    bars, xlabels = [], []
    for i, label in enumerate(labels):
        acc, cx = 0, pl + bw * (i + 0.5)
        for s in series:
            v = s["values"][i]
            if not v:
                continue
            bars.append({"x": round(cx - w / 2, 1), "y": round(pt + ih - (acc + v) / y_max * ih, 1), "w": round(w, 1), "h": round(v / y_max * ih, 1),
                         "color": s["color"], "title": f"{label} · {s['name']}: {fmt(v)}"})
            acc += v
        xlabels.append({"x": round(cx, 1), "label": label})
    return {"w": W, "h": h, "left": pl, "tick_x": pl - 6, "right": W - pr, "label_y": h - 6, "grid": grid, "bars": bars, "xlabels": xlabels, "legend": series}


def hbars(items: list[tuple[str, float]], fmt: Callable, color: str = "var(--accent)", rh: int = 26, marker: float | None = None,
          marker_label: str = "Benchmark") -> dict:
    """Horizontal bars. `marker` draws the mock's red benchmark tick at that value on every row."""
    pl, pr = min(170, round(W * 0.34)), 66
    iw, h = W - pl - pr, len(items) * rh + 6
    top = nice_max(max([v for _, v in items] + ([marker] if marker is not None else []), default=1))
    rows = []
    for i, (label, value) in enumerate(items):
        y0, bw = i * rh + 4, max(2, value / top * iw)
        rows.append({"label": label if len(label) <= 24 else label[:23] + "…", "full_label": label, "text_y": y0 + rh / 2 + 1, "bar_y": y0 + 4,
                     "bar_w": round(bw, 1), "bar_h": rh - 12, "value": fmt(value), "value_x": round(pl + bw + 6, 1),
                     "marker_y1": y0 + 1, "marker_y2": y0 + rh - 5})
    return {"w": W, "h": h, "left": pl, "label_x": pl - 8, "color": color, "rows": rows,
            "marker_x": round(pl + marker / top * iw, 1) if marker is not None else None,
            "marker_title": f"{marker_label}: {fmt(marker)}" if marker is not None else ""}


def donut(items: list[dict], center: str = "", center_label: str = "", size: int = 150) -> dict:
    """`items`: [{"label", "value", "color", "text"}]; `text` is the formatted value for the legend and tooltips. Follows the mock's CH.donut."""
    r, c = size / 2 - 12, size / 2
    total = sum(it["value"] for it in items) or 1
    circ = 2 * math.pi * r
    arcs, acc = [], 0.0
    for it in items:
        f = it["value"] / total
        arcs.append({"color": it["color"], "dash": f"{f * circ:.2f} {circ - f * circ:.2f}", "offset": f"{-acc * circ + 0.0:.2f}",  # + 0.0: no "-0.00"
                     "title": f"{it['label']}: {it['text']}"})
        acc += f
    return {"size": size, "c": c, "r": r, "arcs": arcs, "legend": items, "center": center, "center_label": center_label,
            "center_y": c - 2, "label_y": c + 15}
