#!/usr/bin/env python3
"""Keep contribution squares visible while Platane/snk's snake keeps moving."""

from pathlib import Path
import sys
import xml.etree.ElementTree as ET


SVG = "http://www.w3.org/2000/svg"
KEEP_GRID = "\n.c{animation:none!important}"
ET.register_namespace("", SVG)


def preserve_grid(path):
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    style = root.find(f"{{{SVG}}}style")
    cells = [element for element in root if "c" in element.get("class", "").split()]
    snake = [element for element in root if "s" in element.get("class", "").split()]
    if style is None or not style.text or not cells or not snake:
        raise ValueError(f"Unexpected snake SVG structure: {path}")
    if KEEP_GRID not in style.text:
        # Initial cell colors stay intact; snake keyframes remain untouched.
        style.text += KEEP_GRID
    for element in list(root):
        # The bottom stack counts eaten squares, so it has no role in this view.
        if "u" in element.get("class", "").split():
            root.remove(element)
    path.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8", newline="\n")
    print(f"Preserved {len(cells)} contribution squares in {path.name}")


if __name__ == "__main__":
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist")
    files = sorted(directory.glob("github-contribution-grid-snake*.svg"))
    if not files:
        raise SystemExit(f"No generated snake SVGs in {directory}")
    for path in files:
        preserve_grid(path)
