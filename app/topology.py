"""Builds a switch-to-switch topology diagram (SVG) from LLDP data.

Only switch-to-switch links are drawn - LLDP neighbors that aren't one of
the other switches in this report (APs, phones, uplinks to something
outside the report, etc.) are ignored. The diagram is a hierarchical tree.

LAG detection: a link is a LAG if either switch's actual LAG configuration
(`/sw_lag_cfg`, passed in as each switch's `lag_groups`) shows the local
port used in that link as a member of a port-channel - that's
authoritative, since it reflects real member-port counts regardless of
whether the switch's LLDP happens to report one entry per physical member
or one summarized entry per port-channel. When neither side's LAG data
places the port in a group (config not fetched, or the field genuinely
isn't a LAG), it falls back to the older heuristic: more than one physical
LLDP link between the same two switches implies a LAG (LLDP is per-port, so
a 4-member LAG shows up as 4 separate neighbor entries).

Root selection: if any pair of switches in a connected component has a LAG
between them (by either signal above), that pair is treated as a *co-root*
- a collapsed/dual-core design where both switches are peers, not
parent/child. Everything else in the component is laid out by hop distance
from whichever co-root it's actually attached to. If no LAG is found
anywhere in the component, it falls back to a single root: the switch with
the most inter-switch links.

Any switch with no detected link to another switch in the report is still
shown, in an "unlinked" column, so nothing goes missing silently.

The diagram reads right to left: root/core switch(es) are pinned to the
right edge, each hop toward the access layer sits further left. Internally
the tree is built left-to-right (the natural direction for BFS-from-root)
and then mirrored horizontally as a final step - simplest way to keep the
hierarchy/layout math untouched while flipping which edge the root lands
on.

Boxes are narrow and tall rather than wide and short, with their labels
rotated to read top-to-bottom - this is what lets the diagram use the
*page's* full height: box height is sized dynamically against a page-
height budget (shrinking as more switches share a tier, growing when
there are few), and the diagram is rendered at that exact pixel size
rather than auto-scaled, so it fills the page instead of sitting at
whatever size its content naturally needs.
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

BOX_W = 92
MIN_BOX_H = 130
MAX_BOX_H = 480
NODE_GAP_Y = 22
COMPONENT_GAP_X = 50
LEFT_X = 20
TOP_Y = 20

# Page content-box budget (A4 portrait, matches the rest of the report's
# unit-per-px scale). The diagram is sized to fill this, not just to fit
# whatever its content naturally needs.
TARGET_W = 660.0
TARGET_H = 740.0  # leaves room on the same page for the heading, subtitle, and legend


def _norm_mac(mac: str | None) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()


def _norm_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _local_port(neighbor: dict) -> str:
    for key in ("ifIndex", "ifindex"):
        if neighbor.get(key) not in (None, ""):
            return str(neighbor[key])
    return "?"


def _port_sort_key(port: str):
    try:
        return (0, int(port))
    except (TypeError, ValueError):
        return (1, port)


def _lag_membership(lag_groups: list[dict] | None) -> dict[str, list[str]]:
    """Maps each local physical port to the full real member-port list of
    its LAG group, from the switch's own LAG configuration. Authoritative
    over counting LLDP rows - see module docstring - and is also what lets
    the diagram label a LAG with its actual interface members instead of
    just a count, even when LLDP only reported one summarized entry for
    the whole port-channel."""
    port_to_members: dict[str, list[str]] = {}
    for g in lag_groups or []:
        members = [str(m) for m in (g.get("members") or [])]
        if not members:
            continue
        for m in members:
            port_to_members[m] = members
    return port_to_members


def _build_edges(switches: list[dict]) -> dict[frozenset, dict]:
    """switches: [{"name", "mac", "neighbors": [...], "lag_groups": [...]}].
    Returns edges keyed by frozenset({name_a, name_b}) -> {"a_ports": [...],
    "b_ports": [...], "a_lag_members": [...] | None, "b_lag_members": [...] | None}.
    a/b_ports are the physical ports LLDP actually reported; a/b_lag_members
    is the *real* full member list from LAG config when either side's
    config identifies the link as a port-channel (can be longer than
    a/b_ports if LLDP only summarized one entry for the whole LAG).
    _link_count() and the label prefer the real member list when present."""
    mac_index = {_norm_mac(s["mac"]): s["name"] for s in switches if s.get("mac")}
    name_index = {_norm_name(s["name"]): s["name"] for s in switches}

    edges: dict[frozenset, dict] = {}
    for sw in switches:
        this_name = sw["name"]
        port_to_members = _lag_membership(sw.get("lag_groups"))
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
            edge = edges.setdefault(
                key, {"a_ports": [], "b_ports": [], "a_lag_members": None, "b_lag_members": None}
            )
            ordered = sorted(key)
            side = "a_ports" if this_name == ordered[0] else "b_ports"
            members_key = "a_lag_members" if side == "a_ports" else "b_lag_members"
            port = _local_port(n)
            if port not in edge[side]:
                edge[side].append(port)
            members = port_to_members.get(port)
            if members and (edge[members_key] is None or len(members) > len(edge[members_key])):
                edge[members_key] = members
    return edges


def _link_count(edge: dict) -> int:
    real = max(len(edge.get("a_lag_members") or []), len(edge.get("b_lag_members") or []))
    if real:
        return real
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


def _mlag_pairs(switches: list[dict]) -> dict[str, str]:
    """Groups switches by (domainId, MLAG system MAC) from their MLAG
    status - two switches reporting the same enabled domain and system MAC
    are, by definition, the two peers of that MLAG, i.e. the real
    collapsed core. This is the only reliable signal once a leaf's LAG'd
    uplink to its core becomes structurally indistinguishable from the
    real core-core LAG (see _find_roots) - a secondary core with no direct
    edge connections of its own (everything hangs off the primary, it only
    backs it up) has the same degree-1-plus-one-LAG signature as a plain
    edge switch, so graph shape alone can't tell them apart."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for sw in switches:
        mlag = sw.get("mlag") or {}
        domain = mlag.get("domainId")
        mac = _norm_mac(mlag.get("mac"))
        status = str(mlag.get("adminStatus") or "").strip().lower()
        if domain is None or not mac or status not in ("enabled", "true", "1"):
            continue
        groups[(domain, mac)].append(sw["name"])
    pairs: dict[str, str] = {}
    for names in groups.values():
        if len(names) == 2:
            a, b = names
            pairs[a] = b
            pairs[b] = a
    return pairs


def _find_roots(
    component: list[str], edges: dict[frozenset, dict], mlag_pairs: dict[str, str] | None = None
) -> list[str]:
    """A pair with a LAG between them is a collapsed-core pair - both become
    roots. Otherwise fall back to single highest-degree node.

    Checks real MLAG pairing first (see _mlag_pairs) - authoritative when
    available. Link count/degree alone can't always identify the pair once
    every uplink is a LAG, not just the core-core link (real sites now
    commonly LAG every edge's uplink too): a leaf switch with a 2-member
    LAG to its core ties with the actual core-core LAG on link count, and
    if the secondary core has no direct edge connections of its own, it
    even ties on degree. Degree still helps in the common case where the
    secondary core *does* have its own edge fan-out (a leaf's LAG'd
    partner being a hub, degree > 1, means it's not just a leaf itself)."""
    for name in component:
        partner = (mlag_pairs or {}).get(name)
        if partner and partner in component:
            return sorted([name, partner])

    adjacency = _adjacency(component, edges)
    degree = {n: len(adjacency[n]) for n in component}

    if len(component) == 2:
        # Can't use degree to tell hub from leaf with only two nodes in the
        # component - a LAG between them is the best signal available, and
        # matches the common small-site case (just the two cores).
        a, b = sorted(component)
        if _link_count(edges.get(frozenset({a, b}), {"a_ports": [], "b_ports": []})) > 1:
            return [a, b]
        return [a]

    root = sorted(component, key=lambda n: (-degree[n], n))[0]
    best_partner, best_count = None, 1
    for nb in sorted(adjacency[root]):
        if degree[nb] <= 1:
            continue  # a leaf's LAG uplink doesn't make it a co-root
        count = _link_count(edges.get(frozenset({root, nb}), {"a_ports": [], "b_ports": []}))
        if count > best_count:
            best_count, best_partner = count, nb
    if best_partner:
        return sorted([root, best_partner])
    return [root]


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


def _switch_box(
    x: float, y: float, w: float, h: float, name: str, model: str, is_root: bool,
    stp_priority: int | None = None,
) -> str:
    """A narrow, tall box with text lines rotated 90 degrees so they read
    top-to-bottom - lets the box (and so the whole diagram) use the page's
    height instead of its width. A third line showing STP root bridge
    priority is added when known - mainly useful on the core switches, to
    show at a glance which one actually won root election."""
    fill = NAVY if is_root else GRAY_FILL
    text_fill = WHITE if is_root else NAVY
    sub_fill = "#B9C3DC" if is_root else GRAY_TEXT
    border = TEAL if is_root else GRAY_BORDER
    name_chars = max(6, int((h - 16) / 6.5))
    cy = y + h / 2
    stp_text = f"STP Priority {stp_priority}" if stp_priority is not None else None
    if stp_text:
        title_x, sub_x, stp_x = x + w * 0.22, x + w * 0.5, x + w * 0.8
    else:
        title_x, sub_x, stp_x = x + w * 0.36, x + w * 0.74, None
    parts = [f"""
      <rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="5"
            fill="{fill}" stroke="{border}" stroke-width="{2 if is_root else 1}"/>
      <text x="{title_x:.1f}" y="{cy:.1f}" text-anchor="middle" transform="rotate(90 {title_x:.1f} {cy:.1f})"
            font-family="'Liberation Sans', Arial, sans-serif" font-size="11" font-weight="700"
            fill="{text_fill}">{escape(_truncate(name, name_chars))}</text>
      <text x="{sub_x:.1f}" y="{cy:.1f}" text-anchor="middle" transform="rotate(90 {sub_x:.1f} {cy:.1f})"
            font-family="'Liberation Sans', Arial, sans-serif" font-size="8.5"
            fill="{sub_fill}">{escape(_truncate(model, name_chars + 4))}</text>
    """]
    if stp_text and stp_x is not None:
        parts.append(f"""
      <text x="{stp_x:.1f}" y="{cy:.1f}" text-anchor="middle" transform="rotate(90 {stp_x:.1f} {cy:.1f})"
            font-family="'Liberation Sans', Arial, sans-serif" font-size="7.5"
            fill="{sub_fill}">{escape(_truncate(stp_text, name_chars + 6))}</text>
        """)
    return "".join(parts)


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
    """switches: [{"name": str, "mac": str|None, "model": str,
    "neighbors": [...], "lag_groups": [...], "mlag": {...} | None,
    "stp_priority": int | None}]"""
    if len(switches) < 2:
        return None

    by_name = {s["name"]: s for s in switches}
    edges = _build_edges(switches)
    if not edges:
        return None

    components, unlinked = _connected_components(list(by_name.keys()), edges)
    if not components:
        return None

    mlag_pairs = _mlag_pairs(switches)

    # First pass: figure out every component's tiers/roots up front, so box
    # height can be sized once from the single most-crowded tier anywhere
    # in the diagram - keeps every box the same size instead of each
    # component/tier picking its own.
    component_layouts = []
    max_breadth = 1
    max_tier_count = 1
    for component in components:
        roots = _find_roots(component, edges, mlag_pairs)
        tiers, parent_of = _layout_tiers(component, edges, roots)
        component_layouts.append((component, roots, tiers, parent_of))
        max_breadth = max(max_breadth, max((len(t) for t in tiers), default=1))
        max_tier_count = max(max_tier_count, len(tiers))
    if unlinked:
        max_breadth = max(max_breadth, len(unlinked))

    box_h = max(MIN_BOX_H, min(MAX_BOX_H, (TARGET_H - (max_breadth - 1) * NODE_GAP_Y) / max_breadth))
    tier_gap_x = max(60.0, (TARGET_W - LEFT_X - max_tier_count * BOX_W - 30) / max(1, max_tier_count - 1)) \
        if max_tier_count > 1 else 0.0

    positions: dict[str, tuple[float, float, float, float]] = {}  # name -> (x, y, w, h)
    band_left = LEFT_X
    max_x_reached = 0.0
    max_y_reached = 0.0
    root_names: set[str] = set()

    for component, roots, tiers, parent_of in component_layouts:
        root_names.update(roots)
        root_pair_key = frozenset(roots) if len(roots) == 2 else None
        root_pair_is_lag = bool(root_pair_key and _link_count(edges.get(root_pair_key, {"a_ports": [], "b_ports": []})) > 1)

        def gap_for(tier_index: int) -> float:
            return max(90.0, box_h * 0.2) if (tier_index == 0 and root_pair_is_lag) else NODE_GAP_Y

        tier_widths = [
            len(t) * box_h + max(0, len(t) - 1) * gap_for(i) for i, t in enumerate(tiers)
        ]
        band_height = max(tier_widths) if tier_widths else box_h

        prev_tier_y: dict[str, float] = {}
        x = band_left
        for tier_index, tier_nodes in enumerate(tiers):
            if prev_tier_y:
                tier_nodes = sorted(
                    tier_nodes,
                    key=lambda n: (prev_tier_y.get(parent_of.get(n), 0), n),
                )
            gap = gap_for(tier_index)
            this_w = tier_widths[tier_index]
            start_y = TOP_Y + (band_height - this_w) / 2
            this_tier_y: dict[str, float] = {}
            for i, name in enumerate(tier_nodes):
                y = start_y + i * (box_h + gap)
                positions[name] = (x, y, BOX_W, box_h)
                this_tier_y[name] = y + box_h / 2
            prev_tier_y = this_tier_y
            max_y_reached = max(max_y_reached, TOP_Y + band_height)
            max_x_reached = max(max_x_reached, x + BOX_W)
            x += BOX_W + tier_gap_x
        band_left = x + COMPONENT_GAP_X

    unlinked_label_y = None
    if unlinked:
        unlinked_label_y = TOP_Y
        y = TOP_Y + 20
        for name in unlinked:
            positions[name] = (band_left, y, BOX_W, box_h)
            y += box_h + NODE_GAP_Y
        max_y_reached = max(max_y_reached, y)
        max_x_reached = max(max_x_reached, band_left + BOX_W)

    canvas_w = max_x_reached + 30
    canvas_h = max_y_reached + 10

    # Flip horizontally so the root(s) land on the right edge and the
    # access layer on the left, instead of rebuilding the tier/BFS math
    # mirrored - a straight x -> canvas_w - x - box_width remap of every
    # already-computed position achieves the same result.
    positions = {name: (canvas_w - x - w, y, w, h) for name, (x, y, w, h) in positions.items()}

    svg_parts = [
        f'<svg width="{canvas_w:.0f}" height="{canvas_h:.0f}" viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}" '
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
        ax, ay, aw, ah = positions[a]
        bx, by, bw, bh = positions[b]
        if abs(ax - bx) < 1:
            # Same column - a co-root pair stacked vertically (after the
            # horizontal mirror, both roots share one x). Connect top/bottom
            # edges straight down the middle, not left/right edges (which
            # would be a full box-width apart despite ax == bx, producing a
            # diagonal line instead of the intended straight vertical one).
            if ay <= by:
                x1, y1 = ax + aw / 2, ay + ah
                x2, y2 = bx + bw / 2, by
            else:
                x1, y1 = ax + aw / 2, ay
                x2, y2 = bx + bw / 2, by + bh
        else:
            x1, y1 = ax + aw, ay + ah / 2
            x2, y2 = bx, by + bh / 2

        count = _link_count(edge)
        is_lag = count > 1
        if is_lag:
            a_members = sorted(edge.get("a_lag_members") or edge["a_ports"], key=_port_sort_key)
            b_members = sorted(edge.get("b_lag_members") or edge["b_ports"], key=_port_sort_key)
            label = f"LAG {','.join(a_members)} ↔ {','.join(b_members)}"
        else:
            label = f"{(edge['a_ports'] or ['?'])[0]} ↔ {(edge['b_ports'] or ['?'])[0]}"
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

    for name, (x, y, w, h) in positions.items():
        sw = by_name[name]
        svg_parts.append(_switch_box(
            x, y, w, h, name, sw.get("model") or "", name in root_names, sw.get("stp_priority")
        ))

    if unlinked_label_y is not None:
        ux, uy, uw, uh = positions[unlinked[0]]
        svg_parts.append(
            f'<text x="{ux:.1f}" y="{uy - 8:.1f}" '
            f'font-family="\'Liberation Sans\', Arial, sans-serif" font-size="8.5" '
            f'font-style="italic" fill="{GRAY_TEXT}">'
            "No inter-switch link detected for these:</text>"
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)
