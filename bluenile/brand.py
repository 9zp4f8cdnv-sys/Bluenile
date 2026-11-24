"""Blue Nile brand: the wheel emblem and colours, from the shop's logo system
(full-colour emblem: navy wheel, five white spokes, sun rising over the river).

`emblem_svg()` returns inline SVG; each call gets its own ids so several
emblems can sit on one page.
"""

from __future__ import annotations

import itertools

NILE_NAVY = "#0B2E6B"
SUN_GOLD = "#FFB81C"
ROAD_BLACK = "#0D0F14"
RIVER_SKY = "#38BDF8"
CLOUD_WHITE = "#F5F7FA"
RIVER_DEEP = "#1D8FD1"
RIM_SILVER = "#D9DFE8"

_ids = itertools.count(1)


def emblem_svg(size: int | str = 40, label: str = "Blue Nile emblem") -> str:
    n = next(_ids)
    spoke, clip = f"bnSpoke{n}", f"bnClip{n}"
    spokes = "".join(
        f'<use href="#{spoke}" fill="{CLOUD_WHITE}"{f" transform=\"rotate({a} 210 210)\"" if a else ""}/>'
        for a in (0, 72, 144, 216, 288)
    )
    a11y = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true" focusable="false"'
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 420 420" {a11y}>'
        f'<defs><path id="{spoke}" d="M 192 162 L 180 76 Q 210 60 240 76 L 228 162 Z"/>'
        f'<clipPath id="{clip}"><circle cx="210" cy="210" r="44"/></clipPath></defs>'
        f'<circle cx="210" cy="210" r="208" fill="{ROAD_BLACK}"/>'
        f'<circle cx="210" cy="210" r="194" fill="none" stroke="{SUN_GOLD}" stroke-width="8"/>'
        f'<circle cx="210" cy="210" r="160" fill="{RIM_SILVER}"/>'
        f'<circle cx="210" cy="210" r="146" fill="{NILE_NAVY}"/>'
        f"{spokes}"
        f'<circle cx="210" cy="210" r="56" fill="{NILE_NAVY}" stroke="{SUN_GOLD}" stroke-width="9"/>'
        f'<g clip-path="url(#{clip})"><circle cx="210" cy="198" r="17" fill="{SUN_GOLD}"/>'
        f'<path d="M 160 212 C 178 202, 192 224, 210 212 S 242 202, 260 212 L 260 260 L 160 260 Z" fill="{RIVER_SKY}"/>'
        f'<path d="M 160 230 C 178 220, 192 242, 210 230 S 242 220, 260 230 L 260 260 L 160 260 Z" fill="{RIVER_DEEP}"/>'
        f"</g></svg>"
    )


def favicon_data_uri() -> str:
    from urllib.parse import quote
    return "data:image/svg+xml," + quote(emblem_svg(64, label=""))
