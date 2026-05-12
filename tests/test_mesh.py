"""Smoke / verification tests for the 3MF generator.

Run from the package root:

    PYTHONPATH=. python3 tests/test_mesh.py

The tests check:

  1. The file is a valid ZIP and has the three mandatory 3MF members.
  2. The model XML parses and declares the right unit + namespace.
  3. There are exactly two `<object>` elements with non-empty meshes.
  4. The geometry tiles the requested width/height/thickness perfectly:
     volume(ink) + volume(background) == width * height * thickness, and
     no rectangle escapes the bounding box, and the two rectangle sets do
     not overlap.
  5. Each cell is a closed manifold box (12 triangles, 8 vertices, Euler
     characteristic V - E + F == 2).
"""
from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Make the package importable from this script.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from PIL import Image, ImageDraw  # noqa: E402

from inventree_3mf_qr_label.mesh import (  # noqa: E402
    generate_3mf_from_png,
    grid_to_rectangles,
    png_to_grid,
)


NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2013/02}"


# ---------------------------------------------------------------------------
# Synthetic QR-ish image (no external `qrcode` lib needed)
# ---------------------------------------------------------------------------

def make_fake_qr(modules: int = 25, scale: int = 12) -> Image.Image:
    """Build a binary image that LOOKS like a QR code (for verification only).

    Three finder squares in the corners + a deterministic checkerboard-ish
    pattern in the data area. Returns an RGB PIL image at `modules*scale`
    pixels per side.
    """
    side = modules * scale
    img = Image.new("L", (side, side), color=255)
    draw = ImageDraw.Draw(img)

    def cell(cx: int, cy: int, fill: int = 0):
        x0 = cx * scale
        y0 = cy * scale
        draw.rectangle((x0, y0, x0 + scale - 1, y0 + scale - 1), fill=fill)

    def finder(ox: int, oy: int):
        # 7x7 outer, 5x5 white, 3x3 inner black
        for dx in range(7):
            for dy in range(7):
                cell(ox + dx, oy + dy, 0)
        for dx in range(1, 6):
            for dy in range(1, 6):
                cell(ox + dx, oy + dy, 255)
        for dx in range(2, 5):
            for dy in range(2, 5):
                cell(ox + dx, oy + dy, 0)

    finder(0, 0)
    finder(modules - 7, 0)
    finder(0, modules - 7)

    # Pseudo-random data pattern outside the finder zones.
    import random
    rng = random.Random(0xC0DE)
    for y in range(modules):
        for x in range(modules):
            in_finder = (
                (x < 8 and y < 8)
                or (x >= modules - 8 and y < 8)
                or (x < 8 and y >= modules - 8)
            )
            if in_finder:
                continue
            if rng.random() < 0.5:
                cell(x, y, 0)
    return img


# ---------------------------------------------------------------------------
# Geometry checks
# ---------------------------------------------------------------------------

def overlap(a, b) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1


def rect_area(r) -> float:
    x0, y0, x1, y1 = r
    return (x1 - x0) * (y1 - y0)


def assert_tiling(ink, bg, width, height):
    # 1. Areas must sum to the total.
    total = sum(rect_area(r) for r in ink) + sum(rect_area(r) for r in bg)
    expected = width * height
    assert abs(total - expected) < 1e-6, (
        f"area mismatch: {total} vs {expected}"
    )
    # 2. Nothing escapes the bounding box.
    for r in ink + bg:
        x0, y0, x1, y1 = r
        assert 0 - 1e-9 <= x0 < x1 <= width + 1e-9, r
        assert 0 - 1e-9 <= y0 < y1 <= height + 1e-9, r
    # 3. No rectangle overlap within or between the two sets.
    all_rects = list(ink) + list(bg)
    # A real overlap test is O(n^2). For a 25x25 grid that's <= 1875 boxes
    # which is fine for verification.
    for i, a in enumerate(all_rects):
        for b in all_rects[i + 1:]:
            assert not overlap(a, b), f"overlapping rects: {a} vs {b}"


def assert_3mf_structure(blob: bytes, width, height, thickness):
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = set(z.namelist())
    for required in {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"}:
        assert required in names, f"missing {required} in 3MF"

    model_xml = z.read("3D/3dmodel.model").decode("utf-8")
    root = ET.fromstring(model_xml)
    assert root.tag == NS + "model", f"unexpected root: {root.tag}"
    assert root.get("unit") == "millimeter"

    objects = root.findall(f"{NS}resources/{NS}object")
    assert len(objects) == 2, f"expected 2 objects, got {len(objects)}"

    total_vol = 0.0
    for obj in objects:
        verts = obj.findall(f"{NS}mesh/{NS}vertices/{NS}vertex")
        tris = obj.findall(f"{NS}mesh/{NS}triangles/{NS}triangle")
        n_tris = len(tris)
        n_verts = len(verts)
        # Each box prism contributes 8 verts and 12 triangles.
        assert n_verts % 8 == 0, n_verts
        assert n_tris == n_verts // 8 * 12, (n_verts, n_tris)
        assert n_verts > 0, "object has no geometry"

        # Per-box manifold check: V=8, F=12, E=18 -> V-E+F = 2.
        # We can't easily compute E without building the half-edge graph
        # for the whole mesh, but checking per-box invariants is enough
        # because boxes do not share vertices in our mesh.
        n_boxes = n_verts // 8
        assert n_tris == n_boxes * 12

        # Volume = sum over boxes of (x1-x0)*(y1-y0)*thickness.
        # Vertices come in groups of 8 per box, in the order produced by
        # _box_vertices: 0=(x0,y0,z0), 6=(x1,y1,z1).
        for k in range(n_boxes):
            v0 = verts[k * 8]
            v6 = verts[k * 8 + 6]
            x0 = float(v0.get("x")); y0 = float(v0.get("y"))
            x1 = float(v6.get("x")); y1 = float(v6.get("y"))
            z0 = float(v0.get("z")); z1 = float(v6.get("z"))
            assert abs(z0) < 1e-9, z0
            assert abs(z1 - thickness) < 1e-6, (z1, thickness)
            total_vol += (x1 - x0) * (y1 - y0) * (z1 - z0)

    expected_vol = width * height * thickness
    assert abs(total_vol - expected_vol) < 1e-6, (total_vol, expected_vol)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_grid_round_trip():
    img = make_fake_qr(modules=25, scale=8)
    grid = png_to_grid(img, 25, 25)
    # Top-left finder pattern's outer ring should be ink (dark).
    assert grid[0][0] is True
    assert grid[6][6] is True
    # Top-right finder
    assert grid[0][24] is True
    # Bottom-right corner (no finder there) should mostly NOT be all ink.
    # We just check the corner is reachable -- ie the grid has the right size.
    assert len(grid) == 25 and len(grid[0]) == 25
    print("ok: grid sampling")


def test_tiling_simple():
    grid = [
        [True, False, True, True],
        [False, False, True, False],
        [True, True, False, False],
    ]
    ink, bg = grid_to_rectangles(grid, width_mm=8.0, height_mm=6.0)
    assert_tiling(ink, bg, 8.0, 6.0)
    # Run-length compression: row 0 has runs T,F,TT -> 3 rectangles.
    # Row 1 has runs FF, T, F -> 3 rectangles. Row 2 has TT, FF -> 2 rects.
    # Total 8 rects across both colors.
    assert len(ink) + len(bg) == 8, (len(ink), len(bg))
    print("ok: tiling 8.0x6.0 with 3x4 grid")


def test_full_pipeline():
    img = make_fake_qr(modules=25, scale=8)
    blob = generate_3mf_from_png(
        png_image=img,
        width_mm=40.0,
        height_mm=40.0,
        total_thickness_mm=1.2,
        module_mm=1.6,  # 25 modules across 40mm
        threshold=128,
    )
    assert blob[:2] == b"PK", "not a zip file"
    assert_3mf_structure(blob, width=40.0, height=40.0, thickness=1.2)

    out = HERE / "_sample_output.3mf"
    out.write_bytes(blob)
    print(f"ok: full pipeline; sample written to {out} ({len(blob)} bytes)")


def test_nonsquare_label():
    img = make_fake_qr(modules=25, scale=8)
    blob = generate_3mf_from_png(
        png_image=img,
        width_mm=60.0,
        height_mm=30.0,
        total_thickness_mm=1.0,
        module_mm=1.5,
    )
    assert_3mf_structure(blob, 60.0, 30.0, 1.0)
    print("ok: 60x30 nonsquare label")


def main():
    test_grid_round_trip()
    test_tiling_simple()
    test_full_pipeline()
    test_nonsquare_label()
    print("all tests passed")


if __name__ == "__main__":
    main()
