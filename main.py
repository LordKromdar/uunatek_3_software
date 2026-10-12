"""
PlotStudio -- a lightweight controller for UUNA TEK / iDraw GRBL pen plotters.

    python main.py

Workflow: Open SVG -> size/position it on the paper -> connect ->
home & set origin -> Plot. The app pauses between colors so you can
swap pens, and can also export the G-code to a file.
"""

from __future__ import annotations

import sys
import os
import threading

from PySide6.QtCore import Qt, QThread, Signal, QSettings
from PySide6.QtGui import (QColor, QPainter, QPen, QBrush, QIcon, QPixmap,
                           QPainterPath)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QGroupBox, QPushButton, QDoubleSpinBox, QCheckBox, QComboBox, QLabel,
    QListWidget, QListWidgetItem, QGraphicsView, QGraphicsScene, QFileDialog,
    QPlainTextEdit, QProgressBar, QMessageBox, QAbstractItemView, QGridLayout,
    QLineEdit, QScrollArea, QFrame,
)

from svg_loader import load_svg, Artwork
from gcode_gen import (PlotSettings, generate_layer_gcode, header_gcode,
                       footer_gcode, make_transform, estimate_time_minutes,
                       segment_stats)
from grbl_streamer import GrblConnection, StreamJob, list_serial_ports

QUALITY_LEVELS = {
    "Draft (0.5 mm)": 0.5,
    "Standard (0.2 mm)": 0.2,
    "Fine (0.05 mm)": 0.05,
    "Ultra fine (0.02 mm)": 0.02,
}

PAPER_SIZES = {
    "12 x 12 in (304.8 x 304.8)": (304.8, 304.8),
    "A3 landscape (420 x 297)": (420.0, 297.0),
    "A3 portrait (297 x 420)": (297.0, 420.0),
    "A4 landscape (297 x 210)": (297.0, 210.0),
    "A4 portrait (210 x 297)": (210.0, 297.0),
    "Custom": None,
}


MM_PER_INCH = 25.4


class SvgLoadThread(QThread):
    """Loads/flattens the SVG off the GUI thread so the app stays live."""
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, path, flatness_mm, parent=None):
        super().__init__(parent)
        self.path = path
        self.flatness_mm = flatness_mm

    def run(self):
        try:
            self.loaded.emit(load_svg(self.path, flatness_mm=self.flatness_mm))
        except Exception as exc:
            self.failed.emit(str(exc))


GRBL_ERROR_HINTS = {
    "error:5": "homing is not enabled in the firmware ($22=0)",
    "error:9": "G-code locked out during alarm state",
    "ALARM:1": "hard limit triggered",
    "ALARM:8": "homing failed to find the limit switch",
    "ALARM:9": "homing failed to clear the limit switch",
}


def explain_grbl(resp: str) -> str:
    hint = GRBL_ERROR_HINTS.get(resp)
    return f"{resp} ({hint})" if hint else resp


class ConnectSetup(QThread):
    """Runs after connecting: reads firmware settings, reports speed
    caps, homes the machine if the firmware supports it, and sets the
    work origin. Every failure is reported verbatim in the console."""
    log = Signal(str)

    def __init__(self, conn, draw_feed: float, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.draw_feed = draw_feed

    def run(self):
        settings = {}
        try:
            settings = self.conn.read_settings()
        except Exception as exc:
            self.log.emit(f"(could not read firmware settings: {exc})")

        max_x, max_y = settings.get(110), settings.get(111)
        if max_x is not None and max_y is not None:
            acc_x = settings.get(120, 0)
            acc_y = settings.get(121, 0)
            self.log.emit(f"Firmware limits: max rate X={max_x:g} "
                          f"Y={max_y:g} mm/min, accel X={acc_x:g} "
                          f"Y={acc_y:g} mm/s^2")
            cap = min(max_x, max_y)
            if cap < self.draw_feed:
                self.log.emit(f"NOTE: firmware caps feed at {cap:g} mm/min; "
                              f"your {self.draw_feed:g} draw feed will be "
                              "limited. Raise $110/$111 to change this.")

        homing = settings.get(22)
        try:
            self.conn.send_line_wait_ok("$X", timeout=5)
            homed = False
            if homing == 0:
                self.log.emit("Homing is disabled in this firmware "
                              "($22=0), so the machine cannot find Home "
                              "on its own. Using the current carriage "
                              "position as the origin. If your machine "
                              "has limit switches, enable homing by "
                              "sending $22=1.")
            else:
                self.log.emit("Homing ($H)... the carriage will move.")
                resp = self.conn.send_line_wait_ok("$H", timeout=120)
                if resp == "ok":
                    homed = True
                else:
                    self.log.emit(f"Homing failed: {explain_grbl(resp)}. "
                                  "Using the current position as origin.")
            self.conn.send_line_wait_ok("G92 X0 Y0", timeout=5)
            self.conn.send_line_wait_ok("G90", timeout=5)
            where = "Home" if homed else "current position"
            self.log.emit(f"Origin (X0 Y0) set at {where}.")
        except Exception as exc:
            self.log.emit(f"Connect setup failed: {exc}")


class CommandRunner(QThread):
    """Runs a short list of G-code lines without freezing the GUI."""
    log = Signal(str)
    done = Signal()

    def __init__(self, conn, lines, parent=None):
        super().__init__(parent)
        self.conn, self.lines = conn, lines

    def run(self):
        try:
            for line in self.lines:
                self.log.emit(f"> {line}")
                if line.startswith("$") and line not in ("$H",):
                    for out in self.conn.query(line, timeout=120):
                        self.log.emit(f"  {out}")
                    self.log.emit("  ok")
                else:
                    resp = self.conn.send_line_wait_ok(line, timeout=120)
                    self.log.emit(f"  {resp}")
                    if resp != "ok":
                        break
        except Exception as exc:
            self.log.emit(f"  !! {exc}")
        self.done.emit()


class PreviewView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PlotStudio -- iDraw / UUNA TEK GRBL plotter")
        self.resize(1280, 820)

        self.artwork: Artwork | None = None
        self.conn = GrblConnection()
        self.job: StreamJob | None = None
        self.cmd: CommandRunner | None = None
        self.loader: SvgLoadThread | None = None
        self.svg_path: str | None = None
        self._syncing = False
        self._unit_factor = 1.0        # display units -> mm

        self.scene = QGraphicsScene()
        self.view = PreviewView(self.scene)

        root = QWidget()
        layout = QHBoxLayout(root)
        # Side panel lives in a scroll area so it works on short screens.
        scroll = QScrollArea()
        scroll.setWidget(self._build_side_panel())
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(400 + scroll.verticalScrollBar().sizeHint().width()
                             + 4)
        layout.addWidget(scroll, 0)
        layout.addWidget(self.view, 1)
        self.setCentralWidget(root)
        self._refresh_ports()
        self.unit_combo.setCurrentIndex(1)   # default to inches
        self._load_prefs()
        self._update_preview()

    # ------------------------------------------------------------ UI build
    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(400)
        v = QVBoxLayout(panel)

        self.open_btn = QPushButton("Open SVG...")
        self.open_btn.clicked.connect(self.open_svg)
        v.addWidget(self.open_btn)

        # --- paper & size ---
        self.size_box = QGroupBox("Paper && artwork size (mm)")
        size_box = self.size_box
        form = QFormLayout(size_box)

        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["millimeters", "inches"])
        self.unit_combo.currentIndexChanged.connect(self._units_changed)
        form.addRow("Units", self.unit_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(QUALITY_LEVELS.keys())
        self.quality_combo.setCurrentText("Fine (0.05 mm)")
        self.quality_combo.setToolTip(
            "Maximum deviation of straight segments from the true curve.\n"
            "Finer = smoother curves, more G-code lines, slower plots.")
        self.quality_combo.currentTextChanged.connect(self._quality_changed)
        form.addRow("Curve quality", self.quality_combo)

        self.paper_combo = QComboBox()
        self.paper_combo.addItems(PAPER_SIZES.keys())
        self.paper_combo.currentTextChanged.connect(self._paper_changed)
        form.addRow("Paper", self.paper_combo)

        self.paper_w = self._spin(304.8, 0.1, 2000)
        self.paper_h = self._spin(304.8, 0.1, 2000)
        self.paper_w.valueChanged.connect(self._update_preview)
        self.paper_h.valueChanged.connect(self._update_preview)
        pw = QHBoxLayout()
        pw.addWidget(self.paper_w)
        pw.addWidget(QLabel("x"))
        pw.addWidget(self.paper_h)
        form.addRow("Paper W x H", pw)

        self.art_w = self._spin(304.8, 0.1, 2000)
        self.art_h = self._spin(304.8, 0.1, 2000)
        self.lock_aspect = QCheckBox("Lock aspect ratio")
        self.lock_aspect.setChecked(True)
        self.art_w.valueChanged.connect(lambda: self._size_edited("w"))
        self.art_h.valueChanged.connect(lambda: self._size_edited("h"))
        aw = QHBoxLayout()
        aw.addWidget(self.art_w)
        aw.addWidget(QLabel("x"))
        aw.addWidget(self.art_h)
        form.addRow("Artwork W x H", aw)
        form.addRow("", self.lock_aspect)

        # Scale relative to the SVG's own size (100 % = as drawn in the
        # file). Changing it resizes the artwork; press Center afterwards.
        self.scale_pct = QDoubleSpinBox()
        self.scale_pct.setRange(1, 1000)
        self.scale_pct.setDecimals(1)
        self.scale_pct.setSingleStep(5)
        self.scale_pct.setValue(100)
        self.scale_pct.setSuffix(" %")
        self.scale_pct.setKeyboardTracking(False)
        self.scale_pct.valueChanged.connect(self._scale_edited)
        form.addRow("Scale", self.scale_pct)

        self.off_x = self._spin(0, -500, 2000)
        self.off_y = self._spin(0, -500, 2000)
        self.off_x.valueChanged.connect(self._update_preview)
        self.off_y.valueChanged.connect(self._update_preview)
        ow = QHBoxLayout()
        ow.addWidget(self.off_x)
        ow.addWidget(QLabel(","))
        ow.addWidget(self.off_y)
        form.addRow("Offset X, Y", ow)

        self.margin = self._spin(10, 0, 500)
        self.margin.setToolTip("Blank border kept free on all four sides "
                               "by 'Fit to margins'.")
        form.addRow("Margin", self.margin)

        fit_row = QHBoxLayout()
        fit_btn = QPushButton("Fit to margins")
        fit_btn.clicked.connect(lambda: self.fit_to_paper())
        center_btn = QPushButton("Center")
        center_btn.clicked.connect(lambda: self.center_on_paper())
        fit_row.addWidget(fit_btn)
        fit_row.addWidget(center_btn)
        form.addRow(fit_row)

        # Machine-side orientation. These mirror the generated G-code so
        # the plot matches the preview; the preview itself never mirrors.
        self.mirror_x = QCheckBox("Mirror X")
        self.mirror_x.setChecked(False)
        self.mirror_y = QCheckBox("Mirror Y")
        self.mirror_y.setChecked(False)
        mrow = QHBoxLayout()
        self.mirror_x.toggled.connect(lambda _c: self._update_preview())
        self.mirror_y.toggled.connect(lambda _c: self._update_preview())
        mrow.addWidget(self.mirror_x)
        mrow.addWidget(self.mirror_y)
        form.addRow("Mirror artwork", mrow)
        v.addWidget(size_box)

        # --- pen & speed ---
        pen_box = QGroupBox("Pen && speeds")
        pform = QFormLayout(pen_box)
        # On the iDraw/DrawCore Z axis, Z0 is the pen RAISED and larger
        # Z values lower the pen toward the paper.
        self.pen_up = self._spin(0, 0, 10, step=0.5)
        self.pen_touch = self._spin(5, 0, 10, step=0.5)
        self.pen_down = self._spin(10, 0, 10, step=0.5)
        self.draw_feed = self._spin(10000, 100, 12000, step=100)
        self.z_feed = self._spin(2500, 100, 5000, step=100)
        pform.addRow("Pen-up Z (mm)", self.pen_up)
        pform.addRow("Pen-touch Z (mm)", self.pen_touch)
        pform.addRow("Pen-down Z (mm)", self.pen_down)
        pform.addRow("Draw feed (mm/min)", self.draw_feed)
        pform.addRow("Z feed (mm/min)", self.z_feed)
        self.opacity_z = QCheckBox("Opacity controls Z (touch -> down)")
        self.opacity_z.setChecked(True)
        self.opacity_z.setToolTip(
            "Opacity 0 lifts the pen, opacity 1 presses to Pen-down Z, "
            "in between maps linearly from Pen-touch Z.")
        self.opacity_z.toggled.connect(lambda _c: self._update_preview())
        pform.addRow(self.opacity_z)
        v.addWidget(pen_box)

        # --- layers ---
        layer_box = QGroupBox("Color layers (drag to reorder, tick to plot)")
        lv = QVBoxLayout(layer_box)
        self.layer_list = QListWidget()
        self.layer_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.layer_list.itemChanged.connect(self._update_preview)
        lv.addWidget(self.layer_list)
        v.addWidget(layer_box)

        # --- machine ---
        mach_box = QGroupBox("Machine")
        mv = QVBoxLayout(mach_box)
        port_row = QHBoxLayout()
        self.port_combo = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_ports)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connect)
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(refresh)
        port_row.addWidget(self.connect_btn)
        mv.addLayout(port_row)

        grid = QGridLayout()
        buttons = [
            ("Home ($H)", ["$H"]),
            ("Unlock ($X)", ["$X"]),
            ("Set origin here", ["G92 X0 Y0"]),
            ("Go to origin", ["G90", "G0 X0 Y0"]),
            ("Pen up", None), ("Pen down", None),
        ]
        for i, (label, lines) in enumerate(buttons):
            b = QPushButton(label)
            if label == "Pen up":
                b.clicked.connect(lambda: self.send_lines(
                    [f"G90", f"G1 Z{self.pen_up.value()} F{self.z_feed.value()}"]))
            elif label == "Pen down":
                b.clicked.connect(lambda: self.send_lines(
                    [f"G90", f"G1 Z{self.pen_down.value()} F{self.z_feed.value()}"]))
            else:
                b.clicked.connect(lambda _, l=lines: self.send_lines(l))
            grid.addWidget(b, i // 2, i % 2)
        mv.addLayout(grid)

        self.cmd_entry = QLineEdit()
        self.cmd_entry.setPlaceholderText(
            "Manual command, e.g. $$ or $120=800  (Enter to send)")
        self.cmd_entry.returnPressed.connect(self._send_manual)
        mv.addWidget(self.cmd_entry)
        v.addWidget(mach_box)

        # --- plot controls ---
        run_row = QHBoxLayout()
        self.plot_btn = QPushButton("Plot")
        self.plot_btn.clicked.connect(self.start_plot)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_plot)
        self.save_btn = QPushButton("Save G-code...")
        self.save_btn.clicked.connect(self.save_gcode)
        for b in (self.plot_btn, self.pause_btn, self.stop_btn, self.save_btn):
            run_row.addWidget(b)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        v.addLayout(run_row)

        self.progress = QProgressBar()
        v.addWidget(self.progress)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(2000)
        v.addWidget(self.console, 1)
        return panel

    @staticmethod
    def _spin(value, lo, hi, step=1.0) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setValue(value)
        s.setSingleStep(step)
        s.setDecimals(1)
        return s

    # ------------------------------------------------------------ helpers
    def log(self, text: str):
        self.console.appendPlainText(text)

    def _mm(self, display_value: float) -> float:
        return display_value * self._unit_factor

    def _units_changed(self, index: int):
        """Convert all size/position fields between mm and inches."""
        new_factor = MM_PER_INCH if index == 1 else 1.0
        if new_factor == self._unit_factor:
            return
        ratio = self._unit_factor / new_factor
        decimals = 2 if index == 1 else 1
        step = 0.1 if index == 1 else 1.0
        self._syncing = True
        for spin in (self.paper_w, self.paper_h, self.art_w,
                     self.art_h, self.off_x, self.off_y, self.margin):
            value = spin.value()          # read BEFORE decimals change
            spin.blockSignals(True)
            spin.setDecimals(3 if (spin is self.margin and index == 1)
                             else decimals)
            spin.setSingleStep(step)
            spin.setValue(value * ratio)
            spin.blockSignals(False)
        self._unit_factor = new_factor
        self._syncing = False
        unit = "inches" if index == 1 else "mm"
        self.size_box.setTitle(f"Paper && artwork size ({unit})")
        self._update_preview()

    # ---------------------------------------------------------- preferences
    # (label, widget attribute) pairs persisted between runs. Artwork size
    # and offsets are deliberately not saved: they belong to each SVG.
    _PREF_SPINS = ("pen_up", "pen_touch", "pen_down", "draw_feed", "z_feed")
    _PREF_CHECKS = ("lock_aspect", "mirror_x", "mirror_y", "opacity_z")

    def _load_prefs(self):
        st = QSettings()
        self._syncing = True
        try:
            # Units first: the paper fields are converted by this.
            self.unit_combo.setCurrentIndex(
                st.value("units", 1, type=int) % 2)
            q = st.value("quality", "", type=str)
            if q in QUALITY_LEVELS:
                self.quality_combo.blockSignals(True)
                self.quality_combo.setCurrentText(q)
                self.quality_combo.blockSignals(False)
            paper = st.value("paper", "", type=str)
            if paper:
                i = self.paper_combo.findText(paper)
                if i >= 0:
                    self.paper_combo.blockSignals(True)
                    self.paper_combo.setCurrentIndex(i)
                    self.paper_combo.blockSignals(False)
            pw = st.value("paper_w_mm", 0.0, type=float)
            ph = st.value("paper_h_mm", 0.0, type=float)
            if pw > 0 and ph > 0:
                self.paper_w.setValue(pw / self._unit_factor)
                self.paper_h.setValue(ph / self._unit_factor)
            m = st.value("margin_mm", -1.0, type=float)
            if m >= 0:
                self.margin.setValue(m / self._unit_factor)
            for name in self._PREF_SPINS:
                w = getattr(self, name)
                w.setValue(st.value(name, w.value(), type=float))
            for name in self._PREF_CHECKS:
                w = getattr(self, name)
                w.setChecked(st.value(name, w.isChecked(), type=bool))
            geom = st.value("geometry")
            if geom is not None:
                self.restoreGeometry(geom)
        finally:
            self._syncing = False

    def _save_prefs(self):
        st = QSettings()
        st.setValue("units", self.unit_combo.currentIndex())
        st.setValue("quality", self.quality_combo.currentText())
        st.setValue("paper", self.paper_combo.currentText())
        st.setValue("paper_w_mm", self.paper_w.value() * self._unit_factor)
        st.setValue("paper_h_mm", self.paper_h.value() * self._unit_factor)
        st.setValue("margin_mm", self.margin.value() * self._unit_factor)
        for name in self._PREF_SPINS:
            st.setValue(name, getattr(self, name).value())
        for name in self._PREF_CHECKS:
            st.setValue(name, getattr(self, name).isChecked())
        st.setValue("geometry", self.saveGeometry())

    def current_settings(self) -> PlotSettings:
        return PlotSettings(
            out_width_mm=self._mm(self.art_w.value()),
            out_height_mm=self._mm(self.art_h.value()),
            offset_x_mm=self._mm(self.off_x.value()),
            offset_y_mm=self._mm(self.off_y.value()),
            flip_x=self.mirror_x.isChecked(),
            flip_y=self.mirror_y.isChecked(),
            pen_up_z=self.pen_up.value(),
            pen_touch_z=self.pen_touch.value(),
            pressure_from_opacity=self.opacity_z.isChecked(),
            pen_down_z=self.pen_down.value(),
            draw_feed=self.draw_feed.value(),
            z_feed=self.z_feed.value(),
            simplify_mm=min(0.05, self._flatness_mm() / 4.0),
        )

    def _checked_layers(self):
        """Layers in list order, only those ticked."""
        result = []
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            if item.checkState() == Qt.Checked:
                result.append(item.data(Qt.UserRole))
        return result

    # ------------------------------------------------------------ SVG / size
    def open_svg(self):
        settings = QSettings()
        start_dir = settings.value("last_svg_dir", "", type=str)
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = os.path.dirname(self.svg_path) if self.svg_path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open SVG", start_dir,
                                              "SVG files (*.svg)")
        if not path:
            return
        settings.setValue("last_svg_dir", os.path.dirname(path))
        if self.loader and self.loader.isRunning():
            return
        self._start_load(path, keep_layout=False)

    def _flatness_mm(self) -> float:
        return QUALITY_LEVELS[self.quality_combo.currentText()]

    def _start_load(self, path: str, keep_layout: bool):
        self.open_btn.setEnabled(False)
        self.open_btn.setText("Loading...")
        self.log(f"Loading {path} (quality: "
                 f"{self.quality_combo.currentText()}) ...")
        self.loader = SvgLoadThread(path, self._flatness_mm())
        self.loader.loaded.connect(
            lambda art, p=path, k=keep_layout: self._svg_loaded(art, p, k))
        self.loader.failed.connect(self._svg_failed)
        self.loader.start()

    def _quality_changed(self, _text: str):
        if self.svg_path and not (self.loader and self.loader.isRunning()):
            self._start_load(self.svg_path, keep_layout=True)

    def _svg_failed(self, msg: str):
        self.open_btn.setEnabled(True)
        self.open_btn.setText("Open SVG...")
        QMessageBox.critical(self, "Could not load SVG", msg)

    def _svg_loaded(self, artwork, path: str, keep_layout: bool = False):
        self.open_btn.setEnabled(True)
        self.open_btn.setText("Open SVG...")
        self.artwork = artwork
        self.svg_path = path
        prev_checks = {}
        if keep_layout:
            for i in range(self.layer_list.count()):
                item = self.layer_list.item(i)
                prev_checks[item.data(Qt.UserRole).color] = item.checkState()
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for layer in self.artwork.layers:
            item = QListWidgetItem(
                f"{layer.color}   ({len(layer.polylines)} paths)")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable
                          | Qt.ItemIsDragEnabled)
            item.setCheckState(prev_checks.get(layer.color, Qt.Checked))
            pix = QPixmap(14, 14)
            pix.fill(QColor(layer.color))
            item.setIcon(QIcon(pix))
            item.setData(Qt.UserRole, layer)
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)
        self.log(f"Loaded {path}")
        self.log(f"  {len(self.artwork.layers)} colors, "
                 f"{sum(len(l.polylines) for l in self.artwork.layers)} paths")
        if keep_layout:
            self._update_preview()
        else:
            self.fit_to_paper()

    def _paper_changed(self, name: str):
        size = PAPER_SIZES.get(name)
        if size:
            self.paper_w.setValue(size[0] / self._unit_factor)
            self.paper_h.setValue(size[1] / self._unit_factor)
        self._update_preview()

    def _size_edited(self, which: str):
        if self._syncing or self.artwork is None:
            self._update_preview()
            return
        if self.lock_aspect.isChecked():
            aspect = self.artwork.width / max(self.artwork.height, 1e-9)
            self._syncing = True
            if which == "w":
                self.art_h.setValue(self.art_w.value() / aspect)
            else:
                self.art_w.setValue(self.art_h.value() * aspect)
            self._syncing = False
        self._sync_scale()
        self._update_preview()

    def _natural_size_mm(self):
        """Artwork size in mm as drawn in the SVG (SVG px = 1/96 in)."""
        k = MM_PER_INCH / 96.0
        return (max(self.artwork.width * k, 1e-9),
                max(self.artwork.height * k, 1e-9))

    def _sync_scale(self):
        """Show the current artwork width as a % of its natural size."""
        if self.artwork is None:
            return
        nat_w, _ = self._natural_size_mm()
        pct = self._mm(self.art_w.value()) / nat_w * 100.0
        self.scale_pct.blockSignals(True)
        self.scale_pct.setValue(min(max(pct, 1.0), 1000.0))
        self.scale_pct.blockSignals(False)

    def _scale_edited(self, pct: float):
        if self.artwork is None:
            return
        nat_w, nat_h = self._natural_size_mm()
        self._syncing = True
        self.art_w.setValue(nat_w * pct / 100.0 / self._unit_factor)
        self.art_h.setValue(nat_h * pct / 100.0 / self._unit_factor)
        self._syncing = False
        self._update_preview()

    def fit_to_paper(self):
        """Largest artwork that fits inside the paper minus the margins."""
        if self.artwork is None:
            return
        margin = self.margin.value()
        avail_w = max(self.paper_w.value() - 2 * margin, 0.1)
        avail_h = max(self.paper_h.value() - 2 * margin, 0.1)
        aspect = self.artwork.width / max(self.artwork.height, 1e-9)
        w = avail_w
        h = w / aspect
        if h > avail_h:
            h = avail_h
            w = h * aspect
        self._syncing = True
        self.art_w.setValue(w)
        self.art_h.setValue(h)
        self._syncing = False
        self._sync_scale()
        self.center_on_paper()

    def center_on_paper(self):
        self.off_x.setValue((self.paper_w.value() - self.art_w.value()) / 2)
        self.off_y.setValue((self.paper_h.value() - self.art_h.value()) / 2)
        self._update_preview()

    # ------------------------------------------------------------ preview
    def _update_preview(self):
        self.scene.clear()
        pw, ph = self._mm(self.paper_w.value()), self._mm(self.paper_h.value())
        self.scene.addRect(0, 0, pw, ph, QPen(QColor("#888888")),
                           QBrush(QColor("#ffffff")))
        origin_pen = QPen(QColor("#cc4444"))
        # Work origin (machine 0,0) is the paper's top-left corner; X grows
        # right and Y grows down, exactly like the generated G-code.
        self.scene.addLine(-8, 0, 8, 0, origin_pen)
        self.scene.addLine(0, -8, 0, 8, origin_pen)

        if self.artwork is not None:
            s = self.current_settings()
            # Use the very same transform as the G-code (offsets, mirror
            # flags) so the preview is exactly what the plotter draws.
            tf = make_transform(self.artwork, s)
            use_op = s.pressure_from_opacity
            levels = 10
            for layer in self._checked_layers():
                # One path per opacity bucket keeps the scene light even
                # when opacity varies along every stroke.
                buckets = [QPainterPath() for _ in range(levels + 1)]
                ops = layer.opacities or [1.0] * len(layer.polylines)
                for pl, op in zip(layer.polylines, ops):
                    pts = [tf(x, y) for x, y in pl]
                    if not use_op:
                        op = 1.0
                    if not isinstance(op, (list, tuple)):
                        if op >= s.min_opacity:
                            b = buckets[round(op * levels)]
                            b.moveTo(*pts[0])
                            for p in pts[1:]:
                                b.lineTo(*p)
                        continue
                    last = None
                    for i in range(len(pts) - 1):
                        o = 0.5 * (op[i] + op[i + 1])
                        if o < s.min_opacity:
                            last = None
                            continue
                        k = round(o * levels)
                        if last != k:
                            buckets[k].moveTo(*pts[i])
                            last = k
                        buckets[k].lineTo(*pts[i + 1])
                for k, bpath in enumerate(buckets):
                    if bpath.isEmpty():
                        continue
                    col = QColor(layer.color)
                    col.setAlphaF(max(k / levels, 0.1))
                    pen = QPen(col)
                    pen.setWidthF(0.4)
                    pen.setCapStyle(Qt.RoundCap)
                    self.scene.addPath(bpath, pen)
        self.scene.setSceneRect(-30, -30, pw + 60, ph + 60)
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    # ------------------------------------------------------------ machine
    def _refresh_ports(self):
        self.port_combo.clear()
        for dev, desc in list_serial_ports():
            self.port_combo.addItem(f"{dev}  ({desc})", dev)

    def toggle_connect(self):
        if self.conn.connected:
            self.conn.close()
            self.connect_btn.setText("Connect")
            self.log("Disconnected.")
            return
        dev = self.port_combo.currentData()
        if not dev:
            QMessageBox.warning(self, "No port", "No serial port selected.")
            return
        try:
            banner = self.conn.open(dev)
        except Exception as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
            return
        self.connect_btn.setText("Disconnect")
        self.log(f"Connected to {dev}")
        self.log(banner)
        # Read firmware settings (reporting speed caps), home the
        # machine if the firmware supports it, and set the work origin.
        self.setup = ConnectSetup(self.conn, self.draw_feed.value())
        self.setup.log.connect(self.log)
        self.setup.start()

    def send_lines(self, lines):
        if not self.conn.connected:
            self.log("Not connected.")
            return
        if (self.job and self.job.isRunning()) or \
           (self.cmd and self.cmd.isRunning()) or \
           (getattr(self, "setup", None) and self.setup.isRunning()):
            self.log("Busy -- wait for the current operation.")
            return
        self.cmd = CommandRunner(self.conn, lines)
        self.cmd.log.connect(self.log)
        self.cmd.start()

    def _send_manual(self):
        text = self.cmd_entry.text().strip()
        if text:
            self.cmd_entry.clear()
            self.send_lines([text])

    # ------------------------------------------------------------ plotting
    def start_plot(self):
        if self.artwork is None:
            QMessageBox.information(self, "No artwork", "Open an SVG first.")
            return
        if not self.conn.connected:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the plotter first.")
            return
        layers = self._checked_layers()
        if not layers:
            QMessageBox.information(self, "No layers", "Tick at least one layer.")
            return
        s = self.current_settings()
        jobs = [(l.color, generate_layer_gcode(l, self.artwork, s))
                for l in layers]
        est = sum(estimate_time_minutes(l, self.artwork, s) for l in layers)
        n_seg, avg_len = segment_stats([ln for _, g in jobs for ln in g])
        self.log(f"Starting plot: {len(jobs)} layer(s), {n_seg} segments, "
                 f"avg {avg_len:.2f} mm, ~{est:.0f} min of drawing.")
        if avg_len and avg_len < 1.0:
            self.log("(short segments limit top speed -- GRBL only reaches "
                     "what it can brake from within its lookahead buffer)")

        self.job = StreamJob(self.conn, jobs, header_gcode(s),
                             footer_gcode(s), pen_up_z=s.pen_up_z,
                             z_feed=s.z_feed)
        self.job.progress.connect(self._on_progress)
        self.job.console.connect(self.log)
        self.job.need_pen_change.connect(self._on_pen_change)
        self.job.layer_started.connect(
            lambda i, c: self.log(f">> plotting layer {i + 1}: {c}"))
        self.job.finished_ok.connect(self._on_finished)
        self.job.failed.connect(self._on_failed)
        self.plot_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.job.start()

    def _on_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _on_pen_change(self, idx, color):
        box = QMessageBox(self)
        box.setWindowTitle("Pen change")
        box.setText(f"Layer {idx + 1}: fit the pen for color  {color}\n\n"
                    "Click OK when ready to continue.")
        pix = QPixmap(28, 28)
        pix.fill(QColor(color))
        box.setIconPixmap(pix)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()
        if self.job:
            self.job.confirm_pen_changed()

    def toggle_pause(self):
        if not self.job:
            return
        if self.pause_btn.text() == "Pause":
            self.job.pause()
            self.pause_btn.setText("Resume")
        else:
            self.job.resume()
            self.pause_btn.setText("Pause")

    def stop_plot(self):
        if self.job:
            self.job.abort()

    def _plot_done(self):
        self.plot_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
        self.stop_btn.setEnabled(False)

    def _on_finished(self):
        self.log(">> plot complete.")
        self._play_done_sound()
        self._plot_done()

    @staticmethod
    def _play_done_sound():
        """Three ascending beeps, played off the GUI thread."""
        def beeper():
            try:
                import winsound
                for freq in (660, 880, 1100):
                    winsound.Beep(freq, 180)
            except Exception:
                QApplication.beep()
        threading.Thread(target=beeper, daemon=True).start()

    def _on_failed(self, msg):
        self.log(f">> {msg}")
        self._plot_done()

    # ------------------------------------------------------------ export
    def save_gcode(self):
        if self.artwork is None:
            QMessageBox.information(self, "No artwork", "Open an SVG first.")
            return
        layers = self._checked_layers()
        if not layers:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save G-code", "plot.gcode",
                                              "G-code (*.gcode *.nc)")
        if not path:
            return
        s = self.current_settings()
        lines = [f"; PlotStudio export", *header_gcode(s)]
        for i, layer in enumerate(layers):
            lines.append(f"; ---- layer {i + 1}: {layer.color} ----")
            if i > 0:
                lines.append("G0 X0 Y0")
                lines.append("M0 ; pen change -- press cycle start to resume")
            lines.extend(generate_layer_gcode(layer, self.artwork, s))
        lines.extend(footer_gcode(s))
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        self.log(f"Saved {len(lines)} lines to {path}")

    def closeEvent(self, event):
        self._save_prefs()
        if self.job and self.job.isRunning():
            self.job.abort()
            self.job.wait(3000)
        self.conn.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("PlotStudio")
    app.setApplicationName("PlotStudio")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
