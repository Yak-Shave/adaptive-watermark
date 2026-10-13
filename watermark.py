#!/usr/bin/env python3
"""
Name: Adaptive Watermark
Category: Images
Description: Stamps a logo onto a batch of images and picks its own color and
    glow so a thin logo stays visible on any background - light, dark, or
    busy. Choose a corner and a size in the preview window; nothing on disk
    changes until you hit Apply.
Author: Yak Shave
Version: 1.0
Tags: images, watermark, branding, batch
Requires: pillow; PySide6 (only for the preview window)

HOW THE ADAPTIVE PART WORKS
A thin, script-style logo is easy to lose against a busy or mid-toned
background, and a hard outline makes a thin line look like a blob. So instead
of one fixed color, every image is measured individually, only where the logo
will actually sit:

  1. Read the brightness under the logo's own shape (the shape is used as a
     sampling mask, slightly softened so the area right around each stroke
     counts too).
  2. Pick the stamp color from that: light on a dark background, dark on a
     light one.
  3. Work out how much contrast that color actually gets against the
     surroundings - and where it's marginal, add a soft glow in the opposite
     color. A mid-toned background (neither clearly light nor dark) is the
     hard case: a thin white line can vanish into it even though nothing
     scored as "dark". A soft dark glow behind the line fixes that.
  4. A busy background (lots of local contrast) gets a bit more glow too,
     since there's no single contrast value that holds across the whole area.

The stroke itself is never outlined or thickened - only the glow behind it
changes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QMessageBox,
        QPushButton,
        QSlider,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )
    QT_AVAILABLE = True
except ImportError:
    # Batch mode runs without a window, so a missing PySide6 is only fatal
    # when the preview is actually asked for.
    QT_AVAILABLE = False

    class QWidget:  # placeholder so the window class below can still be defined
        pass

# ── Where this tool keeps its own files ─────────────────────────────────
# Every Yak Shave tool writes its configs, caches and backups into one shared
# folder instead of dropping loose files next to the scripts. A folder of
# sixty scripts stays a folder of sixty scripts.
DATA_DIR_NAME = "yakshave_data"


def data_dir() -> Path:
    """The folder this tool writes into, created on first use.

    `yakshave_data` next to this script by default; override it with the
    YAKSHAVE_DATA_DIR environment variable; falls back to ~/.yakshave when
    the script's own folder is read-only.
    """
    candidates = []
    override = os.environ.get("YAKSHAVE_DATA_DIR")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path(__file__).resolve().parent / DATA_DIR_NAME)
    candidates.append(Path.home() / ".yakshave")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return Path(__file__).resolve().parent


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
LOGO_NAME_PATTERN = re.compile(r"^logo\.(png|webp|jpg|jpeg|bmp)$", re.IGNORECASE)

LIGHT = (255, 255, 255)
DARK = (16, 12, 20)
BRIGHTNESS_SPLIT = 140.0  # above this, the background counts as "light"

BACKUP_DIRNAME = "_watermark_originals"
SOURCE_MARKER_FILENAME = "__source.txt"

STROKE_RATIO = 0.008        # stroke thickness as a fraction of the stamp width
GLOW_RADIUS_STROKES = 4.0   # blur radius, in stroke widths - wide and soft, not a hard outline
GLOW_OFFSET_STROKES = 0.7   # down-right offset, in stroke widths - reads as a shadow, not a halo
GLOW_OPACITY_RANGE = (0.14, 0.42)  # peak glow opacity is capped, never solid

ANCHOR_LABELS = {
    "top-left": "Top left",
    "top-center": "Top center",
    "top-right": "Top right",
    "center-left": "Center left",
    "center": "Center",
    "center-right": "Center right",
    "bottom-left": "Bottom left",
    "bottom-center": "Bottom center",
    "bottom-right": "Bottom right",
}
DEFAULT_ANCHOR = "bottom-left"

DEFAULT_SIZE_PERCENT = 12
DEFAULT_MARGIN_PERCENT = 2
DEFAULT_OPACITY_PERCENT = 85


# ── Pure logic (no Qt) - kept separate so it can be unit tested on its own ──

def find_logo(search_dir: Path, override: Path | None = None) -> Path | None:
    """Look for a logo image to stamp onto pictures.

    Checks an explicit override first, then any file named "logo.<ext>"
    (case-insensitive) directly inside `search_dir` - by default the folder
    this script lives in. Drop a logo next to the script and it's picked up
    automatically; nothing outside that folder is ever searched.
    """
    if override is not None:
        return override if override.is_file() else None
    try:
        for entry in sorted(search_dir.iterdir()):
            if entry.is_file() and LOGO_NAME_PATTERN.match(entry.name):
                return entry
    except OSError:
        pass
    return None


def trim_transparent_padding(logo: Image.Image) -> Image.Image:
    """Crop a logo down to its visible pixels.

    getbbox() alone isn't enough on a logo that was cut out by hand: there
    are usually a few near-transparent pixels left around the edge from the
    cutout process, and getbbox() treats anything above alpha 0 as content -
    so the box comes back as the whole file and nothing gets trimmed.
    Thresholding first (below 8/255 counts as empty) fixes that.

    Without this step every size and margin setting below would be measured
    against the empty space around the logo instead of the logo itself.
    """
    try:
        alpha = logo.split()[3]
        box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
    except (IndexError, OSError):
        box = logo.getbbox()
    return logo.crop(box) if box else logo


def measure_background(
    image: Image.Image, box: tuple[int, int, int, int], mask: Image.Image
) -> tuple[float, float]:
    """Average brightness and spread (both 0-255) under the stamp's shape.

    Sampling is restricted to where the logo actually touches - the shape
    itself is used as a mask, softened slightly so the strip right around
    each stroke counts too. Without that restriction, a large gap between
    letters would dominate the measurement over the strokes themselves.

    Runs through ImageStat (native code) rather than pixel-by-pixel Python -
    on a large batch, a manual pass adds up fast per image.
    """
    x, y, w, h = box
    w, h = max(1, int(w)), max(1, int(h))
    region = image.convert("L").crop((int(x), int(y), int(x) + w, int(y) + h))
    if mask.size != region.size:
        mask = mask.resize(region.size, Image.BILINEAR)
    soft_mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, w * 0.008)))
    soft_mask = soft_mask.point(lambda v: 255 if v > 8 else 0)
    try:
        stats = ImageStat.Stat(region, soft_mask)
        if stats.count[0] < 1:
            raise ValueError("empty mask")
        return float(stats.mean[0]), float(stats.stddev[0])
    except Exception:
        stats = ImageStat.Stat(region)
        return float(stats.mean[0]), float(stats.stddev[0])


def resolve_watermark_color(choice: str | None) -> tuple[int, int, int] | None:
    """"auto" / "light" / "dark" / "0".."255" -> None or an (r, g, b) triple.

    None means "measure the background and decide automatically". Anything
    else is a color chosen by hand, overriding the measurement.
    """
    if choice is None:
        return None
    text = str(choice).strip().lower()
    if not text or text == "auto":
        return None
    if text in ("light", "white"):
        return LIGHT
    if text in ("dark", "black"):
        return DARK
    try:
        value = max(0, min(255, int(round(float(text)))))
        return (value, value, value)
    except ValueError:
        return None


def derive_appearance(
    mean_brightness: float,
    stddev: float,
    base_opacity: float,
    color_choice: str | None = None,
) -> tuple[tuple[int, int, int], tuple[int, int, int], float, float]:
    """Work out stamp color, glow color, glow strength and final opacity.

    `color_choice` overrides the measured color; the glow then follows the
    chosen color instead of the measured one - light stamp, dark glow, and
    vice versa - otherwise stamp and glow could end up equally bright and
    the glow would do nothing.
    """
    background_is_light = mean_brightness > BRIGHTNESS_SPLIT
    color = DARK if background_is_light else LIGHT
    glow_color = LIGHT if background_is_light else DARK

    chosen = resolve_watermark_color(color_choice)
    if chosen is not None:
        color = chosen
        glow_color = DARK if sum(chosen) / 3.0 > 128 else LIGHT

    # How confidently does the chosen color stand out? What matters is the
    # distance from mid-gray, not the raw brightness gap - a saturated
    # mid-toned background (e.g. a strong magenta) measures near the middle
    # even though a thin white line still gets lost in it. Near the middle,
    # give the glow full strength; near either end, almost none.
    certainty = min(1.0, abs(mean_brightness - 128.0) / 128.0)
    busyness = min(1.0, stddev / 55.0)
    glow_min, glow_max = GLOW_OPACITY_RANGE
    glow_strength = glow_min + (glow_max - glow_min) * max(1.0 - certainty, busyness * 0.8)
    opacity = min(1.0, base_opacity * (1.0 + 0.12 * (1.0 - certainty)))
    return color, glow_color, glow_strength, opacity


def colorize_mask(
    mask: Image.Image, color: tuple[int, int, int], opacity: float, canvas_size: tuple[int, int]
) -> Image.Image:
    alpha = mask if opacity >= 1.0 else mask.point(lambda v: int(v * opacity))
    layer = Image.new("RGBA", canvas_size, tuple(color) + (255,))
    layer.putalpha(alpha)
    return layer


def _anchor_parts(anchor: str) -> tuple[str, str]:
    if anchor == "center":
        return "center", "center"
    vertical, _, horizontal = anchor.partition("-")
    return vertical, horizontal


def compute_anchor_position(
    image_size: tuple[int, int],
    stamp_size: tuple[int, int],
    anchor: str,
    margin: int,
) -> tuple[int, int]:
    """Top-left pixel coordinate for the stamp, for a given anchor.

    Margin is applied symmetrically on whichever edges the anchor touches -
    a "top-left" stamp gets the same margin from the top as from the left;
    a centered stamp gets none, since there's no edge to measure from.
    """
    img_w, img_h = image_size
    stamp_w, stamp_h = stamp_size
    vertical, horizontal = _anchor_parts(anchor)

    x = {
        "left": margin,
        "center": (img_w - stamp_w) // 2,
        "right": img_w - stamp_w - margin,
    }[horizontal]
    y = {
        "top": margin,
        "center": (img_h - stamp_h) // 2,
        "bottom": img_h - stamp_h - margin,
    }[vertical]
    return x, y


def apply_watermark(
    image: Image.Image,
    logo: Image.Image,
    *,
    anchor: str = DEFAULT_ANCHOR,
    size_ratio: float = DEFAULT_SIZE_PERCENT / 100,
    margin_ratio: float = DEFAULT_MARGIN_PERCENT / 100,
    opacity: float = DEFAULT_OPACITY_PERCENT / 100,
    color_choice: str | None = None,
) -> Image.Image:
    """Stamp `logo` onto a copy of `image` and return the result.

    `size_ratio` and `margin_ratio` are both fractions of the image width,
    so the logo keeps the same relative size and placement across images of
    different resolutions.
    """
    logo = trim_transparent_padding(logo)
    img_w, img_h = image.size
    stamp_w = max(8, int(img_w * size_ratio))
    stamp_h = max(1, int(logo.height * (stamp_w / logo.width)))
    stamp = logo.resize((stamp_w, stamp_h), Image.LANCZOS)
    alpha = stamp.split()[3]

    margin = max(1, int(img_w * margin_ratio))
    x, y = compute_anchor_position((img_w, img_h), (stamp_w, stamp_h), anchor, margin)

    brightness, spread = measure_background(image, (x, y, stamp_w, stamp_h), alpha)
    color, glow_color, glow_strength, resolved_opacity = derive_appearance(
        brightness, spread, opacity, color_choice
    )

    # The glow: same shape as the stamp, blurred wide, offset slightly, and
    # capped at the top end - it sits BEHIND the stamp, so the stroke on top
    # stays exactly as thin and sharp as the logo itself.
    stroke_width = max(1.0, stamp_w * STROKE_RATIO)
    blur_radius = stroke_width * GLOW_RADIUS_STROKES
    offset = int(round(stroke_width * GLOW_OFFSET_STROKES))
    pad = int(blur_radius * 3) + offset + 2
    canvas_size = (stamp_w + pad * 2, stamp_h + pad * 2)

    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))

    glow_mask = Image.new("L", canvas_size, 0)
    glow_mask.paste(alpha, (pad + offset, pad + offset))
    glow_mask = glow_mask.filter(ImageFilter.GaussianBlur(blur_radius))
    # Scale to the target peak instead of multiplying blindly - blind
    # multiplication pushes the blurred area into saturation, and a
    # saturated area with a soft edge reads as a hard outline again.
    peak = glow_mask.getextrema()[1] or 1
    glow_mask = glow_mask.point(lambda v: int(v * 255 * glow_strength / peak))
    layer.paste(colorize_mask(glow_mask, glow_color, 1.0, canvas_size), (0, 0), glow_mask)

    stamp_mask = Image.new("L", canvas_size, 0)
    stamp_mask.paste(alpha, (pad, pad))
    layer.alpha_composite(colorize_mask(stamp_mask, color, resolved_opacity, canvas_size))

    result = image.convert("RGBA")
    result.alpha_composite(layer, (x - pad, y - pad))
    return result.convert("RGB")


# ── Folders, backup, restore ────────────────────────────────────────────

def collect_images(folder: Path, recursive: bool = False) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    try:
        return sorted(
            p for p in folder.glob(pattern)
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        )
    except OSError:
        return []


def folder_signature(folder: Path) -> str:
    """A short, stable name for this folder's backup subfolder - based on the
    resolved path, so two same-named folders never collide. The plain folder
    name is kept in the string too, purely so the backup directory stays
    readable if you ever need to look through it by hand."""
    digest = hashlib.blake2b(str(folder.resolve()).lower().encode("utf-8"), digest_size=5).hexdigest()
    clean_name = re.sub(r"[^\w\- ]", "_", folder.name)[:40].strip() or "folder"
    return f"{clean_name}__{digest}"


def backup_root() -> Path:
    """Originals live in the shared data folder, one subfolder per source."""
    return data_dir() / BACKUP_DIRNAME


def backup_originals(images: list[Path], folder: Path, log=None) -> int:
    """Copy originals aside before the watermark is drawn.

    Existing backups are never overwritten - running this twice on the same
    folder must not end up backing up the already-watermarked version and
    losing the real original for good.
    """
    destination = backup_root() / folder_signature(folder)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        marker = destination / SOURCE_MARKER_FILENAME
        if not marker.exists():
            marker.write_text(str(folder.resolve()), encoding="utf-8")
    except OSError as exc:
        if log:
            log(f"  Could not create backup folder: {exc}")
        return 0

    count = 0
    for image_path in images:
        # Keep the folder structure: with subfolders included, two files can
        # share a name, and a flat backup would restore them to the wrong place.
        try:
            relative = image_path.resolve().relative_to(folder.resolve())
        except ValueError:
            relative = Path(image_path.name)
        target = destination / relative
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(image_path), str(target))
            count += 1
        except OSError as exc:
            if log:
                log(f"  {image_path.name}: {exc}")
    return count


def find_backup_for(folder: Path) -> Path | None:
    """Where is this folder's backup, if any? Matched first by the stable
    signature; if the folder has since been moved, falls back to reading the
    source marker each backup folder carries."""
    direct = backup_root() / folder_signature(folder)
    if direct.is_dir():
        return direct
    target_name = folder.name.lower()
    try:
        for candidate in backup_root().iterdir():
            if not candidate.is_dir():
                continue
            marker = candidate / SOURCE_MARKER_FILENAME
            if marker.is_file():
                try:
                    if Path(marker.read_text(encoding="utf-8").strip()).name.lower() == target_name:
                        return candidate
                except OSError:
                    pass
    except OSError:
        pass
    return None


def _note_history(description: str, pairs: list[tuple[str, str]]) -> None:
    """Optional integration point: if a Yak Shave file-history/undo tool is
    installed alongside this one, log the restore there too. Purely
    optional - this is a silent no-op if that module isn't present.
    """
    try:
        import yakshave_history  # placeholder name - point at the real module once it exists
        yakshave_history.log_entry(description, pairs)
    except ImportError:
        pass


def restore_originals(folder: Path, log=None) -> tuple[int, str]:
    """Undo the watermark by copying the backed-up originals back - no
    pixels are recomputed, the saved file just takes its place again."""
    backup = find_backup_for(folder)
    if backup is None:
        return 0, "No backup found for this folder."
    restored, errors, pairs = 0, [], []
    for image_path in collect_images(backup, recursive=True):
        try:
            relative = image_path.resolve().relative_to(backup.resolve())
        except ValueError:
            relative = Path(image_path.name)
        target = folder / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(image_path), str(target))
            restored += 1
            pairs.append((str(image_path), str(target)))
            if log:
                log(f"  restored: {relative}")
        except OSError as exc:
            errors.append(f"{relative}: {exc}")
    if pairs:
        _note_history(f"Watermark removed: {folder.name}", pairs)
    message = f"{restored} image(s) restored from backup."
    if errors:
        message += f" {len(errors)} failed:\n  " + "\n  ".join(errors[:5])
    return restored, message


# ── Batch mode (no window) ───────────────────────────────────────────────

def run_batch(
    folder: Path,
    *,
    logo_path: Path | None = None,
    anchor: str = DEFAULT_ANCHOR,
    size_percent: float = DEFAULT_SIZE_PERCENT,
    margin_percent: float = DEFAULT_MARGIN_PERCENT,
    opacity_percent: float = DEFAULT_OPACITY_PERCENT,
    color: str | None = None,
    recursive: bool = False,
    dry_run: bool = False,
) -> int:
    """Watermark every image in `folder` without opening the preview.

    Same code path as the window's Apply button, including the backup step -
    so `--restore` puts these images back just as well.
    """
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2

    logo_file = logo_path or find_logo(Path(__file__).resolve().parent)
    if logo_file is None:
        print(
            'No logo found. Put a "logo.png" next to this script, or pass '
            "--logo PATH.",
            file=sys.stderr,
        )
        return 3

    images = collect_images(folder, recursive)
    if not images:
        print("No images found.", flush=True)
        return 0

    print(f"Folder: {folder}", flush=True)
    print(f"Logo:   {logo_file}", flush=True)
    print(f"Images: {len(images)}", flush=True)
    print(
        f"Anchor: {anchor}   Size: {size_percent}%   Margin: {margin_percent}%   "
        f"Opacity: {opacity_percent}%   Color: {color or 'auto'}",
        flush=True,
    )

    if dry_run:
        print("\nDry run - nothing is written.", flush=True)
        for image_path in images:
            print(f"  Would watermark: {image_path}", flush=True)
        return 0

    backed_up = backup_originals(images, folder, log=lambda line: print(line, flush=True))
    print(f"{backed_up} original(s) backed up to {backup_root()}", flush=True)

    settings = dict(
        anchor=anchor,
        size_ratio=size_percent / 100,
        margin_ratio=margin_percent / 100,
        opacity=opacity_percent / 100,
        color_choice=color,
    )

    failures = 0
    with Image.open(str(logo_file)) as logo_source:
        logo = logo_source.convert("RGBA")
        for index, image_path in enumerate(images, start=1):
            print(f"[{index}/{len(images)}] {image_path.name}", flush=True)
            try:
                with Image.open(str(image_path)) as source:
                    result = apply_watermark(source.convert("RGB"), logo, **settings)
                if image_path.suffix.lower() in (".jpg", ".jpeg"):
                    result.save(str(image_path), quality=95)
                else:
                    result.save(str(image_path))
            except Exception as exc:
                failures += 1
                print(f"  failed: {exc}", file=sys.stderr)

    print(f"\nDone. {len(images) - failures} of {len(images)} image(s) watermarked.", flush=True)
    if failures:
        print(f"{failures} failed - see the messages above.", flush=True)
        print("Run --restore to put the originals back.", flush=True)
    return 1 if failures else 0


def run_restore(folder: Path) -> int:
    """Copy the backed-up originals back over the current images."""
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2
    restored, message = restore_originals(folder, log=lambda line: print(line, flush=True))
    print(message, flush=True)
    return 0 if restored else 1


# ── GUI ──────────────────────────────────────────────────────────────────

class WatermarkWindow(QWidget):
    def __init__(self, start_folder: Path | None = None, recursive: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("Yak Shave - Adaptive Watermark")
        self.resize(940, 640)

        self.script_dir = Path(__file__).resolve().parent
        self._folder: Path | None = None
        self._logo_override: Path | None = None
        self._sample_path: Path | None = None

        self._build_ui()
        self._connect_signals()

        self.recursive_check.setChecked(recursive)
        self._refresh_logo_status()
        if start_folder and start_folder.is_dir():
            self._set_folder(start_folder)

    # -- UI construction --------------------------------------------------

    def _add_slider(self, layout: QVBoxLayout, title: str, minimum: int, maximum: int,
                     default: int, unit: str) -> tuple[QSlider, QLabel]:
        row = QHBoxLayout()
        row.addWidget(QLabel(title))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(default)
        value_label = QLabel(f"{default}{unit}")
        value_label.setMinimumWidth(40)
        row.addWidget(slider, stretch=1)
        row.addWidget(value_label)
        layout.addLayout(row)
        return slider, value_label

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        folder_row = QHBoxLayout()
        self.folder_label = QLabel("No folder selected.")
        self.folder_label.setStyleSheet("color: #666;")
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._pick_folder)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._refresh_preview)
        folder_row.addWidget(QLabel("Folder:"))
        folder_row.addWidget(self.folder_label, stretch=1)
        folder_row.addWidget(refresh_button)
        folder_row.addWidget(browse_button)
        layout.addLayout(folder_row)

        self.recursive_check = QCheckBox("Include subfolders")
        layout.addWidget(self.recursive_check)

        logo_row = QHBoxLayout()
        self.logo_label = QLabel()
        choose_logo_button = QPushButton("Choose logo…")
        choose_logo_button.clicked.connect(self._pick_logo)
        logo_row.addWidget(QLabel("Logo:"))
        logo_row.addWidget(self.logo_label, stretch=1)
        logo_row.addWidget(choose_logo_button)
        layout.addLayout(logo_row)

        main_row = QHBoxLayout()

        controls_box = QGroupBox("Placement")
        controls_layout = QVBoxLayout(controls_box)

        controls_layout.addWidget(QLabel("Position"))
        self.position_combo = QComboBox()
        for key, label in ANCHOR_LABELS.items():
            self.position_combo.addItem(label, userData=key)
        self.position_combo.setCurrentIndex(list(ANCHOR_LABELS).index(DEFAULT_ANCHOR))
        controls_layout.addWidget(self.position_combo)

        self.size_slider, self.size_value_label = self._add_slider(
            controls_layout, "Size", 2, 40, DEFAULT_SIZE_PERCENT, "%"
        )
        self.margin_slider, self.margin_value_label = self._add_slider(
            controls_layout, "Margin", 0, 10, DEFAULT_MARGIN_PERCENT, "%"
        )
        self.opacity_slider, self.opacity_value_label = self._add_slider(
            controls_layout, "Opacity", 10, 100, DEFAULT_OPACITY_PERCENT, "%"
        )

        controls_layout.addWidget(QLabel("Color"))
        self.color_combo = QComboBox()
        self.color_combo.addItem("Automatic (matches background)", userData="auto")
        self.color_combo.addItem("Light", userData="light")
        self.color_combo.addItem("Dark", userData="dark")
        self.color_combo.addItem("Custom gray", userData="custom")
        controls_layout.addWidget(self.color_combo)
        self.custom_gray_spin = QSpinBox()
        self.custom_gray_spin.setRange(0, 255)
        self.custom_gray_spin.setValue(255)
        self.custom_gray_spin.setPrefix("gray value: ")
        self.custom_gray_spin.setVisible(False)
        controls_layout.addWidget(self.custom_gray_spin)

        controls_layout.addStretch(1)
        main_row.addWidget(controls_box)

        preview_box = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_box)
        self.preview_label = QLabel("Pick a folder with images to see a preview.")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(420, 420)
        self.preview_label.setStyleSheet("background: #222; color: #999; border: 1px solid #444;")
        preview_layout.addWidget(self.preview_label, stretch=1)
        main_row.addWidget(preview_box, stretch=1)

        layout.addLayout(main_row, stretch=1)

        bottom_row = QHBoxLayout()
        self.status_label = QLabel("Pick a folder to get started.")
        self.restore_button = QPushButton("Restore originals")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._restore)
        self.apply_button = QPushButton("Apply to all images")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        bottom_row.addWidget(self.status_label, stretch=1)
        bottom_row.addWidget(self.restore_button)
        bottom_row.addWidget(self.apply_button)
        layout.addLayout(bottom_row)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._refresh_preview)

    def _connect_signals(self) -> None:
        self.recursive_check.stateChanged.connect(self._refresh_preview)
        self.position_combo.currentIndexChanged.connect(self._schedule_preview)
        self.color_combo.currentIndexChanged.connect(self._on_color_mode_changed)
        self.custom_gray_spin.valueChanged.connect(self._schedule_preview)
        self.size_slider.valueChanged.connect(
            lambda v: self._on_slider_changed(self.size_value_label, v, "%"))
        self.margin_slider.valueChanged.connect(
            lambda v: self._on_slider_changed(self.margin_value_label, v, "%"))
        self.opacity_slider.valueChanged.connect(
            lambda v: self._on_slider_changed(self.opacity_value_label, v, "%"))

    def _on_slider_changed(self, label: QLabel, value: int, unit: str) -> None:
        label.setText(f"{value}{unit}")
        self._schedule_preview()

    def _on_color_mode_changed(self) -> None:
        self.custom_gray_spin.setVisible(self.color_combo.currentData() == "custom")
        self._schedule_preview()

    # -- Folder / logo handling ---------------------------------------------

    def _pick_folder(self) -> None:
        start = str(self._folder) if self._folder else ""
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if chosen:
            self._set_folder(Path(chosen))

    def _set_folder(self, folder: Path) -> None:
        self._folder = folder
        self.folder_label.setText(str(folder))
        self.folder_label.setStyleSheet("color: black;")
        self.restore_button.setEnabled(find_backup_for(folder) is not None)
        self._refresh_preview()

    def _pick_logo(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Select logo", str(self.script_dir), "Images (*.png *.webp *.jpg *.jpeg *.bmp)"
        )
        if chosen:
            self._logo_override = Path(chosen)
            self._refresh_logo_status()
            self._refresh_preview()

    def _current_logo_path(self) -> Path | None:
        return find_logo(self.script_dir, override=self._logo_override)

    def _refresh_logo_status(self) -> None:
        logo_path = self._current_logo_path()
        if logo_path:
            self.logo_label.setText(str(logo_path))
            self.logo_label.setStyleSheet("color: black;")
        else:
            self.logo_label.setText('No logo found - place a "logo.png" next to this script, or choose one.')
            self.logo_label.setStyleSheet("color: #a33;")

    # -- Preview --------------------------------------------------------

    def _current_color_choice(self) -> str | None:
        mode = self.color_combo.currentData()
        if mode == "auto":
            return None
        if mode == "custom":
            return str(self.custom_gray_spin.value())
        return mode

    def _current_settings(self) -> dict:
        return dict(
            anchor=self.position_combo.currentData(),
            size_ratio=self.size_slider.value() / 100,
            margin_ratio=self.margin_slider.value() / 100,
            opacity=self.opacity_slider.value() / 100,
            color_choice=self._current_color_choice(),
        )

    def _schedule_preview(self, *_args: object) -> None:
        self._preview_timer.start(200)

    def _refresh_preview(self) -> None:
        self._refresh_logo_status()
        self.apply_button.setEnabled(False)

        if self._folder is None:
            return

        images = collect_images(self._folder, self.recursive_check.isChecked())
        if not images:
            self.preview_label.setText("No images found in this folder.")
            self.status_label.setText("No images found.")
            return

        logo_path = self._current_logo_path()
        if logo_path is None:
            self.preview_label.setText("No logo found.")
            self.status_label.setText("Add a logo.png next to this script, or choose one, to continue.")
            return

        self._sample_path = images[0]
        try:
            with Image.open(str(logo_path)) as logo_file:
                logo = logo_file.convert("RGBA")
                warning = ""
                if logo.split()[3].getextrema()[0] == 255:
                    warning = "Note: this logo has no transparent background - it will be a solid rectangle. "
                with Image.open(str(self._sample_path)) as sample:
                    preview = apply_watermark(sample.convert("RGB"), logo, **self._current_settings())
        except Exception as exc:
            self.preview_label.setText(f"Could not render preview:\n{exc}")
            return

        self._show_preview_image(preview)
        self.status_label.setText(f"{warning}{len(images)} image(s) in folder. Preview: {self._sample_path.name}")
        self.apply_button.setEnabled(True)

    def _show_preview_image(self, image: Image.Image) -> None:
        buffer = io.BytesIO()
        preview = image.copy()
        preview.thumbnail((600, 600), Image.LANCZOS)
        preview.save(buffer, format="PNG")
        pixmap = QPixmap()
        pixmap.loadFromData(buffer.getvalue())
        self.preview_label.setPixmap(pixmap)

    # -- Actions --------------------------------------------------------

    def _apply(self) -> None:
        if self._folder is None:
            return
        images = collect_images(self._folder, self.recursive_check.isChecked())
        if not images:
            return
        logo_path = self._current_logo_path()
        if logo_path is None:
            return

        self.apply_button.setEnabled(False)
        settings = self._current_settings()

        backed_up = backup_originals(images, self._folder)
        if backed_up:
            self.status_label.setText(f"{backed_up} original(s) backed up.")
            QApplication.processEvents()

        failures = 0
        with Image.open(str(logo_path)) as logo_file:
            logo = logo_file.convert("RGBA")
            for index, image_path in enumerate(images, start=1):
                self.status_label.setText(f"[{index}/{len(images)}] {image_path.name}")
                QApplication.processEvents()
                try:
                    with Image.open(str(image_path)) as source:
                        result = apply_watermark(source.convert("RGB"), logo, **settings)
                    if image_path.suffix.lower() in (".jpg", ".jpeg"):
                        result.save(str(image_path), quality=95)
                    else:
                        result.save(str(image_path))
                except Exception as exc:
                    failures += 1
                    print(f"  {image_path.name}: {exc}", file=sys.stderr)

        message = f"Done. {len(images) - failures} of {len(images)} image(s) watermarked."
        if failures:
            message += f" {failures} failed - see the terminal for details."
        self.status_label.setText(message)
        self.restore_button.setEnabled(find_backup_for(self._folder) is not None)
        self._refresh_preview()

    def _restore(self) -> None:
        if self._folder is None:
            return
        confirmation = QMessageBox.question(
            self, "Restore originals",
            f"Copy the backed-up originals back over the current images in\n{self._folder}?",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        _count, message = restore_originals(self._folder)
        self.status_label.setText(message)
        self._refresh_preview()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Adaptive logo watermark. Opens a live preview by default; "
                     "--batch stamps a folder straight away, without a window."
    )
    parser.add_argument("folder", nargs="?", help="Folder to work on.")
    parser.add_argument("--subfolders", action="store_true",
                        help="Include subfolders (pre-checked in the window).")

    batch = parser.add_argument_group("batch mode (no window)")
    batch.add_argument("--batch", action="store_true",
                       help="Watermark every image in FOLDER and exit.")
    batch.add_argument("--restore", action="store_true",
                       help="Copy the backed-up originals in FOLDER back, and exit.")
    batch.add_argument("--logo", type=Path, default=None,
                       help='Logo image (default: "logo.png" next to this script).')
    batch.add_argument("--anchor", choices=sorted(ANCHOR_LABELS), default=DEFAULT_ANCHOR,
                       help=f"Where the logo sits (default: {DEFAULT_ANCHOR}).")
    batch.add_argument("--size", type=float, default=DEFAULT_SIZE_PERCENT,
                       help=f"Logo width, in percent of the image width "
                            f"(default: {DEFAULT_SIZE_PERCENT}).")
    batch.add_argument("--margin", type=float, default=DEFAULT_MARGIN_PERCENT,
                       help=f"Distance from the edge, in percent of the image width "
                            f"(default: {DEFAULT_MARGIN_PERCENT}).")
    batch.add_argument("--opacity", type=float, default=DEFAULT_OPACITY_PERCENT,
                       help=f"Stamp opacity in percent (default: {DEFAULT_OPACITY_PERCENT}).")
    batch.add_argument("--color", default="auto",
                       help="auto (measure the background), light, dark, or a grey value 0-255.")
    batch.add_argument("--dry-run", action="store_true",
                       help="List what would be watermarked, without writing anything.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    folder = Path(args.folder).expanduser().resolve() if args.folder else None

    if args.restore:
        if folder is None:
            print("--restore needs a folder.", file=sys.stderr)
            return 2
        return run_restore(folder)

    if args.batch or args.dry_run:
        if folder is None:
            print("--batch needs a folder.", file=sys.stderr)
            return 2
        return run_batch(
            folder,
            logo_path=args.logo,
            anchor=args.anchor,
            size_percent=args.size,
            margin_percent=args.margin,
            opacity_percent=args.opacity,
            color=args.color,
            recursive=args.subfolders,
            dry_run=args.dry_run,
        )

    if not QT_AVAILABLE:
        print(
            "The preview window needs PySide6 (pip install PySide6).\n"
            "Batch mode runs without it: python watermark.py FOLDER --batch",
            file=sys.stderr,
        )
        return 4

    app = QApplication(sys.argv)
    window = WatermarkWindow(start_folder=folder, recursive=args.subfolders)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))