"""InvenTree LabelPrintingMixin plugin that generates a 2-color 3MF per label.

The InvenTree pipeline renders the label template (typically a QR-code label)
to a PNG; this plugin then voxelizes that PNG into a flat 2-color tile and
writes a .3mf for the user to download. Slicers can assign a different
filament/extruder to each of the two objects in the 3MF for a clean
black-on-white (or any-color-on-any-color) printed label.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime

from django.core.files.base import ContentFile
from django.http import JsonResponse

from plugin import InvenTreePlugin
from plugin.mixins import LabelPrintingMixin, SettingsMixin

# `LabelOutput` moved between apps across InvenTree versions. Try the modern
# location first, then fall back to the legacy one.
try:
    from report.models import LabelOutput  # InvenTree >= 0.13
except ImportError:  # pragma: no cover
    from label.models import LabelOutput  # InvenTree < 0.13

from .mesh import generate_3mf_from_png
from .version import PLUGIN_VERSION


logger = logging.getLogger("inventree")


def _slugify(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)[:64] or "label"


class Inventree3MFQRLabel(LabelPrintingMixin, SettingsMixin, InvenTreePlugin):
    """Render an InvenTree label as a 2-color 3MF flat tile for 3D printing."""

    AUTHOR = "Jeremy"
    DESCRIPTION = (
        "Convert rendered InvenTree labels (typically QR-code labels) into a "
        "two-color flat-tile 3MF for 3D printing on multi-material printers."
    )
    VERSION = PLUGIN_VERSION
    NAME = "3MF QR Label"
    SLUG = "inventree-3mf-qr-label"
    TITLE = "3MF QR Label Generator"

    # The plugin runs synchronously and returns a downloadable file URL.
    BLOCKING_PRINT = True

    SETTINGS = {
        "USE_LABEL_DIMENSIONS": {
            "name": "Use label template dimensions",
            "description": (
                "If enabled, the printed tile uses the label template's own "
                "width and height. If disabled, the explicit settings below "
                "override them."
            ),
            "default": True,
            "validator": bool,
        },
        "WIDTH_MM": {
            "name": "Label width (mm)",
            "description": "Width of the printed tile in millimeters.",
            "default": 40.0,
            "validator": [float, lambda v: v > 0],
        },
        "HEIGHT_MM": {
            "name": "Label height (mm)",
            "description": "Height of the printed tile in millimeters.",
            "default": 40.0,
            "validator": [float, lambda v: v > 0],
        },
        "TOTAL_THICKNESS_MM": {
            "name": "Total thickness (mm)",
            "description": "Total Z thickness of the flat tile in millimeters.",
            "default": 1.2,
            "validator": [float, lambda v: v > 0],
        },
        "MODULE_MM": {
            "name": "Cell size (mm)",
            "description": (
                "Edge length of one cell of the down-sampled grid in mm. "
                "Smaller = more detail, larger mesh. 1.2 mm works well for "
                "QR codes printed at 0.4 mm nozzle."
            ),
            "default": 1.2,
            "validator": [float, lambda v: v >= 0.1],
        },
        "INK_NAME": {
            "name": "Ink object name",
            "description": "Display name for the dark/QR object in the 3MF.",
            "default": "QR Ink (dark)",
        },
        "BG_NAME": {
            "name": "Background object name",
            "description": "Display name for the light/background object in the 3MF.",
            "default": "Background (light)",
        },
        "THRESHOLD": {
            "name": "Black/white threshold (0-255)",
            "description": "Pixels darker than this threshold become QR ink.",
            "default": 128,
            "validator": [int, lambda v: 0 <= v <= 255],
        },
    }

    # ------------------------------------------------------------------ #
    # InvenTree LabelPrintingMixin entry points
    # ------------------------------------------------------------------ #

    def print_labels(self, label, items, request, **kwargs):
        """Generate one 3MF per item and return a single download URL.

        Multiple items are bundled into a ZIP. A single item is delivered as
        a bare .3mf so users can drag it straight into their slicer.
        """
        # Pull settings once.
        use_label_dim = bool(self.get_setting("USE_LABEL_DIMENSIONS"))
        width_mm = float(self.get_setting("WIDTH_MM"))
        height_mm = float(self.get_setting("HEIGHT_MM"))
        total_thickness_mm = float(self.get_setting("TOTAL_THICKNESS_MM"))
        module_mm = float(self.get_setting("MODULE_MM"))
        threshold = int(self.get_setting("THRESHOLD"))
        ink_name = self.get_setting("INK_NAME") or "QR Ink"
        bg_name = self.get_setting("BG_NAME") or "Background"

        if use_label_dim:
            template_w = float(getattr(label, "width", 0) or 0)
            template_h = float(getattr(label, "height", 0) or 0)
            if template_w > 0 and template_h > 0:
                width_mm, height_mm = template_w, template_h

        # Render one 3MF per item. We reuse the mixin's render_to_png so we
        # don't have to know the inner machinery of label rendering.
        outputs = []
        for item in items:
            try:
                png = self.render_to_png(label=label, item=item, **kwargs)
            except TypeError:
                # Older signature: render_to_png(item, ...)
                png = self.render_to_png(item, **kwargs)

            data = generate_3mf_from_png(
                png_image=png,
                width_mm=width_mm,
                height_mm=height_mm,
                total_thickness_mm=total_thickness_mm,
                module_mm=module_mm,
                threshold=threshold,
                ink_name=ink_name,
                bg_name=bg_name,
                title=self._label_title(item),
            )
            outputs.append((self._item_filename(item) + ".3mf", data))
            logger.info(
                "[%s] generated 3MF for item=%s (%d bytes)",
                self.SLUG,
                getattr(item, "pk", "?"),
                len(data),
            )

        # Required by some legacy LabelPrintingMixin implementations.
        # `print_label` is also invoked per-item by the base class, but our
        # override of `print_labels` short-circuits that path.

        if len(outputs) == 1:
            output_name, content = outputs[0]
            output_content = ContentFile(content, name=output_name)
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in outputs:
                    zf.writestr(name, data)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output_name = f"qr-labels-{stamp}.zip"
            output_content = ContentFile(buf.getvalue(), name=output_name)

        user = getattr(request, "user", None)
        output = LabelOutput.objects.create(label=output_content, user=user)

        return JsonResponse({
            "file": output.label.url,
            "success": True,
            "message": f"Generated {len(outputs)} 3MF label(s)",
        })

    def print_label(self, **kwargs):
        """Per-item hook (used by some InvenTree code paths).

        We mostly rely on the `print_labels` override above, but we keep this
        defined so the plugin remains compatible with older versions of
        InvenTree that drive `print_label` directly. It writes a single .3mf
        to a LabelOutput and returns nothing (the caller wraps the response).
        """
        png = kwargs.get("png_file")
        if png is None:
            # Some versions only pass pdf_data; try to rasterize it.
            png = self._png_from_pdf(kwargs.get("pdf_data"))

        if png is None:
            raise RuntimeError(
                "inventree-3mf-qr-label: neither png_file nor pdf_data was "
                "supplied to print_label"
            )

        width_mm = float(kwargs.get("width") or self.get_setting("WIDTH_MM"))
        height_mm = float(kwargs.get("height") or self.get_setting("HEIGHT_MM"))
        total_thickness_mm = float(self.get_setting("TOTAL_THICKNESS_MM"))
        module_mm = float(self.get_setting("MODULE_MM"))
        threshold = int(self.get_setting("THRESHOLD"))

        item = kwargs.get("item_instance")
        data = generate_3mf_from_png(
            png_image=png,
            width_mm=width_mm,
            height_mm=height_mm,
            total_thickness_mm=total_thickness_mm,
            module_mm=module_mm,
            threshold=threshold,
            ink_name=self.get_setting("INK_NAME") or "QR Ink",
            bg_name=self.get_setting("BG_NAME") or "Background",
            title=self._label_title(item),
        )

        name = self._item_filename(item) + ".3mf"
        user = kwargs.get("user")
        return LabelOutput.objects.create(
            label=ContentFile(data, name=name),
            user=user,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _item_filename(item) -> str:
        if item is None:
            return "qr-label"
        for attr in ("IPN", "name", "pk"):
            v = getattr(item, attr, None)
            if v:
                return f"qr-{_slugify(str(v))}"
        return "qr-label"

    @staticmethod
    def _label_title(item) -> str:
        if item is None:
            return "InvenTree QR Label"
        for attr in ("IPN", "name"):
            v = getattr(item, attr, None)
            if v:
                return f"InvenTree QR Label - {v}"
        return "InvenTree QR Label"

    @staticmethod
    def _png_from_pdf(pdf_data):
        """Best-effort PDF -> PIL.Image fallback. Requires pdf2image+poppler.

        Returns None if conversion is not available; the caller should treat
        that as a hard error.
        """
        if not pdf_data:
            return None
        try:
            from pdf2image import convert_from_bytes
        except ImportError:
            return None
        try:
            pages = convert_from_bytes(pdf_data, dpi=300)
        except Exception:  # pragma: no cover
            return None
        return pages[0] if pages else None
