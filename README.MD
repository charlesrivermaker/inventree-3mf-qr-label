# inventree-3mf-qr-label

An [InvenTree](https://inventree.org) label-printing plugin that turns any
rendered label (typically one with a QR code on it) into a **two-color flat
tile 3MF** ready to drop into your slicer of choice.

The plugin treats InvenTree's normal label-render pipeline as the design
tool: you build the label as you would for any other printer (HTML/CSS,
QR-code template tag, optional text, frame, logo, etc.), and this plugin
voxelizes the rasterized PNG into a 2-color flat tile.

```
+--------------------------------+        +--------------------------------+
|  InvenTree label template      |  -->   |  Flat 2-color 3MF tile        |
|  (HTML + qrcode template tag)  |        |  width x height x thickness   |
|     -> rendered PNG            |        |  Part 1 = QR ink (dark)       |
+--------------------------------+        |  Part 2 = background          |
                                          +--------------------------------+
```

## What you get

Each printed label produces a single `.3mf` file containing **one object
made of two parts** sharing the exact same Z range (`0` to `total_thickness`):

| Part name (configurable)    | Geometry                                      |
| --------------------------- | --------------------------------------------- |
| `QR Ink (dark)`             | Rectangular prisms covering all dark pixels   |
| `Background (light)`        | Rectangular prisms covering all light pixels  |

Together the two parts tile a perfect `width x height` rectangle. The top
and bottom faces are flat. Any slicer that lets you assign a different
filament/extruder per part (Cura, PrusaSlicer, OrcaSlicer, Bambu Studio,
Slic3r, etc.) will produce a flat-top, two-color label with no supports
required.

The 3MF is written in Bambu Studio project format, so **OrcaSlicer, Snapmaker
Orca and Bambu Studio open it with each part already assigned to its
filament slot** (see *Ink filament slot* / *Background filament slot*).

If you print 1 item the plugin returns a bare `.3mf`; multiple items are
bundled into a single zip for download.

## Installation

```bash
pip install inventree-3mf-qr-label
```

Then in InvenTree, go to **Settings -> Plugins**, enable **3MF QR Label
Generator**, and reload.

If you'd like a PDF -> PNG fallback for older InvenTree versions that don't
populate `png_file`, install with:

```bash
pip install 'inventree-3mf-qr-label[pdf-fallback]'
```

This adds `pdf2image` (and you'll also need `poppler` on the system).

## Settings

All settings are exposed under the plugin's settings page in InvenTree:

| Setting                  | Default | Description                                          |
| ------------------------ | ------- | ---------------------------------------------------- |
| Use label dimensions     | `true`  | Use the InvenTree template's mm width/height         |
| Label width              | `40 mm` | Override width (when "Use label dimensions" is off)  |
| Label height             | `40 mm` | Override height (when "Use label dimensions" is off) |
| Total thickness          | `1.2 mm`| Z thickness of the flat tile                         |
| Cell size                | `1.2 mm`| Edge length of one downsampled grid cell             |
| Black/white threshold    | `128`   | 0-255 grayscale cutoff between ink and background    |
| Ink object name          | "QR Ink (dark)"     | Name shown in the slicer                 |
| Background object name   | "Background (light)"| Name shown in the slicer                 |
| Ink filament slot        | `2`     | Filament slot for the ink part (Orca / Bambu)        |
| Background filament slot | `1`     | Filament slot for the background part (Orca / Bambu) |

A note on **cell size**: this is what controls the resolution of the QR
modules in the print. For a 0.4 mm nozzle, `1.2 mm` (= 3 perimeters per
module) is a comfortable lower bound. Pushing below `0.8 mm` per module
tends to make narrow QR features blur into each other.

## Building your label template

Use InvenTree's built-in `qrcode` template tag inside any normal HTML label
template. Anything that renders as solid black on white in the PNG will
become "ink" geometry in the 3MF; anything else becomes background. So you
can include text, a frame, a small logo, etc. and they'll come out in two
colors as well.

If you only want the QR code itself in the print, design your label
template with only the QR code (no text, no border).

## Slicer setup

After the print job, drop the `.3mf` into your slicer. You'll see one object
with two parts named as you configured.

In **OrcaSlicer, Snapmaker Orca or Bambu Studio** the parts are already
assigned to the configured filament slots -- just load your dark filament in
the ink slot and your light filament in the background slot. In other
slicers, assign them by hand:

- **Part 1 ("QR Ink")** -> your dark filament (e.g. black)
- **Part 2 ("Background")** -> your light filament (e.g. white)

Slice and print. Because both parts span the full Z range and tile the
plate exactly, the slicer will alternate filaments per layer (on
multi-material printers) or you can use one filament swap at the bottom
layer if you only own a single-extruder machine -- though for a true
flat-top two-color result a multi-material printer is recommended.

## How it works

1. InvenTree renders the label template to a PNG (greyscale, high DPI).
2. The plugin downsamples the PNG to a `cols x rows` grid where each cell
   is `module_mm` on a side.
3. Each row is run-length-encoded into runs of "ink" or "background"
   cells, producing two lists of axis-aligned rectangles.
4. Each rectangle becomes a 12-triangle box prism in the 3MF.
5. The two collections (ink + background) become two `<object>` elements
   in `3D/3dmodel.model`, each referenced from a `<build>` `<item>`.
6. The whole thing is zipped with the standard 3MF content-types and
   relationship XMLs.

The output is dependency-light: the only runtime requirement is Pillow
(which InvenTree already ships).

## License

MIT
