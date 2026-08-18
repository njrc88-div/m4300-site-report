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
shown, in an "unlinked" column, so nothing goes missing silently.

Layout flows left to right - root(s) at the left edge, each hop further
right - rather than top to bottom. An SVG embedded at width:100% always
fills the page's horizontal space regardless of its declared viewBox size,
so whichever axis carries "how many switches sit at this hop" ends up
fighting for room inside a fixed page width if it's mapped to X. Mapping
it to Y instead means it only competes for vertical space, which a tall
portrait page has far more of - a tier of 6 switches stacks cleanly
instead of being squeezed into narrow, label-colliding boxes.
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

BOX_W = 175
BOX_H = 46
TIER_GAP_X = 110   # horizontal space between one tier's boxes and the next
NODE_GAP_Y = 18    # vertical space between stacked boxes within a tier
COMPONENT_GAP_Y = 40
LEFT_X = 20
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


def _resolve_label_collisions(
    labels: list[tuple[float, float, float, str, bool]],
) -> list[tuple[float, float, float, str, bool]]:
    """Nudge label Y positions apart when their boxes would overlap.

    Redundant/dual-homed links cross paths, and two edges' midpoints can
    end up close together even though the lines themselves go to different
    boxes. Moving the label off the exact line midpoint is a normal
    diagramming trick and reads better than overlapping, unreadable text.
    """
    label_h = 13
    placed: list[tuple[float, float, float, str, bool]] = []
    for mx, my, w, label, is_lag in sorted(labels, key=lambda t: (t[0], t[1])):
        y = my
        for _ in range(20):
            collision = next(
                (
                    p for p in placed
                    if abs(p[0] - mx) < (w + p[2]) / 2
                    and abs(p[1] - y) < label_h + 2
                ),
                None,
            )
            if not collision:
                break
            y = collision[1] + label_h + 2
        placed.append((mx, y, w, label, is_lag))
    return placed


def build_switch_topology(switches: list[dict]) -> str | None:
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
    band_top = TOP_Y
    max_x_reached = 0.0
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

        def gap_for(tier_index: int) -> float:
            return 70 if (tier_index == 0 and root_pair_is_lag) else NODE_GAP_Y

        # First pass: each tier's column height, so shorter tiers can be
        # centered against the tallest one in this component.
        tier_heights = [
            len(t) * BOX_H + max(0, len(t) - 1) * gap_for(i) for i, t in enumerate(tiers)
        ]
        band_height = max(tier_heights) if tier_heights else BOX_H

        prev_tier_y: dict[str, float] = {}
        x = LEFT_X
        for tier_index, tier_nodes in enumerate(tiers):
            if prev_tier_y:
                # Group children under their parent's vertical position so
                # each co-root's subtree visually clusters on its own side.
                tier_nodes = sorted(
                    tier_nodes,
                    key=lambda n: (prev_tier_y.get(parent_of.get(n), 0), n),
                )
            gap = gap_for(tier_index)
            this_h = tier_heights[tier_index]
            start_y = band_top + (band_height - this_h) / 2
            this_tier_y: dict[str, float] = {}
            for i, name in enumerate(tier_nodes):
                y = start_y + i * (BOX_H + gap)
                positions[name] = (x, y, BOX_W)
                this_tier_y[name] = y + BOX_H / 2
            prev_tier_y = this_tier_y
            max_x_reached = max(max_x_reached, x + BOX_W)
            x += BOX_W + TIER_GAP_X
        band_top += band_height + COMPONENT_GAP_Y

    unlinked_label_y = None
    if unlinked:
        unlinked_label_y = band_top
        y = band_top + 16
        for name in unlinked:
            positions[name] = (LEFT_X, y, BOX_W)
            max_x_reached = max(max_x_reached, LEFT_X + BOX_W)
            y += BOX_H + NODE_GAP_Y
        band_top = y

    canvas_w = max_x_reached + 30
    canvas_h = band_top + 10

    svg_parts = [
        f'<svg width="100%" viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}" '
        'xmlns="http://www.w3.org/2000/svg" role="img">',
        "<title>Switch topology</title>",
        "<desc>Switch-to-switch links discovered via LLDP.</desc>",
    ]

    # Connector lines are drawn immediately (boxes get drawn on top of their
    # ends afterwards). Labels are collected and placed in a second pass,
    # after lines, so overlapping labels - which happen when redundant/
    # dual-homed links cross paths - can be nudged apart from each other.
    pending_labels: list[tuple[float, float, float, str, bool]] = []  # (mx, my, w, label, is_lag)

    for key, edge in edges.items():
        a, b = tuple(key)
        if a not in positions or b not in positions:
            continue
        ax, ay, aw = positions[a]
        bx, by, bw = positions[b]
        x1, y1 = ax + aw, ay + BOX_H / 2
        x2, y2 = bx, by + BOX_H / 2
        if abs(x1 - x2) < 1:  # same column (co-root pair stacked vertically)
            if ay <= by:
                x1, y1 = ax + aw / 2, ay + BOX_H
                x2, y2 = bx + bw / 2, by
            else:
                x1, y1 = ax + aw / 2, ay
                x2, y2 = bx + bw / 2, by + BOX_H

        count = _link_count(edge)
        is_lag = count > 1
        label = f"LAG ×{count}" if is_lag else (
            f"{(edge['a_ports'] or ['?'])[0]} ↔ {(edge['b_ports'] or ['?'])[0]}"
        )
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2

        if is_lag:
            # Two links bowing apart into a lens/loop shape - the standard
            # way network diagrams depict an aggregated/redundant link,
            # rather than a single line that reads as one physical cable.
            dx, dy = x2 - x1, y2 - y1
            length = max((dx ** 2 + dy ** 2) ** 0.5, 1)
            px, py = -dy / length, dx / length
            bow = 10
            c1x, c1y = mx + px * bow, my + py * bow
            c2x, c2y = mx - px * bow, my - py * bow
            svg_parts.append(
                f'<path d="M {x1:.1f} {y1:.1f} Q {c1x:.1f} {c1y:.1f} {x2:.1f} {y2:.1f}" '
                f'fill="none" stroke="{TEAL}" stroke-width="2"/>'
            )
            svg_parts.append(
                f'<path d="M {x1:.1f} {y1:.1f} Q {c2x:.1f} {c2y:.1f} {x2:.1f} {y2:.1f}" '
                f'fill="none" stroke="{TEAL}" stroke-width="2"/>'
            )
        else:
            svg_parts.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="{LINE_COLOR}" stroke-width="1.5"/>'
            )

        label_w = 26 + len(label) * 4.3
        pending_labels.append((mx, my, label_w, label, is_lag))

    for mx, my, label_w, label, is_lag in _resolve_label_collisions(pending_labels):
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

    if unlinked_label_y is not None:
        svg_parts.append(
            f'<text x="{LEFT_X}" y="{unlinked_label_y + 10:.1f}" '
            f'font-family="\'Liberation Sans\', Arial, sans-serif" font-size="8.5" '
            f'font-style="italic" fill="{GRAY_TEXT}">'
            "No inter-switch link detected for these:</text>"
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)
