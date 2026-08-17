#!/usr/bin/env python3
"""
spinning-globe -- animated ASCII art of a rotating 3D planet.

Renders a shaded, rotating globe that fits your terminal exactly and reacts
to window resizes. Pure standard library, zero dependencies.

    python3 globe.py

It picks the best rendering for your screen size:
  * large terminals -> classic character shading
  * small terminals  -> braille dots (2x4 sub-pixels per cell) for sharp detail
"""

import argparse
import math
import os
import shutil
import sys
import time

# ---------------------------------------------------------------------------
# Shading ramps (dark -> bright)
# ---------------------------------------------------------------------------
RAMP_CLASSIC = " .:-=+*#%@"   # rich gradients, best on large terminals
RAMP_BLOCK   = " ░▒▓█"        # solid fills, good on medium screens
RAMP_SOLID   = "░▒▓█"         # darkest shade still visible -> solid disk
RAMP_MINIMAL = " .oO@"


def make_ramp(text):
    """Brightness 0..1 -> a single character."""
    text = text or RAMP_CLASSIC
    n = max(1, len(text) - 1)
    return lambda v: text[max(0, min(n, int(v * n)))]


# ---------------------------------------------------------------------------
# Shading helpers
# ---------------------------------------------------------------------------
def shade(brightness, light, ca, sa):
    """World-space brightness of a sphere normal (nx,ny,nz) after spin."""
    nx, ny, nz = brightness  # sphere-local normal
    wx = nx * ca + nz * sa
    wz = -nx * sa + nz * ca
    wy = ny
    b = wx * light[0] + wy * 0.4 + wz * light[1]
    return max(0.0, min(1.0, b * 1.15))  # boost contrast a touch


# ---------------------------------------------------------------------------
# Character-based frame rendering
# ---------------------------------------------------------------------------
def render_frame(cols, rows, tilt_deg, angle, ramp, light, grid_on, color=False):
    """Render one frame using a character ramp. Returns (text, None)."""
    r = max(1, min(cols // 4, int(rows * 0.42)))  # radius in units
    cx, cy = cols // 2, rows // 2
    tilt = math.radians(tilt_deg)
    cz, sz = math.cos(tilt), math.sin(tilt)
    ca, sa = math.cos(angle), math.sin(angle)
    lx, lz = light[0], light[1]

    grid_cells = set()
    if grid_on:
        for pts in _oriented_lines(tilt_deg, angle, 8, 5):
            for (px, py, pz) in pts:
                if pz <= 0:
                    continue
                ix = cx + int(round(px * 2 * r))
                iy = cy + int(round(py * r))
                if 0 <= ix < cols and 0 <= iy < rows:
                    grid_cells.add((iy, ix))

    out = []
    for iy in range(rows):
        dy = (iy - cy) / r
        if dy < -1.0 or dy > 1.0:
            out.append((" " * cols) if not color else ("\x1b[0m" + " " * cols))
            continue
        cell = []
        for ix in range(0, cols, 2):
            dx = ((ix + 1) - cx) / (2 * r)   # 2 cells per unit x
            d2 = dx * dx + dy * dy
            if d2 > 1.0:
                cell.append(" ")
                cell.append(" ")
                continue
            z = math.sqrt(1.0 - d2)
            b = shade((dx, dy, z), light, ca, sa)
            ch = ramp(b)
            if (iy, ix) in grid_cells or (iy, ix + 1) in grid_cells:
                ch = "#"
            if color:
                shade_c = 232 + int(b * 23)
                ch = ("\x1b[38;5;117m#" if ch == "#"
                      else "\x1b[38;5;%dm%s" % (shade_c, ch))
            cell.append(ch)
            cell.append(ch)
        if color:
            cell.append("\x1b[0m")
        out.append("".join(cell))
    return "\n".join(out), None


# ---------------------------------------------------------------------------
# Braille frame rendering (2x4 sub-pixels per cell -- sharp on small screens)
# ---------------------------------------------------------------------------
BRAILLE_BASE = 0x2800
# Dot -> bit. Within a cell: dot row 0..3, dot col 0..1.
# col 0 uses bits 0-3, col 1 uses bits 4-7.
def _braille_mask(row, col):
    return (1 << row) if col == 0 else (1 << (row + 4))


def render_braille_frame(cols, rows, tilt_deg, angle, light, grid_on,
                         threshold=0.16):
    """Render one frame as braille characters. Returns (text, None).

    Each terminal cell becomes a 2x4 dot bitmap, so a small terminal still
    shows a smooth, detailed sphere. Aspect: dots are roughly square.
    """
    W, H = cols * 2, rows * 4            # dot resolution
    cx, cy = W // 2, H // 2
    r = max(1, min(cx, cy) - 1)          # sphere radius in dots
    tilt = math.radians(tilt_deg)
    cz, sz = math.cos(tilt), math.sin(tilt)
    ca, sa = math.cos(angle), math.sin(angle)

    grid_dots = set()
    if grid_on:
        for pts in _oriented_lines(tilt_deg, angle, 8, 5):
            for (px, py, pz) in pts:
                if pz <= 0:
                    continue
                dx = cx + int(round(px * r))
                dy = cy + int(round(py * r))
                if 0 <= dx < W and 0 <= dy < H:
                    grid_dots.add((dy, dx))

    out = []
    for i in range(rows):
        line = []
        for j in range(cols):
            mask = 0
            for drr in range(4):
                for dcc in range(2):
                    dx = (2 * j + dcc - cx) / r
                    dy = (4 * i + drr - cy) / r
                    d2 = dx * dx + dy * dy
                    if d2 > 1.0:
                        continue
                    z = math.sqrt(1.0 - d2)
                    b = shade((dx, dy, z), light, ca, sa)
                    if b > threshold or (4 * i + drr, 2 * j + dcc) in grid_dots:
                        mask |= _braille_mask(drr, dcc)
            # Empty cell -> blank space keeps the background clean.
            line.append(chr(BRAILLE_BASE + mask) if mask else " ")
        out.append("".join(line))
    return "\n".join(out), None


# ---------------------------------------------------------------------------
# Grid polylines (oriented: tilt then spin), shared by both renderers
# ---------------------------------------------------------------------------
def _oriented_lines(tilt_deg, angle, n_meridians, n_parallels):
    """Yield latitude/longitude polylines in oriented unit-sphere coords."""
    tilt = math.radians(tilt_deg)
    cz, sz = math.cos(tilt), math.sin(tilt)
    ca, sa = math.cos(angle), math.sin(angle)
    res = []

    def spin(p):
        x, y, z = p
        return (x * ca + z * sa, y, -x * sa + z * ca)

    def point(lat, lon):
        x = math.cos(lat) * math.cos(lon)
        y = math.sin(lat)
        z = math.cos(lat) * math.sin(lon)
        ny = y * cz - z * sz
        nz = y * sz + z * cz
        return spin((x, ny, nz))

    for k in range(n_meridians):
        lon = 2 * math.pi * k / n_meridians
        res.append([point(-math.pi / 2 + math.pi * i / 64, lon) for i in range(65)])
    for k in range(n_parallels):
        lat = -math.pi / 2 + math.pi * (k + 1) / (n_parallels + 1)
        res.append([point(lat, 2 * math.pi * i / 64) for i in range(65)])
    return res


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="globe.py",
        description="Animated ASCII rotating globe that fits your terminal.",
    )
    ap.add_argument("--fps", type=float, default=20.0, help="frames/sec")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="rotation speed (negative = reverse)")
    ap.add_argument("--tilt", type=float, default=23.4,
                    help="axial tilt in degrees")
    ap.add_argument("--charset", default="auto",
                    choices=["auto", "classic", "block", "solid", "minimal",
                             "braille"],
                    help="rendering style; auto picks by terminal size")
    ap.add_argument("--grid", default="on", choices=["on", "off"],
                    help="overlay latitude/longitude lines")
    ap.add_argument("--color", default="auto",
                    choices=["auto", "on", "off"],
                    help="ANSI color shading (char styles only)")
    ap.add_argument("--cols", type=int, default=None)
    ap.add_argument("--rows", type=int, default=None)
    ap.add_argument("--frames", type=int, default=None,
                    help="render this many frames then exit (debug)")
    return ap.parse_args(argv)


def is_small(cols, rows):
    """A terminal is 'small' when character shading would get muddy."""
    return cols < 80 or rows < 24


def main(argv=None):
    args = parse_args(argv)

    # Resolve terminal size first so "auto" charset can adapt.
    if args.cols and args.rows:
        cols, rows = args.cols, args.rows
    else:
        try:
            cols, rows = shutil.get_terminal_size((80, 24))
        except Exception:
            cols, rows = 80, 24
    small = is_small(max(20, cols - 2), rows)

    charset = args.charset
    if charset == "auto":
        charset = "braille" if small else "classic"

    color_on = (args.color == "on" or
                (args.color == "auto" and
                 os.environ.get("TERM", "").lower() != "dumb" and
                 charset != "braille"))

    # On small char-mode terminals, auto-drop the grid: it overwhelms the globe.
    grid_on = args.grid == "on" and not (small and charset != "braille")

    ramps = {"classic": RAMP_CLASSIC, "block": RAMP_BLOCK,
             "solid": RAMP_SOLID, "minimal": RAMP_MINIMAL}
    light = (0.6, 0.8)

    frame_time = 1.0 / max(1.0, args.fps)
    ang = 0.0
    frame_count = 0
    try:
        sys.stdout.write("\x1b[2J\x1b[?25l")
        while True:
            t0 = time.perf_counter()
            if args.cols and args.rows:
                cols, rows = args.cols, args.rows
            else:
                try:
                    cols, rows = shutil.get_terminal_size((80, 24))
                except Exception:
                    cols, rows = 80, 24
            cols = max(20, cols - 2)

            if charset == "braille":
                text, _ = render_braille_frame(cols, rows, args.tilt, ang,
                                               light, grid_on)
            else:
                text, _ = render_frame(cols, rows, args.tilt, ang,
                                       make_ramp(ramps[charset]), light,
                                       grid_on, color_on)
            sys.stdout.write("\x1b[H" + text)
            sys.stdout.flush()

            ang += 0.045 * args.speed
            frame_count += 1
            if args.frames and frame_count >= args.frames:
                break
            time.sleep(max(0.0, frame_time - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[2J\x1b[H")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
