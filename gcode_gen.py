"""
gcode_gen.py -- Turn color layers into GRBL-flavoured G-code.

Coordinate conventions for the UUNA TEK / iDraw DrawCore boards:
  * Firmware is GRBL 1.1 over USB serial (115200 baud).
  * The pen lift is a real Z axis (0..~10 mm). Z0 is the pen RAISED
    and larger Z lowers the pen to the paper. Both are configurable.
  * The machine homes ($H) to a corner; users typically set the
    front-left corner of the paper as X0 Y0 with G92.
  * SVG Y grows downward, machine Y grows away from the operator,
    so Y is flipped by default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class PlotSettings:
    out_width_mm: float = 304.8     # artwork width on paper (12 in)
    out_height_mm: float = 304.8    # artwork height on paper
    offset_x_mm: float = 0.0        # margin from machine X0
    offset_y_mm: float = 0.0        # margin from machine Y0
    flip_x: bool = False            # mirror machine X
    flip_y: bool = False            # mirror machine Y
    pen_up_z: float = 0.0           # mm (Z0 = raised on iDraw)
    pen_down_z: float = 10.0        # mm (full pressure, opacity 1)
    pen_touch_z: float = 5.0        # mm (pen just touches, opacity 0+)
    pressure_from_opacity: bool = True  # Z depth from SVG opacity
    min_opacity: float = 0.02       # below this the pen lifts (invisible ink)
    opacity_tol: float = 0.03       # max opacity error kept when simplifying
    draw_feed: float = 10000.0      # mm/min for drawing moves
    travel_feed: float = 8000.0     # mm/min for pen-up moves (G1-limited)
    z_feed: float = 2500.0          # mm/min for pen lift/drop
    use_rapids: bool = True         # G0 for travel instead of G1
    simplify_mm: float = 0.05       # merge near-collinear points (0 = off)


def simplify_indices(points: list, epsilon: float, values=None,
                     value_eps: float = 0.0) -> list:
    """
    Ramer-Douglas-Peucker simplification (iterative, numpy).
    Returns the sorted indices of the points to keep.

    Uniform curve sampling leaves many nearly-collinear points; merging
    them into longer segments matters for speed: GRBL only accelerates
    to what it can brake from within its ~15-move lookahead buffer, so
    longer segments let the planner actually reach the commanded feed.
    Max deviation from the original path is bounded by *epsilon*.

    If *values* (one number per point, e.g. opacity) is given, a point is
    also kept when dropping it would make the linearly interpolated value
    deviate by more than *value_eps*, so pen pressure follows the artwork.
    """
    n = len(points)
    if n < 3 or (epsilon <= 0 and values is None):
        return list(range(n))
    pts = np.asarray(points, dtype=float)
    vals = None if values is None else np.asarray(values, dtype=float)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = pts[a + 1:b]
        d = pts[b] - pts[a]
        norm = math.hypot(d[0], d[1])
        if norm < 1e-12:
            dist = np.hypot(seg[:, 0] - pts[a][0], seg[:, 1] - pts[a][1])
        else:
            dist = np.abs((seg[:, 0] - pts[a][0]) * d[1]
                          - (seg[:, 1] - pts[a][1]) * d[0]) / norm
        i = int(np.argmax(dist))
        split = i if dist[i] > epsilon else None
        if vals is not None:
            # Interpolate by distance travelled along the chord.
            if norm < 1e-12:
                frac = np.linspace(0.0, 1.0, b - a + 1)[1:-1]
            else:
                frac = np.clip(((seg[:, 0] - pts[a][0]) * d[0]
                                + (seg[:, 1] - pts[a][1]) * d[1])
                               / (norm * norm), 0.0, 1.0)
            err = np.abs(vals[a + 1:b] - (vals[a] + frac * (vals[b] - vals[a])))
            j = int(np.argmax(err))
            if split is None and err[j] > value_eps:
                split = j
        if split is not None:
            idx = a + 1 + split
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return np.nonzero(keep)[0].tolist()


def simplify_polyline(points: list, epsilon: float) -> list:
    """Geometry-only RDP simplification (see simplify_indices)."""
    if epsilon <= 0 or len(points) < 3:
        return points
    return [tuple(points[i]) for i in simplify_indices(points, epsilon)]


def make_transform(artwork, s: PlotSettings):
    """
    Return f(x, y) -> (mx, my) mapping SVG user units to machine mm.
    flip_x / flip_y mirror the output to match the machine's homed
    orientation; the on-screen preview is unaffected by them.
    """
    sx = s.out_width_mm / max(artwork.width, 1e-9)
    sy = s.out_height_mm / max(artwork.height, 1e-9)

    def transform(x: float, y: float):
        if s.flip_x:
            mx = (artwork.max_x - x) * sx + s.offset_x_mm
        else:
            mx = (x - artwork.min_x) * sx + s.offset_x_mm
        if s.flip_y:
            my = (artwork.max_y - y) * sy + s.offset_y_mm
        else:
            my = (y - artwork.min_y) * sy + s.offset_y_mm
        return mx, my

    return transform


def pressure_z(opacity: float, s: PlotSettings) -> float:
    """Map opacity 0..1 to Z between touch and full pressure."""
    if not s.pressure_from_opacity:
        return s.pen_down_z
    opacity = min(max(opacity, 0.0), 1.0)
    return s.pen_touch_z + opacity * (s.pen_down_z - s.pen_touch_z)


def _opacity_runs(points: list, op, s: PlotSettings) -> list:
    """Split one polyline into drawable runs [(points, ops|None)].

    *op* is a constant opacity or a per-point list.  With opacity-driven
    pressure, stretches whose opacity is below s.min_opacity are dropped
    (the pen lifts there) instead of scratching the paper.
    """
    if not s.pressure_from_opacity:
        return [(points, None)]
    if not isinstance(op, (list, tuple, np.ndarray)):
        return [(points, None)] if op >= s.min_opacity else []
    keep_idx = simplify_indices(points, s.simplify_mm, op, s.opacity_tol)
    pts = [points[i] for i in keep_idx]
    ops = [float(op[i]) for i in keep_idx]
    runs, cur_p, cur_o = [], [], []
    for p, o in zip(pts, ops):
        if o >= s.min_opacity:
            cur_p.append(p)
            cur_o.append(o)
        else:
            if len(cur_p) >= 2:
                runs.append((cur_p, cur_o))
            cur_p, cur_o = [], []
    if len(cur_p) >= 2:
        runs.append((cur_p, cur_o))
    return runs


def order_polylines(polylines: list) -> list:
    """
    Greedy nearest-neighbour ordering to cut pen-up travel time.
    Also reverses polylines when their far end is closer.
    """
    remaining = list(polylines)
    ordered = []
    cx, cy = 0.0, 0.0
    while remaining:
        best_i, best_rev, best_d = 0, False, float("inf")
        for i, line in enumerate(remaining):
            hx, hy = line[0][0]
            tx, ty = line[0][-1]
            dh = (hx - cx) ** 2 + (hy - cy) ** 2
            dt = (tx - cx) ** 2 + (ty - cy) ** 2
            if dh < best_d:
                best_i, best_rev, best_d = i, False, dh
            if dt < best_d:
                best_i, best_rev, best_d = i, True, dt
        line, op = remaining.pop(best_i)
        if best_rev:
            line = line[::-1]
            if isinstance(op, list):
                op = op[::-1]
        ordered.append((line, op))
        cx, cy = line[-1]
    return ordered


def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def generate_layer_gcode(layer, artwork, s: PlotSettings) -> list:
    """G-code lines for one color layer (no header/footer)."""
    tf = make_transform(artwork, s)
    lines = []
    travel_cmd = "G0" if s.use_rapids else f"G1 F{_fmt(s.travel_feed)}"

    ops = layer.opacities or [1.0] * len(layer.polylines)
    runs = []   # (points, ops list | scalar)
    for pl, op in zip(layer.polylines, ops):
        mp = [tf(x, y) for x, y in pl]
        if isinstance(op, (list, tuple, np.ndarray)):
            for rp, ro in _opacity_runs(mp, op, s):
                runs.append((rp, ro if ro is not None else 1.0))
        else:
            for rp, _ in _opacity_runs(mp, op, s):
                runs.append((simplify_polyline(rp, s.simplify_mm), op))

    for pl, op in order_polylines(runs):
        is_var = isinstance(op, list)
        x0, y0 = pl[0]
        lines.append(f"{travel_cmd} X{_fmt(x0)} Y{_fmt(y0)}")
        z_of = (lambda i: pressure_z(op[i], s)) if is_var \
            else (lambda i: pressure_z(op, s))
        z = z_of(0)
        lines.append(f"G1 Z{_fmt(z)} F{_fmt(s.z_feed)}")
        first = True
        for i in range(1, len(pl)):
            x, y = pl[i]
            feed = f" F{_fmt(s.draw_feed)}" if first else ""
            zi = z_of(i)
            zpart = ""
            if abs(zi - z) >= 0.005:
                zpart = f" Z{_fmt(zi)}"
                z = zi
            lines.append(f"G1 X{_fmt(x)} Y{_fmt(y)}{zpart}{feed}")
            first = False
        lines.append(f"G1 Z{_fmt(s.pen_up_z)} F{_fmt(s.z_feed)}")
    return lines


def header_gcode(s: PlotSettings) -> list:
    return [
        "G21",                       # millimetres
        "G90",                       # absolute positioning
        f"G1 Z{_fmt(s.pen_up_z)} F{_fmt(s.z_feed)}",   # make sure pen is up
    ]


def footer_gcode(s: PlotSettings) -> list:
    return [
        f"G1 Z{_fmt(s.pen_up_z)} F{_fmt(s.z_feed)}",
        "G0 X0 Y0",                  # park at work origin
    ]


def segment_stats(gcode_lines: list) -> tuple:
    """(count, avg_len_mm) of drawing segments in generated G-code."""
    import re as _re
    count, total = 0, 0.0
    last = None
    for line in gcode_lines:
        if line.startswith("G1 X") or line.startswith("G0 X"):
            m = _re.match(r"G[01] X(-?[\d.]+) Y(-?[\d.]+)", line)
            if not m:
                continue
            pt = (float(m.group(1)), float(m.group(2)))
            if line.startswith("G1 X") and last is not None:
                count += 1
                total += math.hypot(pt[0] - last[0], pt[1] - last[1])
            last = pt
    return count, (total / count if count else 0.0)


def estimate_time_minutes(layer, artwork, s: PlotSettings) -> float:
    """Very rough plot-time estimate for one layer."""
    tf = make_transform(artwork, s)
    draw = 0.0
    for pl in layer.polylines:
        pts = [tf(x, y) for x, y in pl]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            draw += math.hypot(x1 - x0, y1 - y0)
    lifts = len(layer.polylines)
    z_travel = lifts * 2 * abs(s.pen_up_z - s.pen_down_z)
    return draw / max(s.draw_feed, 1) + z_travel / max(s.z_feed, 1)
