'''
Per-style letter size, measured once and shipped as styles.json.

The model's 80 style vectors differ in size by nearly 4x, so --x-height-mm and the page's
line width mean nothing until the chosen style's size is known. Size depends on the style
alone -- across the full bias range it moves by under 5% -- so it is measured once per style
by tools/build_style_table.py rather than on every run.
'''

import json
from functools import lru_cache
from pathlib import Path

TABLE = Path(__file__).resolve().parent / "styles.json"


@lru_cache(maxsize=1)
def _table():
    if not TABLE.is_file():
        return {}
    data = json.loads(TABLE.read_text(encoding="utf-8"))
    return {int(k): v for k, v in data.get("styles", {}).items()}


def lookup(style):
    '''(x_height_units, char_advance_units) for a style, or None if it isn't in the table.'''
    entry = _table().get(style)
    if not entry:
        return None
    return entry["x_height"], entry["advance"]
