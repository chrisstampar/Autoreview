#!/usr/bin/env python3
"""Generate Autoreview app icon (PNG iconset + optional .icns via iconutil)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Install Pillow: pip install Pillow", file=sys.stderr)
    sys.exit(1)

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"

# Tokyo Night–adjacent tile colors (match GUI chrome)
COLOR_TILE_FILL = (36, 40, 59, 255)
COLOR_TILE_OUTLINE = (122, 162, 247, 255)
COLOR_TEXT = (192, 202, 245, 255)

# filename -> pixel dimension (square)
MAC_ICONSET: list[tuple[str, int]] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

# Draw once at high resolution, then downscale for crisp edges
_BASE_SIZE = 1024
_cached_base: Image.Image | None = None
_font_cache: dict[int, ImageFont.ImageFont | ImageFont.FreeTypeFont] = {}


def _mono_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    if size not in _font_cache:
        for path in (
            "/System/Library/Fonts/SFNSMono.ttf",
            "/Library/Fonts/Courier New.ttf",
        ):
            try:
                _font_cache[size] = ImageFont.truetype(path, max(9, size // 6))
                break
            except OSError:
                continue
        else:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def _draw_at(size: int) -> Image.Image:
    """Dark rounded tile with magnifier + </> motif at ``size``×``size``."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 14)
    rad = max(6, size // 6)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=rad,
        fill=COLOR_TILE_FILL,
        outline=COLOR_TILE_OUTLINE,
        width=max(1, size // 64),
    )
    mr = max(4, size // 7)
    cx, cy = size // 2 - mr // 3, size // 2 - mr // 3
    draw.ellipse(
        [cx - mr, cy - mr, cx + mr, cy + mr],
        outline=COLOR_TILE_OUTLINE,
        width=max(1, size // 48),
    )
    hx0, hy0 = cx + int(mr * 0.55), cy + int(mr * 0.55)
    hx1 = hx0 + int(mr * 0.9)
    hy1 = hy0 + int(mr * 0.9)
    draw.line([(hx0, hy0), (hx1, hy1)], fill=COLOR_TILE_OUTLINE, width=max(2, size // 28))
    font = _mono_font(size)
    t = "</>"
    # Align with lens center (cx, cy), not tile center — matches magnifier geometry.
    oy = max(1, size // 128)  # slight downward nudge: monospace often looks optically high
    draw.text((cx, cy + oy), t, fill=COLOR_TEXT, font=font, anchor="mm")
    return img


def draw_icon(size: int) -> Image.Image:
    """Return icon bitmap; large master is drawn once and resized for smaller sizes."""
    global _cached_base
    if _cached_base is None:
        _cached_base = _draw_at(_BASE_SIZE)
    if size == _BASE_SIZE:
        return _cached_base.copy()
    return _cached_base.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Autoreview PNG iconset and optional .icns.")
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=ASSETS,
        help=f"Output directory (default: {ASSETS})",
    )
    args = parser.parse_args()
    assets_dir = args.assets_dir.resolve()
    iconset_dir = assets_dir / "Autoreview.iconset"

    assets_dir.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(assets_dir / "app_icon_256.png")

    iconset_dir.mkdir(parents=True, exist_ok=True)
    for name, dim in MAC_ICONSET:
        draw_icon(dim).save(iconset_dir / name)

    icns_path = assets_dir / "Autoreview.icns"
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
            check=True,
            capture_output=True,
        )
        print(f"Wrote {icns_path}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"iconutil skipped ({e!r}); PNGs still generated.", file=sys.stderr)

    print(f"Wrote {assets_dir / 'app_icon_256.png'} and iconset.")


if __name__ == "__main__":
    main()
