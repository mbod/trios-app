"""Check common-lines stimulus sets: which lines are actually shared by A, B and C.

usage: python check_lines.py tasks/common_lines/stimuli/animal_tangrams

For every set folder holding A.svg, B.svg and C.svg, each cell's figure is
identified by its shape data (not by its position), and the 8 lines (3 rows,
3 columns, 2 diagonals) are checked. A line counts as shared when all three
images have the same figures in the same order along it.
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SVG = "{http://www.w3.org/2000/svg}"
LINES = {
    "row1": (0, 1, 2), "row2": (3, 4, 5), "row3": (6, 7, 8),
    "col1": (0, 3, 6), "col2": (1, 4, 7), "col3": (2, 5, 8),
    "diag_tlbr": (0, 4, 8), "diag_bltr": (6, 4, 2),
}


def cells(path: Path) -> list[str]:
    """The 9 figures of one image, in reading order, each as a shape signature."""
    root = ET.parse(path).getroot()
    found = []
    # tangram style: one <g transform="translate(x y)"> per cell
    for g in root.iter(f"{SVG}g"):
        m = re.search(r"translate\(\s*([\d.-]+)[ ,]+([\d.-]+)", g.get("transform", ""))
        shapes = [el.get("d") or el.get("points") for el in g
                  if el.tag in (f"{SVG}path", f"{SVG}polygon")]
        if m and shapes:
            found.append((float(m[2]), float(m[1]), "|".join(sorted(shapes))))
    # text style: one <text x y> per cell (letters, emoji)
    if not found:
        for t in root.iter(f"{SVG}text"):
            label = "".join(t.itertext()).strip()
            if label:
                found.append((float(t.get("y", 0)), float(t.get("x", 0)), label))
    if len(found) != 9:
        raise ValueError(f"{path}: expected 9 cells, found {len(found)}")
    # sort into rows by y, then by x within each row
    found.sort()
    rows = [sorted(found[i:i + 3], key=lambda c: c[1]) for i in (0, 3, 6)]
    return [c[2] for row in rows for c in row]


def shared_lines(folder: Path) -> list[str]:
    grids = [cells(folder / f"{p}.svg") for p in "ABC"]
    for g in grids:
        if len(set(g)) != 9:
            raise ValueError(f"{folder}: an image has a repeated figure")
    return [name for name, idx in LINES.items()
            if len({tuple(g[i] for i in idx) for g in grids}) == 1]


if __name__ == "__main__":
    root = Path(sys.argv[1])
    for folder in sorted(p for p in root.iterdir() if (p / "A.svg").exists()):
        try:
            lines = shared_lines(folder)
            flag = "" if len(lines) == 2 else f"   <-- {len(lines)} shared lines"
            print(f"{folder.name}: {', '.join(lines) or 'none'}{flag}")
        except Exception as e:
            print(f"{folder.name}: ERROR {e}")
