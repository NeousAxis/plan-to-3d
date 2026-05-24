#!/usr/bin/env python3
"""
plan-to-3d input normalizer.

Turns ANY supported plan file into one or more clean PNG images that the
vision step can read. This is what lets the skill accept "all file types".

Supported:
  - Raster images  (.png .jpg .jpeg .webp .gif .bmp .tif .tiff)  -> Pillow
        EXIF orientation fixed, flattened on white, exported as PNG
  - PDF            (.pdf)                                          -> PyMuPDF
        every page rendered to PNG at the requested DPI
  - SVG            (.svg)                                          -> cairosvg
        rasterized if cairosvg is installed, otherwise reported
  - Vector CAD     (.dxf .dwg)                                     -> reported
        not rasterized here; export/screenshot to image or PDF first

Usage:
  python3 ingest.py PLAN_FILE [--out DIR] [--dpi 200] [--max-pages N]

Prints, one per line, the PNG path(s) produced. Exit code 0 on success.
"""

import argparse
import os
import sys

RASTER_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _slug(path):
    return os.path.splitext(os.path.basename(path))[0]


def ingest_raster(path, out_dir, stem):
    from PIL import Image, ImageOps
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # honour camera/scanner rotation
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    dst = os.path.join(out_dir, f"{stem}.png")
    img.save(dst, "PNG")
    return [dst]


def ingest_pdf(path, out_dir, stem, dpi, max_pages):
    import fitz  # PyMuPDF
    doc = fitz.open(path)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    out = []
    n = len(doc) if max_pages is None else min(max_pages, len(doc))
    for i in range(n):
        pix = doc[i].get_pixmap(matrix=mat, alpha=False)
        dst = os.path.join(out_dir, f"{stem}_page{i + 1}.png")
        pix.save(dst)
        out.append(dst)
    doc.close()
    return out


def ingest_svg(path, out_dir, stem, dpi):
    try:
        import cairosvg
    except Exception:
        raise RuntimeError(
            "SVG support needs cairosvg (pip install cairosvg). "
            "Alternatively open the SVG and export it to PNG or PDF first."
        )
    dst = os.path.join(out_dir, f"{stem}.png")
    cairosvg.svg2png(url=path, write_to=dst, dpi=dpi)
    return [dst]


def ingest(path, out_dir="ingested", dpi=200, max_pages=None):
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    stem = _slug(path)
    if ext in RASTER_EXT:
        return ingest_raster(path, out_dir, stem)
    if ext == ".pdf":
        return ingest_pdf(path, out_dir, stem, dpi, max_pages)
    if ext == ".svg":
        return ingest_svg(path, out_dir, stem, dpi)
    if ext in (".dxf", ".dwg"):
        raise RuntimeError(
            f"{ext} is a vector CAD format. Export it to PDF or PNG from your "
            "CAD viewer (or use a converter such as ezdxf/ODA) and re-run."
        )
    raise RuntimeError(f"Unsupported file type: {ext or '(none)'}")


def main():
    ap = argparse.ArgumentParser(description="Normalize a plan file to PNG(s).")
    ap.add_argument("input", help="plan file (image, pdf, svg, ...)")
    ap.add_argument("--out", default="ingested", help="output directory")
    ap.add_argument("--dpi", type=int, default=200, help="render DPI for vector/pdf")
    ap.add_argument("--max-pages", type=int, default=None, help="cap PDF pages")
    args = ap.parse_args()
    try:
        for p in ingest(args.input, args.out, args.dpi, args.max_pages):
            print(p)
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
