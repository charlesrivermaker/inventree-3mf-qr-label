"""
3MF generator for two-color flat-tile QR labels.

The output 3MF contains ONE object built from TWO parts:

    Part 1 ("ink"):        rectangular prisms covering the dark cells.
    Part 2 ("background"): rectangular prisms covering the light cells.

Together they tile a perfect WxH rectangle of total_thickness_mm. The file
is written in the Bambu Studio project flavour (Metadata/model_settings.config)
so OrcaSlicer, Snapmaker Orca and Bambu Studio load each part with its
filament slot already assigned. Other slicers read the standard 3MF core and
let the user assign filaments per part manually.

The generator is dependency-light: it builds the .model XML by hand and
zips it with stdlib zipfile, so the only runtime requirement is Pillow.
"""

from __future__ import annotations

import io
import zipfile
from typing import Iterable, List, Tuple
from xml.sax.saxutils import escape

from PIL import Image


# ---------------------------------------------------------------------------
# 3MF package boilerplate
# ---------------------------------------------------------------------------

CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="config" ContentType="text/xml"/>
</Types>
"""

ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel-1"
                Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

MODEL_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
BAMBU_NS = "http://schemas.bambulab.com/package/2021"

# OrcaSlicer (and forks such as Snapmaker Orca) and Bambu Studio only honour
# the per-part filament assignments in Metadata/model_settings.config when the
# model's Application metadata identifies a Bambu-format project. Files from
# any other application are loaded as "geometry only".
APPLICATION = "BambuStudio-01.09.00.00"

# Object ids inside 3D/3dmodel.model. The two meshes are separate resources
# and a third object assembles them as components, so the slicer sees ONE
# object with two parts that always move together.
INK_ID = 1
BG_ID = 2
LABEL_ID = 3


def _attr(value: str) -> str:
    """Escape a string for use inside a double-quoted XML attribute."""
    return escape(value, {'"': "&quot;"})


# ---------------------------------------------------------------------------
# Image -> rectangle list
# ---------------------------------------------------------------------------

Rect = Tuple[float, float, float, float]  # (x0, y0, x1, y1) in mm


def png_to_grid(
    png_image: Image.Image,
    cols: int,
    rows: int,
    threshold: int = 128,
) -> List[List[bool]]:
    """Resample a PIL image down to a `cols x rows` boolean grid (True = ink)."""
    if cols < 1 or rows < 1:
        raise ValueError("cols and rows must be >= 1")
    img = png_image.convert("L")
    if img.size != (cols, rows):
        # NEAREST keeps the binary look crisp; LANCZOS smears finder patterns.
        img = img.resize((cols, rows), Image.NEAREST)
    px = img.load()
    return [[px[x, y] < threshold for x in range(cols)] for y in range(rows)]


def grid_to_rectangles(
    grid: List[List[bool]],
    width_mm: float,
    height_mm: float,
) -> Tuple[List[Rect], List[Rect]]:
    """Run-length encode the grid into per-row rectangles for each color.

    Returns (ink_rects, background_rects). Together they tile width x height.
    Y is flipped so that grid row 0 (top of the image) maps to the back of the
    print bed and the QR comes out right-reading when looking down at the tile.
    """
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    if rows == 0 or cols == 0:
        return [], []

    cell_w = width_mm / cols
    cell_h = height_mm / rows

    ink: List[Rect] = []
    bg: List[Rect] = []

    for j, row in enumerate(grid):
        # Flip vertically: row 0 (top of PNG) -> highest Y on the print bed.
        y0 = (rows - 1 - j) * cell_h
        y1 = y0 + cell_h

        i = 0
        while i < cols:
            run_value = row[i]
            start = i
            while i < cols and row[i] == run_value:
                i += 1
            x0 = start * cell_w
            x1 = i * cell_w
            (ink if run_value else bg).append((x0, y0, x1, y1))

    return ink, bg


# ---------------------------------------------------------------------------
# Mesh construction
# ---------------------------------------------------------------------------

def _box_vertices(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float):
    return (
        (x0, y0, z0),  # 0
        (x1, y0, z0),  # 1
        (x1, y1, z0),  # 2
        (x0, y1, z0),  # 3
        (x0, y0, z1),  # 4
        (x1, y0, z1),  # 5
        (x1, y1, z1),  # 6
        (x0, y1, z1),  # 7
    )


def _box_triangles(b: int):
    """Return 12 triangles (CCW, outward-facing) for a box whose 8 vertices
    start at index `b` in the mesh's vertex list."""
    return (
        # bottom face (z = z0), normal -Z, CCW seen from below
        (b + 0, b + 3, b + 2), (b + 0, b + 2, b + 1),
        # top face (z = z1), normal +Z, CCW seen from above
        (b + 4, b + 5, b + 6), (b + 4, b + 6, b + 7),
        # front face (y = y0), normal -Y
        (b + 0, b + 1, b + 5), (b + 0, b + 5, b + 4),
        # right face (x = x1), normal +X
        (b + 1, b + 2, b + 6), (b + 1, b + 6, b + 5),
        # back face (y = y1), normal +Y
        (b + 2, b + 3, b + 7), (b + 2, b + 7, b + 6),
        # left face (x = x0), normal -X
        (b + 3, b + 0, b + 4), (b + 3, b + 4, b + 7),
    )


def _build_mesh(rectangles: Iterable[Rect], z0: float, z1: float):
    vertices: List[Tuple[float, float, float]] = []
    triangles: List[Tuple[int, int, int]] = []
    for x0, y0, x1, y1 in rectangles:
        base = len(vertices)
        vertices.extend(_box_vertices(x0, y0, z0, x1, y1, z1))
        triangles.extend(_box_triangles(base))
    return vertices, triangles


def _mesh_xml(vertices, triangles, indent: str = "        ") -> str:
    v_lines = "\n".join(
        f'{indent}  <vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>'
        for x, y, z in vertices
    )
    t_lines = "\n".join(
        f'{indent}  <triangle v1="{a}" v2="{b}" v3="{c}"/>'
        for a, b, c in triangles
    )
    return (
        f"{indent}<mesh>\n"
        f"{indent}  <vertices>\n"
        f"{v_lines}\n"
        f"{indent}  </vertices>\n"
        f"{indent}  <triangles>\n"
        f"{t_lines}\n"
        f"{indent}  </triangles>\n"
        f"{indent}</mesh>"
    )


def _concat_meshes(*meshes):
    """Concatenate multiple (vertices, triangles) mesh pieces into one,
    offsetting triangle indices so the combined mesh stays valid."""
    all_v: List = []
    all_t: List = []
    for v_list, t_list in meshes:
        offset = len(all_v)
        all_v.extend(v_list)
        all_t.extend((a + offset, b + offset, c + offset) for a, b, c in t_list)
    return all_v, all_t


def _model_xml(
    ink_rects: List[Rect],
    bg_rects: List[Rect],
    width_mm: float,
    height_mm: float,
    total_thickness_mm: float,
    base_thickness_mm: float,
    raise_height_mm: float,
    ink_name: str,
    bg_name: str,
    title: str,
) -> str:
    """Build the model XML with three optional Z bands:

    * ``[0, base]``           - solid base plate in the background color (skipped
                                if ``base_thickness_mm == 0``). This is the
                                "no color change for the first N mm" band.
    * ``[base, total]``       - the two-color swap region. Ink and background
                                rectangles both fill this band, tiling the WxH
                                surface flat.
    * ``[total, total+raise]``- ink-only band. Where the QR/text pixels are
                                dark, the ink protrudes above the flat top by
                                ``raise_height_mm``. Skipped if 0.
    """
    z_swap_bot = base_thickness_mm
    z_swap_top = total_thickness_mm
    z_ink_top = total_thickness_mm + raise_height_mm

    # Ink object: rectangles from z_swap_bot up to z_ink_top (one piece).
    ink_v, ink_t = _build_mesh(ink_rects, z_swap_bot, z_ink_top)

    # Background object: swap-region rectangles from z_swap_bot to z_swap_top.
    bg_pieces = [_build_mesh(bg_rects, z_swap_bot, z_swap_top)]

    # If a base plate is requested, add a single full-WxH box from 0 to base.
    if base_thickness_mm > 0:
        bg_pieces.append(
            _build_mesh([(0.0, 0.0, width_mm, height_mm)], 0.0, base_thickness_mm)
        )

    bg_v, bg_t = _concat_meshes(*bg_pieces)

    ink_mesh = _mesh_xml(ink_v, ink_t)
    bg_mesh = _mesh_xml(bg_v, bg_t)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{MODEL_NS}" '
        f'xmlns:BambuStudio="{BAMBU_NS}">\n'
        f'  <metadata name="Application">{APPLICATION}</metadata>\n'
        '  <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        f'  <metadata name="Title">{escape(title)}</metadata>\n'
        '  <metadata name="Designer">inventree-3mf-qr-label</metadata>\n'
        '  <resources>\n'
        f'    <object id="{INK_ID}" type="model" name="{_attr(ink_name)}">\n'
        f'{ink_mesh}\n'
        '    </object>\n'
        f'    <object id="{BG_ID}" type="model" name="{_attr(bg_name)}">\n'
        f'{bg_mesh}\n'
        '    </object>\n'
        f'    <object id="{LABEL_ID}" type="model" name="{_attr(title)}">\n'
        '      <components>\n'
        f'        <component objectid="{INK_ID}"/>\n'
        f'        <component objectid="{BG_ID}"/>\n'
        '      </components>\n'
        '    </object>\n'
        '  </resources>\n'
        '  <build>\n'
        f'    <item objectid="{LABEL_ID}" printable="1"/>\n'
        '  </build>\n'
        '</model>\n'
    )


def _model_settings_xml(
    ink_name: str,
    bg_name: str,
    title: str,
    ink_filament: int,
    bg_filament: int,
) -> str:
    """Bambu/Orca per-object config: assigns a filament slot to each part.

    The ``extruder`` key is the 1-based filament slot in the slicer's
    filament list, so the two parts come in already colored.
    """
    def part(part_id: int, name: str, filament: int) -> str:
        return (
            f'    <part id="{part_id}" subtype="normal_part">\n'
            f'      <metadata key="name" value="{_attr(name)}"/>\n'
            '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
            f'      <metadata key="extruder" value="{filament}"/>\n'
            '    </part>\n'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n'
        f'  <object id="{LABEL_ID}">\n'
        f'    <metadata key="name" value="{_attr(title)}"/>\n'
        f'    <metadata key="extruder" value="{bg_filament}"/>\n'
        f'{part(INK_ID, ink_name, ink_filament)}'
        f'{part(BG_ID, bg_name, bg_filament)}'
        '  </object>\n'
        '  <plate>\n'
        '    <metadata key="plater_id" value="1"/>\n'
        '    <metadata key="plater_name" value=""/>\n'
        '    <metadata key="locked" value="false"/>\n'
        '    <model_instance>\n'
        f'      <metadata key="object_id" value="{LABEL_ID}"/>\n'
        '      <metadata key="instance_id" value="0"/>\n'
        '    </model_instance>\n'
        '  </plate>\n'
        '</config>\n'
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_3mf_from_png(
    png_image: Image.Image,
    width_mm: float,
    height_mm: float,
    total_thickness_mm: float = 1.2,
    module_mm: float = 0.4,
    threshold: int = 128,
    base_thickness_mm: float = 0.0,
    raise_height_mm: float = 0.0,
    ink_name: str = "QR Ink (dark)",
    bg_name: str = "Background (light)",
    title: str = "InvenTree QR Label",
    ink_filament: int = 2,
    bg_filament: int = 1,
) -> bytes:
    """Convert a rendered InvenTree label PNG into a 2-color flat-tile 3MF.

    Parameters
    ----------
    png_image : PIL.Image
        The rasterized label as delivered by InvenTree's `print_label` hook.
    width_mm, height_mm : float
        Physical size of the label in millimeters.
    total_thickness_mm : float
        Total Z thickness of the flat tile.
    module_mm : float
        Edge length of one cell of the down-sampled grid in millimeters.
        Lower values preserve more detail at the cost of a larger mesh.
    threshold : int
        Grayscale threshold (0-255). Pixels darker than this become ink.
    ink_name, bg_name : str
        Display names for the two objects in the 3MF (visible in slicers).
    title : str
        Title metadata (also the object name in the slicer).
    ink_filament, bg_filament : int
        1-based filament slots assigned to the ink and background parts.
        OrcaSlicer, Snapmaker Orca and Bambu Studio apply these on load.

    Returns
    -------
    bytes : the .3mf file (a ZIP container).
    """
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("width_mm and height_mm must be positive")
    if total_thickness_mm <= 0:
        raise ValueError("total_thickness_mm must be positive")
    if module_mm <= 0:
        raise ValueError("module_mm must be positive")
    if base_thickness_mm < 0:
        raise ValueError("base_thickness_mm must be >= 0")
    if raise_height_mm < 0:
        raise ValueError("raise_height_mm must be >= 0")
    if ink_filament < 1 or bg_filament < 1:
        raise ValueError("ink_filament and bg_filament must be >= 1")
    if base_thickness_mm >= total_thickness_mm:
        raise ValueError(
            f"base_thickness_mm ({base_thickness_mm}) must be less than "
            f"total_thickness_mm ({total_thickness_mm}) - the color-swap "
            "region needs some Z extent."
        )

    cols = max(1, int(round(width_mm / module_mm)))
    rows = max(1, int(round(height_mm / module_mm)))

    grid = png_to_grid(png_image, cols, rows, threshold=threshold)
    ink_rects, bg_rects = grid_to_rectangles(grid, width_mm, height_mm)

    # Edge case: a uniform image (all ink or all background) yields one empty
    # mesh, which some slicers reject. Drop the empty object.
    if not ink_rects and not bg_rects:
        raise ValueError("No geometry generated from input image")

    model_xml = _model_xml(
        ink_rects=ink_rects,
        bg_rects=bg_rects,
        width_mm=width_mm,
        height_mm=height_mm,
        total_thickness_mm=total_thickness_mm,
        base_thickness_mm=base_thickness_mm,
        raise_height_mm=raise_height_mm,
        ink_name=ink_name,
        bg_name=bg_name,
        title=title,
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", ROOT_RELS_XML)
        zf.writestr("3D/3dmodel.model", model_xml)
        zf.writestr(
            "Metadata/model_settings.config",
            _model_settings_xml(
                ink_name=ink_name,
                bg_name=bg_name,
                title=title,
                ink_filament=ink_filament,
                bg_filament=bg_filament,
            ),
        )
    return buf.getvalue()
