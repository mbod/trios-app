"""
Maze grids for the maze task

Utility functions for:
   - reading maze specifications (`parse_grid`)
   - checking
   - finding routes

"""

import re
from collections import deque

SYMBOLS = {"s", "e", ".", "x"}
CELL = re.compile(r"r(\d+)c(\d+)$")
DIRS = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}


def parse_grid(text: str) -> list[list[str]]:
    """
    Parse a text representation of a maze into a list of rows
    - Valid cell contents: {x . s e}
    - Must be exactly ONE `s` and `e` cell

    e.g. 5x5 maze 

      s . . . x
      x . x . x
      . . x . x
      . x x x x
      . . . . e

    returns

    [
      ['s', '.', '.', '.', 'x'],
      ['x', '.', 'x', '.', 'x'],
      ['.', '.', 'x', '.', 'x'],
      ['.', 'x', 'x', 'x', 'x'],
      ['.', '.', '.', '.', 'e']
    ]

    """
    rows = [line.split() for line in (text or "").strip().splitlines()]
    if not rows or any(len(r) != len(rows[0]) for r in rows):
        raise ValueError("the maze grid must be a rectangle")
    flat = [s for r in rows for s in r]
    if not set(flat) <= SYMBOLS:
        raise ValueError(f"maze symbols must be {sorted(SYMBOLS)}")
    if flat.count("s") != 1 or flat.count("e") != 1:
        raise ValueError("the maze needs exactly one start (s) and one end (e)")
    return rows


def name(r: int, c: int) -> str:
    """
    Translate row and column INDICES into reference string
    e.g.
    name(0,4) -> "r1c5"
    """
    return f"r{r + 1}c{c + 1}"


def cell(text: str) -> tuple[int, int]:
    """
    Translate cell string reference into row and col indicies
    e.g.
    "r1c5" -> (0,4)
    """
    m = CELL.match(str(text).strip().lower())
    if not m:
        raise ValueError(f"'{text}' is not a cell like r3c2")
    return int(m[1]) - 1, int(m[2]) - 1


def find(grid, symbol) -> tuple[int, int]:
    """
    Find the first cell containing `symbol` in `grid`
    - traverse `grid` by cells in rows
    - return row and cell indicies for first match
    """
    for r, row in enumerate(grid):
        for c, s in enumerate(row):
            if s == symbol:
                return r, c


def inside(grid, r, c) -> bool:
    """
    Check a row-cell index pair are within `grid`
    """
    return 0 <= r < len(grid) and 0 <= c < len(grid[0])


def shortest_path(grid) -> list[tuple[int, int]] | None:
    start, end = find(grid, "s"), find(grid, "e")
    prev = {start: None}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        if here == end:
            path = []
            while here is not None:
                path.append(here)
                here = prev[here]
            return path[::-1]
        for dr, dc in DIRS.values():
            nxt = (here[0] + dr, here[1] + dc)
            if nxt not in prev and inside(grid, *nxt) and grid[nxt[0]][nxt[1]] != "x":
                prev[nxt] = here
                queue.append(nxt)
    return None


def count_routes(grid, limit: int = 20) -> int:
    """Simple routes from start to end; stops counting at limit."""
    start, end = find(grid, "s"), find(grid, "e")
    count = 0

    def walk(here, seen):
        nonlocal count
        if count >= limit:
            return
        if here == end:
            count += 1
            return
        for dr, dc in DIRS.values():
            nxt = (here[0] + dr, here[1] + dc)
            if nxt not in seen and inside(grid, *nxt) and grid[nxt[0]][nxt[1]] != "x":
                walk(nxt, seen | {nxt})

    walk(start, {start})
    return count


def check(spec: dict, participants: list[str]) -> dict:
    """Validate one maze. Returns its shortest length, route count and how the
    cells on the shortest route are split between participants."""
    grid = parse_grid(spec.get("grid", ""))
    path = shortest_path(grid)
    if not path:
        raise ValueError("no route from start to end")
    known = spec.get("known") or {}
    cells = {}
    for p in participants:
        cells[p] = {cell(t) for t in known.get(p, [])}
        if (bad := [name(*c) for c in cells[p] if not inside(grid, *c)]):
            raise ValueError(f"{p}: cells outside the grid: {bad}")
    route = path[1:-1]
    return {"shortest": len(path) - 1, "routes": count_routes(grid),
            "route_split": {p: sum(c in cells[p] for c in route) for p in participants}}
