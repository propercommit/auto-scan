#!/usr/bin/env python3
"""Generate app icons for Auto Scan (.icns for macOS, .ico for Windows).

Run from the packaging/ directory:
    python icon_gen.py

Produces:
    icon.icns  — macOS app icon
    icon.ico   — Windows app icon
    icon_512.png — high-res PNG (for Linux / README)
"""

import io
import math
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _get_font(size: int):
    """Load a bold font for the icon text."""
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSDisplay-Bold.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def generate_icon(size: int = 512) -> Image.Image:
    """Generate a scanner-themed app icon at the given size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = size * 0.08
    s = size  # shorthand

    # ── Background: rounded rectangle with gradient feel ─────────
    # Base: dark blue rounded rect
    r = int(s * 0.18)  # corner radius
    draw.rounded_rectangle(
        [margin, margin, s - margin, s - margin],
        radius=r,
        fill=(20, 60, 140),
    )

    # Lighter overlay on top half for depth
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    ov_draw.rounded_rectangle(
        [margin, margin, s - margin, s * 0.55],
        radius=r,
        fill=(40, 100, 200, 80),
    )
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # ── Scanner body: white rectangle (the scanner lid) ──────────
    sx1 = s * 0.18
    sy1 = s * 0.28
    sx2 = s * 0.82
    sy2 = s * 0.52
    draw.rounded_rectangle(
        [sx1, sy1, sx2, sy2],
        radius=int(s * 0.03),
        fill=(240, 245, 255),
        outline=(180, 200, 230),
        width=max(1, int(s * 0.005)),
    )

    # Scanner glass line
    draw.line(
        [(sx1 + s * 0.05, sy2 - s * 0.04), (sx2 - s * 0.05, sy2 - s * 0.04)],
        fill=(100, 160, 230),
        width=max(2, int(s * 0.008)),
    )

    # ── Document coming out of scanner ───────────────────────────
    dx1 = s * 0.25
    dy1 = s * 0.48
    dx2 = s * 0.75
    dy2 = s * 0.78
    draw.rounded_rectangle(
        [dx1, dy1, dx2, dy2],
        radius=int(s * 0.02),
        fill=(255, 255, 255),
        outline=(200, 210, 225),
        width=max(1, int(s * 0.004)),
    )

    # Document text lines
    line_color = (180, 190, 210)
    line_w = max(2, int(s * 0.008))
    for i, frac in enumerate([0.56, 0.62, 0.68]):
        lx1 = s * 0.32
        lx2 = s * (0.68 - i * 0.04)
        ly = s * frac
        draw.line([(lx1, ly), (lx2, ly)], fill=line_color, width=line_w)

    # ── AI sparkle / checkmark badge ─────────────────────────────
    # Green circle with checkmark in bottom-right
    badge_r = s * 0.12
    badge_cx = s * 0.76
    badge_cy = s * 0.76
    draw.ellipse(
        [badge_cx - badge_r, badge_cy - badge_r,
         badge_cx + badge_r, badge_cy + badge_r],
        fill=(34, 197, 94),
        outline=(20, 140, 70),
        width=max(1, int(s * 0.005)),
    )

    # Checkmark
    ck_w = max(2, int(s * 0.02))
    cx, cy = badge_cx, badge_cy
    points = [
        (cx - badge_r * 0.45, cy - badge_r * 0.05),
        (cx - badge_r * 0.1, cy + badge_r * 0.35),
        (cx + badge_r * 0.5, cy - badge_r * 0.35),
    ]
    draw.line(points[:2], fill="white", width=ck_w)
    draw.line(points[1:], fill="white", width=ck_w)

    return img


def save_png(img: Image.Image, path: Path) -> None:
    """Save as PNG."""
    img.save(path, format="PNG")
    print(f"  Created {path} ({path.stat().st_size:,} bytes)")


def save_ico(img: Image.Image, path: Path) -> None:
    """Save as .ico with multiple sizes for Windows."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icons = []
    for sz in sizes:
        resized = img.resize((sz, sz), Image.LANCZOS)
        icons.append(resized)
    icons[0].save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=icons[1:])
    print(f"  Created {path} ({path.stat().st_size:,} bytes)")


def save_icns(img: Image.Image, path: Path) -> None:
    """Save as .icns for macOS using iconutil."""
    iconset = path.parent / "icon.iconset"
    iconset.mkdir(exist_ok=True)

    # macOS icon sizes: 16, 32, 128, 256, 512 (and @2x variants)
    icon_sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }

    for name, sz in icon_sizes.items():
        resized = img.resize((sz, sz), Image.LANCZOS)
        resized.save(iconset / name, format="PNG")

    # Use iconutil to create .icns
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(path)],
            check=True, capture_output=True,
        )
        print(f"  Created {path} ({path.stat().st_size:,} bytes)")
    except FileNotFoundError:
        print("  Warning: iconutil not found (macOS only). Skipping .icns.")
    except subprocess.CalledProcessError as e:
        print(f"  Warning: iconutil failed: {e.stderr.decode()}")
    finally:
        # Clean up iconset
        import shutil
        shutil.rmtree(iconset, ignore_errors=True)


def main():
    out_dir = Path(__file__).parent
    print("Generating Auto Scan icons...")

    # Generate high-res base icon (1024px for Retina)
    icon = generate_icon(1024)

    save_png(icon.resize((512, 512), Image.LANCZOS), out_dir / "icon_512.png")
    save_ico(icon, out_dir / "icon.ico")
    save_icns(icon, out_dir / "icon.icns")

    print("Done!")


if __name__ == "__main__":
    main()
