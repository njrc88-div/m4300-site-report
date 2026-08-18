"""Builds a switch-to-switch topology diagram (SVG) from LLDP data.

Only switch-to-switch links are drawn - LLDP neighbors that aren't one of
the other switches in this report (APs, phones, uplinks to something
outside the report, etc.) are ignored. The diagram is a hierarchical tree.

Root selection: if any pair of switches in a connected component has more
than one physical LLDP link between them (a LAG - LLDP is per-port, so a
4-member LAG shows up as 4 separate neighbor entries between the same two
switches), that pair is treated as a *co-root* - a collapsed/dual-core
design where both switches are peers, not parent/child. Everything else in
the component is laid out by hop distance from whichever co-root it's
actually attached to. If no LAG is found anywhere in the component, it
falls back to a single root: the switch with the most inter-switch links.

Any switch with no detected link to another switch in the report is still
shown, in an "unlinked" row at the bottom, so nothing goes missing silently.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from xml.sax.saxutils import escape

NAVY = "#001E62"
TEAL = "#00BFB2"
GRAY_FILL = "#EEF0F5"
GRAY_BORDER = "#B1B3B3"
GRAY_TEXT = "#605E5C"
LINE_COLOR = "#9A9C9F"
WHITE = "#FFFFFF"

BOX_H = 46
TIER_GAP_Y = 78
MIN_BOX_W = 100
MAX_BOX_W = 170
BOX_GAP_X = 20
MARGIN_X = 30
TOP_Y = 20


def _norm_mac(mac: str | None) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()


def _norm_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _local_port(neighbor: dict) -> str:
    for key in ("ifIndex", "ifindex"):
        if neighbor.get(key) not in (None, ""):
            return str(neighbor[key])
    return "?"


def _build_edges(switches: list[dict]) -> dict[frozenset, dict]:
    """switches: [{"name", "mac", "neighbors": [...]}]. Returns edges keyed
    by frozenset({name_a, name_b}) -> {"a_ports": [...], "b_ports": [...]}.
    Multiple ports on a side means a LAG was detected between that pair."""
    mac_index = {_norm_mac(s["mac"]): s["name"] for s in switches if s.get("mac")}
    name_index = {_norm_name(s["name"]): s["name"] for s in switches}

    edges: dict[frozenset, dict] = {}
    for sw in switches:
        this_name = sw["name"]
        for n in sw.get("neighbors") or []:
            match = None
            chassis_id = n.get("chassisId")
            try:
                subtype = int(n.get("chassisIdSubtype"))
            except (TypeError, ValueError):
                subtype = None
            if subtype == 4 and chassis_id:
                match = mac_index.get(_norm_mac(chassis_id))
            if not match:
                match = name_index.get(_norm_name(n.get("remoteSysName")))
            if not match or match == this_name:
                continue

            key = frozenset({this_name, match})
            edge = edges.setdefault(key, {"a_ports": [], "b_ports": []})
            ordered = sorted(key)
            side = "a_ports" if this_name == ordered[0] else "b_ports"
            port = _local_port(n)
            if port not in edge[side]:
                edge[side].append(port)
    return edges


def _link_count(edge: dict) -> int:
    return max(len(edge["a_ports"]), len(edge["b_ports"])) or 1


def _adjacency(component: list[str], edges: dict[frozenset, dict]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for key in edges:
        a, b = tuple(key)
        if a in component and b in component:
            adjacency[a].add(b)
            adjacency[b].add(a)
    return adjacency


def _connected_components(
    nodes: list[str], edges: dict[frozenset, dict]
) -> tuple[list[list[str]], list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for key in edges:
        a, b = tuple(key)
        adjacency[a].add(b)
        adjacency[b].add(a)

    seen: set[str] = set()
    components: list[list[str]] = []
    for node in nodes:
        if node in seen or node not in adjacency:
            continue
        component, queue = [], deque([node])
        seen.add(node)
        while queue:
            cur = queue.popleft()
            component.append(cur)
            for nb in adjacency[cur]:
                if nb not in seen:
                    seen.add(nb)
                    queue.append(nb)
        components.append(component)

    unlinked = [n for n in nodes if n not in seen]
    return components, unlinked


def _find_roots(component: list[str], edges: dict[frozenset, dict]) -> list[str]:
    """A pair with a LAG between them (>1 physical link) is a collapsed-core
    pair - both become roots. Otherwise fall back to single highest-degree node."""
    best_key, best_count = None, 1
    for key, edge in edges.items():
        a, b = tuple(key)
        if a not in component or b not in component:
            continue
        count = _link_count(edge)
        if count > best_count:
            best_count, best_key = count, key
    if best_key:
        return sorted(best_key)

    adjacency = _adjacency(component, edges)
    degree = {n: len(adjacency[n]) for n in component}
    return [sorted(component, key=lambda n: (-degree[n], n))[0]]


def _layout_tiers(
    component: list[str], edges: dict[frozenset, dict], roots: list[str]
) -> tuple[list[list[str]], dict[str, str | None]]:
    """Multi-source BFS from `roots`. Returns (tiers, parent_of)."""
    adjacency = _adjacency(component, edges)

    tier_of: dict[str, int] = {r: 0 for r in roots}
    parent_of: dict[str, str | None] = {r: None for r in roots}
    queue: deque[str] = deque(roots)
    while queue:
        cur = queue.popleft()
        for nb in sorted(adjacency[cur]):
            if nb not in tier_of:
                tier_of[nb] = tier_of[cur] + 1
                parent_of[nb] = cur
                queue.append(nb)

    max_tier = max(tier_of.values())
    tiers: list[list[str]] = [[] for _ in range(max_tier + 1)]
    for name, t in tier_of.items():
        tiers[t].append(name)
    tiers[0].sort()  # deterministic order for co-roots
    return tiers, parent_of


def _box_w_for_tier(count: int, canvas_w: float) -> float:
    usable = canvas_w - 2 * MARGIN_X
    ideal = (usable - (count - 1) * BOX_GAP_X) / count if count else usable
    return max(MIN_BOX_W, min(MAX_BOX_W, ideal))


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _switch_box(x: float, y: float, w: float, name: str, model: str, is_root: bool) -> str:
    fill = NAVY if is_root else GRAY_FILL
    text_fill = WHITE if is_root else NAVY
    sub_fill = "#B9C3DC" if is_root else GRAY_TEXT
    border = TEAL if is_root else GRAY_BORDER
    name_chars = max(6, int((w - 16) / 6.5))
    return f"""
      <rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{BOX_H}" rx="5"
            fill="{fill}" stroke="{border}" stroke-width="{2 if is_root else 1}"/>
      <text x="{x + w / 2:.1f}" y="{y + 20:.1f}" text-anchor="middle"
            font-family="'Liberation Sans', Arial, sans-serif" font-size="10.5" font-weight="700"
            fill="{text_fill}">{escape(_truncate(name, name_chars))}</text>
      <text x="{x + w / 2:.1f}" y="{y + 35:.1f}" text-anchor="middle"
            font-family="'Liberation Sans', Arial, sans-serif" font-size="8.5"
            fill="{sub_fill}">{escape(_truncate(model, name_chars + 4))}</text>
    """


def build_switch_topology(switches: list[dict], canvas_w: float = 700) -> str | None:
    """switches: [{"name": str, "mac": str|None, "model": str, "neighbors": [...]}]"""
    if len(switches) < 2:
        return None

    by_name = {s["name"]: s for s in switches}
    edges = _build_edges(switches)
    if not edges:
        return None

    components, unlinked = _connected_components(list(by_name.keys()), edges)
    if not components:
        return None

    positions: dict[str, tuple[float, float, float]] = {}  # name -> (x, y, w)
    cursor_y = TOP_Y
    root_names: set[str] = set()

    for component in components:
        roots = _find_roots(component, edges)
        root_names.update(roots)
        tiers, parent_of = _layout_tiers(component, edges, roots)
        # A LAG-linked root pair needs a wider gap between them - otherwise
        # the boxes sit edge to edge and the "LAG x N" label has nowhere to
        # render (it ends up hidden behind the boxes drawn on top of it).
        root_pair_key = frozenset(roots) if len(roots) == 2 else None
        root_pair_is_lag = bool(root_pair_key and _link_count(edges.get(root_pair_key, {"a_ports": [], "b_ports": []})) > 1)

        prev_tier_x: dict[str, float] = {}
        for tier_index, tier_nodes in enumerate(tiers):
            if prev_tier_x:
                # Group children under their parent's horizontal position so
                # each co-root's subtree visually clusters on its own side.
                tier_nodes = sorted(
                    tier_nodes,
                    key=lambda n: (prev_tier_x.get(parent_of.get(n), 0), n),
                )
            gap = 90 if (tier_index == 0 and root_pair_is_lag) else BOX_GAP_X
            box_w = _box_w_for_tier(len(tier_nodes), canvas_w)
            total_w = len(tier_nodes) * box_w + (len(tier_nodes) - 1) * gap
            start_x = (canvas_w - total_w) / 2
            this_tier_x: dict[str, float] = {}
            for i, name in enumerate(tier_nodes):
                x = start_x + i * (box_w + gap)
                positions[name] = (x, cursor_y, box_w)
                this_tier_x[name] = x + box_w / 2
            prev_tier_x = this_tier_x
            cursor_y += BOX_H + TIER_GAP_Y
        cursor_y += 30  # gap between separate components

    unlinked_y = None
    row_count = 1
    if unlinked:
        box_w = _box_w_for_tier(min(len(unlinked), 5), canvas_w)
        row_count = min(len(unlinked), max(1, int((canvas_w - 2 * MARGIN_X) // (box_w + BOX_GAP_X))))
        unlinked_y = cursor_y
        row = 0
        for i, name in enumerate(unlinked):
            row, col = divmod(i, row_count)
            row_len = min(row_count, len(unlinked) - row * row_count)
            total_w_row = row_len * box_w + (row_len - 1) * BOX_GAP_X
            start_x = (canvas_w - total_w_row) / 2
            x = start_x + col * (box_w + BOX_GAP_X)
            y = unlinked_y + row * (BOX_H + 24)
            positions[name] = (x, y, box_w)
        cursor_y = unlinked_y + (row + 1) * (BOX_H + 24)

    canvas_h = cursor_y + 10

    svg_parts = [
        f'<svg width="100%" viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}" '
        'xmlns="http://www.w3.org/2000/svg" role="img">',
        "<title>Switch topology</title>",
        "<desc>Switch-to-switch links discovered via LLDP.</desc>",
    ]

    # connector lines first, so boxes sit visually on top of the line ends
    for key, edge in edges.items():
        a, b = tuple(key)
        if a not in positions or b not in positions:
            continue
        ax, ay, aw = positions[a]
        bx, by, bw = positions[b]
        x1, y1 = ax + aw / 2, ay + BOX_H
        x2, y2 = bx + bw / 2, by
        if abs(y1 - y2) < 1:  # same row (unlinked grid, or a co-root pair)
            if ax <= bx:
                x1, y1 = ax + aw, ay + BOX_H / 2
                x2, y2 = bx, by + BOX_H / 2
            else:
                x1, y1 = ax, ay + BOX_H / 2
                x2, y2 = bx + bw, by + BOX_H / 2

        count = _link_count(edge)
        is_lag = count > 1
        label = f"LAG ×{count}" if is_lag else (
            f"{(edge['a_ports'] or ['?'])[0]} ↔ {(edge['b_ports'] or ['?'])[0]}"
        )
        stroke = TEAL if is_lag else LINE_COLOR
        width = 3 if is_lag else 1.5

        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        svg_parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}"/>'
        )
        label_w = 26 + len(label) * 4.3
        svg_parts.append(
            f'<rect x="{mx - label_w / 2:.1f}" y="{my - 9:.1f}" width="{label_w:.1f}" height="13" fill="{WHITE}"/>'
            f'<text x="{mx:.1f}" y="{my + 1:.1f}" text-anchor="middle" '
            f'font-family="\'Liberation Sans\', Arial, sans-serif" font-size="7.5" '
            f'font-weight="{700 if is_lag else 400}" '
            f'fill="{TEAL if is_lag else GRAY_TEXT}">{escape(label)}</text>'
        )

    for name, (x, y, w) in positions.items():
        sw = by_name[name]
        svg_parts.append(_switch_box(x, y, w, name, sw.get("model") or "", name in root_names))

    if unlinked_y is not None:
        svg_parts.append(
            f'<text x="{MARGIN_X}" y="{unlinked_y - 10:.1f}" '
            f'font-family="\'Liberation Sans\', Arial, sans-serif" font-size="8.5" '
            f'font-style="italic" fill="{GRAY_TEXT}">'
            "No inter-switch link detected for these:</text>"
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)
