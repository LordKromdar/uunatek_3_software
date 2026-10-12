"""
svg_loader.py -- Parse an SVG file into color-grouped polylines.

Uses svgelements, which handles nested transforms, shape primitives
(rect/circle/ellipse/line/polyline/polygon/path) and CSS-style colors.
Curves (cubic/quadratic Beziers, arcs) are flattened into short line
segments based on a length tolerance, since GRBL plotting works best
with plain G1 moves.

Text elements are NOT rendered -- convert text to paths in your vector
editor (Inkscape: Path > Object to Path) before loading.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np
from svgelements import SVG, Shape, Path, Move, Close, Line, Color

from gcode_gen import simplify_polyline

# Any curve is subdivided so no sub-segment is longer than this many
# SVG user-units.  It is re-scaled at load time so that flattening
# quality is roughly constant in *output* millimetres.
DEFAULT_FLATNESS_MM = 0.2


@dataclass
class ColorLayer:
    """All polylines that share one pen color."""
    color: str                                   # '#rrggbb'
    polylines: list = field(default_factory=list)  # list[list[(x, y)]]
    # One entry per polyline: a float (constant 0..1 opacity) or a list of
    # floats, one per point (opacity varies along the stroke, e.g. from an
    # SVG <mask>).  Opacity drives pen pressure (Z) when plotting.
    opacities: list = field(default_factory=list)
    enabled: bool = True

    def total_length(self) -> float:
        total = 0.0
        for line in self.polylines:
            for (x0, y0), (x1, y1) in zip(line, line[1:]):
                total += math.hypot(x1 - x0, y1 - y0)
        return total


@dataclass
class Artwork:
    """A parsed SVG: layers keyed by color, plus its bounding box."""
    layers: list = field(default_factory=list)   # list[ColorLayer]
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


# --------------------------------------------------------------------------
# Paint / opacity resolution
# --------------------------------------------------------------------------
_URL_RE = re.compile(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)")
_LAYER_ID_RE = re.compile(r"color[_\-\s#]*([0-9a-f]{6})$", re.I)
_NO_PAINT = (None, "", "none", "transparent")


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _num(text, default=1.0) -> float:
    """Parse '0.5', '50%' or garbage into a float (garbage -> default)."""
    if text is None:
        return default
    t = str(text).strip()
    try:
        if t.endswith("%"):
            return float(t[:-1]) / 100.0
        return float(t)
    except ValueError:
        return default


def _style_value(elem, name):
    """Value of *name* from the presentation attribute or style=''."""
    style = elem.get("style")
    if style:
        for part in style.split(";"):
            k, _, v = part.partition(":")
            if k.strip() == name:
                return v.strip()
    return elem.get(name)


def _parse_color(text):
    """'rgb(1.5,2,3)', '#abc', names, ... -> (r, g, b, alpha) in 0..255/0..1,
    or None when it is not a plain colour."""
    if text is None:
        return None
    t = str(text).strip()
    if t.lower() in _NO_PAINT or t.startswith("url("):
        return None
    m = re.match(r"rgba?\(\s*([^)]*)\)", t, re.I)
    if m:
        parts = [p for p in re.split(r"[,\s/]+", m.group(1).strip()) if p]
        if len(parts) >= 3:
            def ch(p):
                return (float(p[:-1]) * 2.55 if p.endswith("%")
                        else float(p))
            try:
                r, g, b = (min(max(ch(p), 0.0), 255.0) for p in parts[:3])
                a = _num(parts[3]) if len(parts) > 3 else 1.0
                return r, g, b, min(max(a, 0.0), 1.0)
            except ValueError:
                return None
    try:
        c = Color(t)
        if c.value is None:
            return None
        return float(c.red), float(c.green), float(c.blue), \
            float(c.opacity if c.opacity is not None else 1.0)
    except Exception:
        return None


def _hex(r, g, b) -> str:
    return "#%02x%02x%02x" % (int(round(r)), int(round(g)), int(round(b)))


def _luminance(r, g, b) -> float:
    """Luminance 0..1 (what an SVG luminance mask uses as alpha)."""
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


class _Doc:
    """XML side-view of the SVG: paint servers, masks and group opacity.

    svgelements flattens url(#...) paint to black and ignores masks, so
    those are resolved here straight from the XML tree.
    """

    def __init__(self, filepath: str):
        self.root = ET.parse(filepath).getroot()
        self.parent = {}
        self.by_id = {}
        for p in self.root.iter():
            i = p.get("id")
            if i:
                self.by_id[i] = p
            for c in p:
                self.parent[c] = p
        self._paint_cache = {}
        self._mask_cache = {}

    # ---- ancestors -------------------------------------------------------
    def group_opacity(self, elem) -> float:
        """Product of 'opacity' on the element and all its ancestors."""
        op = 1.0
        while elem is not None:
            v = _style_value(elem, "opacity")
            if v is not None:
                op *= min(max(_num(v), 0.0), 1.0)
            elem = self.parent.get(elem)
        return op

    def layer_hint(self, elem):
        """Colour hint from an ancestor id such as 'Color_daff75'."""
        elem = self.parent.get(elem)
        while elem is not None:
            m = _LAYER_ID_RE.search(elem.get("id") or "")
            if m:
                return "#" + m.group(1).lower()
            elem = self.parent.get(elem)
        return None

    # ---- paint servers ---------------------------------------------------
    def _ref(self, text):
        m = _URL_RE.search(text or "")
        return self.by_id.get(m.group(1)) if m else None

    def resolve_paint(self, text):
        """url(#id) -> (hex, alpha, mask_profile|None), or None."""
        key = text
        if key in self._paint_cache:
            return self._paint_cache[key]
        res = None
        server = self._ref(text)
        if server is not None:
            tag = _local(server.tag)
            if tag == "pattern":
                for child in server.iter():
                    if child is server:
                        continue
                    col = _parse_color(_style_value(child, "fill"))
                    if col is None:
                        continue
                    alpha = col[3]
                    alpha *= _num(_style_value(child, "fill-opacity"))
                    alpha *= _num(_style_value(child, "opacity"))
                    mask = self.mask_profile(child.get("mask"))
                    res = (_hex(*col[:3]), alpha, mask)
                    break
            elif tag in ("linearGradient", "radialGradient"):
                stops = self.gradient_stops(server)
                if stops:
                    r, g, b, a = stops[0][1]
                    res = (_hex(r, g, b), a, None)
        self._paint_cache[key] = res
        return res

    def gradient_stops(self, grad):
        """[(offset, (r, g, b, alpha))] following xlink:href chains."""
        seen = set()
        while grad is not None and id(grad) not in seen:
            seen.add(id(grad))
            stops = []
            for st in grad:
                if _local(st.tag) != "stop":
                    continue
                col = _parse_color(_style_value(st, "stop-color")) \
                    or (0.0, 0.0, 0.0, 1.0)
                a = col[3] * _num(_style_value(st, "stop-opacity"))
                stops.append((min(max(_num(st.get("offset"), 0.0), 0.0), 1.0),
                              (col[0], col[1], col[2], a)))
            if stops:
                return stops
            href = grad.get("{http://www.w3.org/1999/xlink}href") \
                or grad.get("href")
            grad = self.by_id.get((href or "").lstrip("#"))
        return []

    # ---- masks -----------------------------------------------------------
    def mask_profile(self, text):
        """Build a lookup for a luminance mask made of stroked <line>s.

        Returns a list of segments (x1, y1, x2, y2, gx1, gy1, gx2, gy2,
        offsets, lums) in user space, or None when unsupported.  A point
        is looked up by finding the nearest segment, then evaluating that
        segment's gradient there.
        """
        m = _URL_RE.search(text or "")
        if not m:
            return None
        mid = m.group(1)
        if mid in self._mask_cache:
            return self._mask_cache[mid]
        mask = self.by_id.get(mid)
        segs = []
        if mask is not None and _local(mask.tag) == "mask":
            for ln in mask.iter():
                if _local(ln.tag) != "line":
                    continue
                try:
                    x1, y1, x2, y2 = (float(ln.get(k, 0))
                                      for k in ("x1", "y1", "x2", "y2"))
                except ValueError:
                    continue
                stroke = _style_value(ln, "stroke")
                grad = self._ref(stroke)
                if grad is not None:
                    stops = self.gradient_stops(grad)
                    try:
                        gx1, gy1, gx2, gy2 = (float(grad.get(k, d)) for k, d in
                                              (("x1", x1), ("y1", y1),
                                               ("x2", x2), ("y2", y2)))
                    except ValueError:
                        gx1, gy1, gx2, gy2 = x1, y1, x2, y2
                else:
                    col = _parse_color(stroke)
                    stops = [(0.0, col)] if col else []
                    gx1, gy1, gx2, gy2 = x1, y1, x2, y2
                if not stops:
                    continue
                offs = np.array([s[0] for s in stops], dtype=float)
                lums = np.array([_luminance(*s[1][:3]) * s[1][3]
                                 for s in stops], dtype=float)
                segs.append((x1, y1, x2, y2, gx1, gy1, gx2, gy2, offs, lums))
        prof = _MaskProfile(segs) if segs else None
        self._mask_cache[mid] = prof
        return prof


class _MaskProfile:
    """Nearest-segment lookup of mask luminance for many points at once."""

    def __init__(self, segs):
        a = np.array([s[:8] for s in segs], dtype=float)
        self.p1, self.p2 = a[:, 0:2], a[:, 2:4]
        self.g1, self.g2 = a[:, 4:6], a[:, 6:8]
        self.offs = [s[8] for s in segs]
        self.lums = [s[9] for s in segs]

    def sample(self, pts: np.ndarray) -> np.ndarray:
        """Luminance 0..1 at each of *pts* (m, 2) in mask user space."""
        d = self.p2 - self.p1
        l2 = np.maximum((d ** 2).sum(axis=1), 1e-12)
        rel = pts[:, None, :] - self.p1[None, :, :]          # (m, n, 2)
        t = np.clip((rel * d[None]).sum(axis=2) / l2[None], 0.0, 1.0)
        near = self.p1[None] + t[..., None] * d[None]
        dist = ((pts[:, None, :] - near) ** 2).sum(axis=2)
        best = dist.argmin(axis=1)
        out = np.empty(len(pts))
        for j in np.unique(best):
            sel = best == j
            gd = self.g2[j] - self.g1[j]
            gl2 = float((gd ** 2).sum())
            if gl2 < 1e-12:
                u = np.zeros(int(sel.sum()))
            else:
                u = np.clip(((pts[sel] - self.g1[j]) @ gd) / gl2, 0.0, 1.0)
            out[sel] = np.interp(u, self.offs[j], self.lums[j])
        return out


def _element_paint(el, doc):
    """Pick pen colour, constant opacity and optional mask for a shape.

    Order: stroke, then fill (plotters draw outlines), then a colour hint
    from an ancestor id (Color_rrggbb), then black.  Opacity multiplies
    paint alpha, stroke/fill-opacity and every ancestor's opacity.
    """
    xml = doc.by_id.get(getattr(el, "id", None)) if doc else None
    values = getattr(el, "values", {}) or {}

    for name, obj in (("stroke", el.stroke), ("fill", el.fill)):
        raw = values.get(name)
        if raw is None and xml is not None:
            raw = _style_value(xml, name)
        if raw is not None and str(raw).strip().lower() in _NO_PAINT:
            continue
        if raw is not None and str(raw).strip().startswith("url("):
            res = doc.resolve_paint(str(raw)) if doc else None
            if res is None:
                continue
            color, alpha, mask = res
            alpha *= _num(values.get(f"{name}-opacity"))
            return color, alpha, mask, name
        if obj is None or obj.value is None:
            continue
        hexv = obj.hexrgb.lower()
        if name == "fill" and hexv == "#ffffff":
            continue                     # white fill = paper, not ink
        alpha = float(obj.opacity if obj.opacity is not None else 1.0)
        return hexv, alpha, None, name

    hint = doc.layer_hint(xml) if (doc and xml is not None) else None
    return hint or "#000000", 1.0, None, "stroke"


def _element_opacity(el, doc, alpha: float) -> float:
    xml = doc.by_id.get(getattr(el, "id", None)) if doc else None
    if xml is not None:
        grp = doc.group_opacity(xml)
    else:
        grp = _num((getattr(el, "values", {}) or {}).get("opacity"))
    return min(max(alpha * grp, 0.0), 1.0)


def _approx_seg_length(seg) -> float:
    """Cheap upper-bound length via the control polygon (no integration)."""
    pts = []
    for attr in ("start", "control1", "control2", "control", "end"):
        p = getattr(seg, attr, None)
        if p is not None:
            pts.append((p.x, p.y))
    if len(pts) >= 2:
        return sum(math.hypot(x1 - x0, y1 - y0)
                   for (x0, y0), (x1, y1) in zip(pts, pts[1:]))
    try:
        return seg.length(error=1e-2)
    except TypeError:
        return seg.length()


def _flatten_path(path: Path, flatness: float) -> list:
    """Convert one svgelements Path into a list of polylines.

    Curvature-aware: every curve is oversampled well below the target
    tolerance (with a floor of 8 samples so small tight arcs cannot be
    under-sampled into visible facets), then Douglas-Peucker keeps
    points only where curvature demands them. The result is dense on
    tight curves, sparse on gentle ones, with chord error <= flatness.
    """
    polylines = []
    current = []

    def emit():
        nonlocal current
        if len(current) >= 2:
            polylines.append(current)
        current = []

    for seg in path:
        if isinstance(seg, Move):
            emit()
            if seg.end is not None:
                current = [(seg.end.x, seg.end.y)]
        elif isinstance(seg, (Line, Close)):
            if seg.end is not None:
                if not current and seg.start is not None:
                    current = [(seg.start.x, seg.start.y)]
                current.append((seg.end.x, seg.end.y))
        else:
            # CubicBezier / QuadraticBezier / Arc
            if not current and seg.start is not None:
                current = [(seg.start.x, seg.start.y)]
            seg_len = _approx_seg_length(seg)
            steps = int(math.ceil(math.sqrt(seg_len / max(0.5 * flatness,
                                                          1e-9))))
            steps = min(max(steps, 8), 4000)
            ts = np.linspace(0.0, 1.0, steps + 1)[1:]
            pts = seg.npoint(ts)
            current.extend(zip(pts[:, 0].tolist(), pts[:, 1].tolist()))
    emit()
    return [simplify_polyline(pl, flatness) for pl in polylines]


def _to_user_space(points, matrix) -> np.ndarray:
    """Map final (post-transform) points back to the element's user space,
    which is where masks / paint servers are defined."""
    a = np.array([[matrix.a, matrix.c, matrix.e],
                  [matrix.b, matrix.d, matrix.f],
                  [0.0, 0.0, 1.0]], dtype=float)
    inv = np.linalg.inv(a)
    p = np.asarray(points, dtype=float)
    return p @ inv[:2, :2].T + inv[:2, 2]


def load_svg(filepath: str, flatness_mm: float = DEFAULT_FLATNESS_MM,
             expected_output_mm: float = 300.0) -> Artwork:
    """
    Parse *filepath* into an Artwork.

    flatness_mm is the desired curve tolerance in output millimetres;
    it is converted into user-units using a first pass over the bbox.

    Colours and opacities are read from stroke/fill (incl. rgb()/rgba(),
    stroke-opacity/fill-opacity, group opacity, pattern/gradient paint
    servers and per-stroke luminance masks as exported by Line & Form).
    """
    svg = SVG.parse(filepath, reify=False)
    try:
        doc = _Doc(filepath)
    except Exception:
        doc = None

    shapes = [el for el in svg.elements()
              if isinstance(el, Shape) and not isinstance(el, SVG)]

    # First pass: rough bbox to estimate units-per-mm for flattening.
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    paths = []
    for el in shapes:
        try:
            path = abs(Path(el))
        except Exception:
            continue
        if len(path) == 0:
            continue
        bbox = path.bbox()
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        min_x, min_y = min(min_x, x0), min(min_y, y0)
        max_x, max_y = max(max_x, x1), max(max_y, y1)
        color, alpha, mask, _ = _element_paint(el, doc)
        opacity = _element_opacity(el, doc, alpha)
        paths.append((path, color, opacity, mask, el.transform))

    if not paths:
        raise ValueError("No drawable vector shapes found in this SVG. "
                         "(Text must be converted to paths first.)")

    span = max(max_x - min_x, max_y - min_y, 1e-6)
    units_per_mm = span / max(expected_output_mm, 1.0)
    flatness_units = max(flatness_mm * units_per_mm, span * 1e-5)

    # Second pass: flatten and group by color.
    by_color: dict = {}
    for path, color, opacity, mask, matrix in paths:
        for polyline in _flatten_path(path, flatness_units):
            if mask is not None:
                lum = mask.sample(_to_user_space(polyline, matrix))
                op = np.clip(lum * opacity, 0.0, 1.0).tolist()
            else:
                op = opacity
            by_color.setdefault(color, []).append((polyline, op))

    layers = [ColorLayer(color=c, polylines=[p for p, _ in items],
                         opacities=[o for _, o in items])
              for c, items in sorted(by_color.items(),
                                     key=lambda kv: -len(kv[1]))]
    return Artwork(layers=layers, min_x=min_x, min_y=min_y,
                   max_x=max_x, max_y=max_y)
