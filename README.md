# PlotStudio

A lightweight, crash-free desktop controller for UUNA TEK / iDraw pen
plotters (DrawCore boards running GRBL 1.1). Loads multi-color SVGs,
lets you resize and position the artwork on the paper, and streams
G-code over USB serial — pausing between colors so you can swap pens.

## Install (Windows)

1. Install Python 3.10+ from python.org (tick "Add to PATH").
2. In a terminal, from this folder:

       pip install -r requirements.txt
       python main.py

The plotter shows up as a COM port (the driver is the same one the
stock software uses; if the port is missing, install the CH340/CDC
driver from the UUNA TEK site). Close the stock software first — only
one program can hold the serial port.

## First run — calibrate before plotting

1. **Connect**: pick the COM port, click Connect. After the banner,
   the app reads the firmware settings and reports its speed limits.
   If homing is enabled in the firmware ($22=1) it homes automatically
   and sets the work origin (X0 Y0) at Home -- the carriage will move
   on connect. If homing is disabled ($22=0) it says so and uses the
   current carriage position as the origin instead (enable homing with
   `$22=1` if your machine has limit switches). The pen parks back at
   the origin before every pen change and when the plot finishes. You
   can re-zero anywhere with `Set origin here`.
2. **Check pen Z direction**: with a pen fitted, click `Pen down` then
   `Pen up`. Defaults: **Z0 = pen raised, Z10 = pen on paper**. If
   your machine moves the opposite way, swap the two Z values.
3. **Check orientation**: the preview shows the paper as the plotter
   sees it: the red cross (top-left) is machine X0 Y0, X grows right,
   Y grows down. Plot a small asymmetric test; it should match the
   preview. If it comes out mirrored, tick `Mirror X` / `Mirror Y`:
   they flip the artwork in place, in the preview and in the G-code.
4. Do a dry run with no pen (or pen high) on a small test SVG before
   committing to a 3-hour plot.

## Plotting a multi-color SVG

1. `Open SVG...` — loading runs in the background, so even huge
   files keep the app responsive. Paths are grouped into layers by
   stroke color. (Convert text to paths in Inkscape first:
   *Path → Object to Path*.)
2. Units default to inches with a 12 x 12 in paper and artwork, and
   offsets default to 0 (switchable to millimeters in the size box).
   `Fit to paper` fills the paper edge-to-edge; or type an exact
   width/height (aspect ratio locks by default).
   Offset X/Y positions the artwork relative to the origin. G-code is
   always emitted in millimeters regardless of display units.
3. Drag layers to set plot order; untick any you want to skip.
4. Click `Plot`. Before each layer the pen parks at the origin and a
   dialog asks you to fit the pen for that color — click OK to draw.
5. `Pause`/`Resume` use GRBL feed-hold, so they're instant and safe.
   `Stop` decelerates the machine to a controlled halt first, then
   resets the board, lifts the pen, and returns the carriage to Home
   (via homing when available, otherwise via recorded machine
   coordinates) with the work origin restored -- you can start the
   next plot immediately.

`Save G-code...` exports the whole job (with `M0` pauses between
colors) for use with UGS, CNCjs, or any other sender.

## Speed

Streaming uses GRBL's character-counting protocol (the controller's
receive buffer is kept full rather than waiting for an `ok` per line),
so the motion planner always has lookahead and can sustain high feeds
through short segments. On connect the app also reads the firmware
settings and reports the machine's own caps: `$110/$111` clamp the
maximum feed rate, and `$120/$121` (acceleration) decide how quickly
it can reach it -- if plots still feel slow, those settings on the
board are the remaining limit, not the sender. GRBL also slows down
through tight corners by design (junction deviation, `$11`).

## Tuning

- **Draw feed**: defaults to 10,000 mm/min; the boards are rated to
  12,000. Fine nibs like slower speeds.
- **Curve quality**: the selector in the size panel sets the maximum
  deviation of straight segments from the true curve (Draft 0.5 mm to
  Ultra fine 0.02 mm; default Fine 0.05 mm). Flattening is
  curvature-aware, so tight little circles get the points they need
  while gentle sweeps stay economical. Changing quality re-flattens
  the loaded SVG in the background, keeping your size, offsets, and
  layer selections. Finer quality = smoother curves but more G-code
  and slightly slower plots; path simplification scales with the
  chosen quality automatically.
- **Acceleration is usually the real speed limit**: at $120/$121=300
  mm/s^2 the machine needs ~67 mm just to ramp up to 12,000 mm/min, so
  short strokes never reach top speed no matter what F says. Check
  your values by typing `$$` into the manual command box; raise with
  e.g. `$120=800` and `$121=800` (the vendor software typically runs
  higher acceleration than GRBL defaults). Change gradually and watch
  for skipped steps or excessive vibration.
- **Travel moves** use `G0` rapids; set `use_rapids=False` in
  `gcode_gen.PlotSettings` if you prefer capped-speed travel.

## Files

    main.py           GUI (PySide6)
    svg_loader.py     SVG parsing + curve flattening + color grouping
    gcode_gen.py      scaling transform, path ordering, G-code emission
    grbl_streamer.py  threaded serial sender (pause/resume/abort)
