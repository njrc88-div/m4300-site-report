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
on. The legend is a small HTML/CSS block below the diagram (see
report.html), not part of this SVG - it's compact enough that giving it
a full reserved column here just shrank and off-centered the actual
diagram to make room for a column mostly empty space.

Boxes are narrow and tall rather than wide and short, with their labels
rotated to read top-to-bottom - this is what lets the diagram use the
*page's* full height: box height is sized dynamically against a page-
height budget (shrinking as more switches share a tier, growing when
there are few), and the diagram is rendered at that exact pixel size
rather than auto-scaled, so it fills the page instead of sitting at
whatever size its content naturally needs.

Each LAG gets its own color from a fixed palette (cycled in a stable
order) so it can be traced by eye through a diagram where several LAGs
cross paths - a single shared color for every LAG made that impossible.
A LAG is drawn as N genuinely parallel-ish straight lines, one per
physical member (real member ports from LAG config, not just a count),
each with its own single-interface label positioned beside that specific
line - rather than one label combining both members together, which
can't tell you which number belongs to which physical cable. All
interface-number labels read top-to-bottom, rotated 90 degrees just like
the switch boxes' own text - one consistent orientation across the whole
diagram rather than horizontal numbers next to vertical box labels.

Every individual physical line touching a box - not just every LAG as a
whole - gets its own genuinely independent, evenly-spaced slot along that
box's full edge, ordered by the other end's vertical position (so a
LAG's members still land adjacent to each other) then by member index.
A box with four edges' worth of 2-member LAGs plus a core-core LAG has
nine individual lines to place; giving each one a real slot across the
box's whole height - the same mechanism that already kept different
switches' connections apart - is what guarantees none of them (or their
labels) can collide, rather than sharing one slot per LAG and hoping a
small local offset keeps two members apart without drifting into
whichever box happens to be nearby. Labels sit a fixed pixel distance
from the box they belong to, not a percentage of the line's length - a
percentage-based offset put a label farther from its switch the
longer/more diagonal the line was, scattering labels into the busy
crossing area in the middle of the diagram rather than next to the
switch they describe.
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

# One color per LAG, cycled in a stable order - lets a given aggregated
# link be traced by eye through a diagram where several LAGs cross paths.
# Chosen for contrast against the white page and against each other.
LAG_PALETTE = [
    "#00BFB2", "#7B2FF7", "#E8590C", "#1E88E5", "#D81B60",
    "#43A047", "#FB8C00", "#8E24AA", "#00838F", "#C0CA33",
]
LINE_COLOR = "#9A9C9F"
WHITE = "#FFFFFF"

BOX_W = 92
MIN_BOX_H = 130
MAX_BOX_H = 480
NODE_GAP_Y = 16
COMPONENT_GAP_X = 50
LEFT_X = 20
TOP_Y = 20

# Page content-box budget (A4 portrait, matches the rest of the report's
# unit-per-px scale). The diagram is sized to fill this, not just to fit
# whatever its content naturally needs.
#
# The legend (report.html's plain HTML/CSS `.legend` block, below the
# diagram) is horizontal text, not rotated to match the diagram's own
# rotated box labels - that was tried, twice, and both versions ran into
# the same hard wall: a full caption sentence rotated at a legible size
# needs far more vertical room (60-90+px per short label, 250px+ for one
# full sentence) than fits below a *full-size* diagram on a single A4
# page - there just isn't a TARGET_H value that leaves enough room for
# both a full-size diagram and a rotated multi-line legend on the same
# page, only a choice of which one to shrink to make room for the other.
# Horizontal text needs one line's worth of height regardless of how long
# the sentence is, so TARGET_H barely has to give up anything to leave
# room for it - which is what actually lets the diagram stay full-size
# *and* the legend keep its full caption text, both on the same page.
TARGET_W = 660.0
TARGET_H = 700.0


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
    x: float, y: float, w: float, h: float, name: str, is_root: bool,
    model: str = "", stp_priority: int | None = None,
) -> str:
    """A narrow, tall box with hostname / model / (root switches only) STP
    priority as three separate columns side by side across the box's
    width, each rotated 90 degrees to read top-to-bottom - rather than
    stacked one above the next along the box's height.

    That distinction matters because of how rotated text is actually read:
    nobody tilts their head to skim rotated-in-place rows on a printed
    page, they turn the page (or the diagram) itself. In this diagram's
    orientation that means hostname sits in the *rightmost* column, model
    to its left, and STP priority to the left of that - so that turning
    the page 90 degrees the way the rotation is meant to be read puts
    hostname on top, model underneath it, and STP underneath model, left
    to right in box-space becoming top to bottom once actually read.

    Columns are independent of each other, so - unlike an earlier shared-
    budget version - each one gets the box's *full* height for its own
    text rather than a fraction of it split between hostname and its
    sub-headings."""
    fill = NAVY if is_root else GRAY_FILL
    text_fill = WHITE if is_root else NAVY
    sub_fill = "#B9C3DC" if is_root else GRAY_TEXT
    border = TEAL if is_root else GRAY_BORDER
    stp_text = f"STP Priority: {stp_priority}" if stp_priority is not None else None

    def _fit(text: str, char_px: float) -> str:
        chars = max(4, int((h - 16) / char_px))
        return _truncate(text, chars)

    lines = []
    if stp_text:
        lines.append((_fit(stp_text, 5.2), 7.5, "400", sub_fill))
    if model:
        lines.append((_fit(model, 5.2), 7.5, "400", sub_fill))
    lines.append((_fit(name, 6.3), 11, "700", text_fill))

    col_w = w / len(lines)
    cy = y + h / 2
    parts = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="5" '
        f'fill="{fill}" stroke="{border}" stroke-width="{2 if is_root else 1}"/>',
    ]
    for i, (text, font_size, weight, fill_color) in enumerate(lines):
        col_cx = x + col_w * (i + 0.5)
        parts.append(
            f'<text x="{col_cx:.1f}" y="{cy:.1f}" text-anchor="middle" transform="rotate(90 {col_cx:.1f} {cy:.1f})" '
            f'font-family="\'Liberation Sans\', Arial, sans-serif" font-size="{font_size}" font-weight="{weight}" '
            f'fill="{fill_color}">{escape(text)}</text>'
        )
    return "".join(parts)


def _resolve_label_collisions(
    labels: list[tuple[float, float, float, str, str, bool, float]],
) -> list[tuple[float, float, float, str, str, bool, float]]:
    """Nudge label X positions apart when their (rotated, vertical-reading)
    text would overlap.

    Every label on the diagram reads top-to-bottom now, matching the
    switch boxes' own text - so unlike ordinary horizontal labels, the
    space a label actually needs runs vertically (`extent`, roughly its
    character count) while its footprint sideways is just the fixed
    thickness of one line of text. Collision therefore checks vertical
    overlap and resolves it by nudging horizontally - the transpose of the
    old horizontal-label version. With one label per physical LAG member
    landing right where several lines fan out near a switch, collisions
    here are routine, not an edge case.

    Each label carries a `sign` (+1/-1) fixing which horizontal direction
    is actually safe for it - away from whichever switch box it's
    labeling. Nudging unconditionally in +x (as an earlier version did)
    could push a label that started safely clear of its box right back
    into it once a second label needed to slot in beside it; nudging along
    the label's own safe direction can't reintroduce that.
    """
    label_w = 13
    placed: list[tuple[float, float, float, str, str, bool, float]] = []
    for mx, my, extent, label, color, bold, sign in sorted(labels, key=lambda t: (t[1], t[0])):
        x = mx
        for _ in range(20):
            collision = next(
                (
                    p for p in placed
                    if abs(p[1] - my) < (extent + p[2]) / 2
                    and abs(p[0] - x) < label_w + 2
                ),
                None,
            )
            if not collision:
                break
            x = x + sign * (label_w + 2)
        placed.append((x, my, extent, label, color, bold, sign))
    return placed


def _push_clear_of_box(
    base: tuple[float, float], dx: float, dy: float, bx: float, by: float, bw: float, bh: float
) -> tuple[float, float]:
    """Apply push (dx, dy) to `base`, or its exact opposite - whichever
    ends up farther from the given box.

    The push direction is perpendicular to a LAG's own line, which is
    correct for spreading two member labels apart from each other - but
    that perpendicular flips orientation relative to "away from the box"
    depending on whether the line's other end sits above or below this
    box's anchor point (a fanned-out set of connections includes lines
    sloping both ways), so a fixed sign convention on which member pushes
    which way isn't reliably "outward". Picking whichever candidate is
    farther from the box (by actual distance to its boundary, zero if
    inside it) sidesteps having to reason about which fixed margin is
    "safe" - the anchor point always starts close to the box, so a small
    margin can flag both candidates as unsafe and a large one can flag
    neither; comparing them against each other has no such tuning problem.
    """
    def dist_to_box(px: float, py: float) -> float:
        cx = max(bx, min(px, bx + bw))
        cy = max(by, min(py, by + bh))
        return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5

    opt1 = (base[0] + dx, base[1] + dy)
    opt2 = (base[0] - dx, base[1] - dy)
    return opt1 if dist_to_box(*opt1) >= dist_to_box(*opt2) else opt2


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

    # Where a line attaches to a box: every *individual physical line* gets
    # its own evenly-spaced slot along the box edge - not just one slot per
    # LAG (with its members then squeezed into a narrow sub-budget via an
    # offset/push that has to avoid both its sibling and the box itself).
    # A box with 4 edges' worth of 2-member LAGs plus a core-core LAG has 9
    # individual lines to place, and giving each one a real, independent
    # slot across the box's *full* height - the same mechanism already used
    # to keep different switches' connections apart - is what actually
    # guarantees no two of them (or their labels) can collide, rather than
    # hoping a small local nudge is enough. Ordered by (other end's
    # position, member index) so a LAG's members still land adjacent to
    # each other, not scattered.
    attach_order: dict[str, list[tuple[frozenset, int]]] = defaultdict(list)
    for key, edge in edges.items():
        a, b = sorted(key)
        if a not in positions or b not in positions:
            continue
        n = _link_count(edge) if _link_count(edge) > 1 else 1
        for i in range(n):
            attach_order[a].append((key, i))
            attach_order[b].append((key, i))

    def _other_y(name: str, key: frozenset) -> float:
        other = next(iter(key - {name}))
        if other not in positions:
            return 0.0
        oy, oh = positions[other][1], positions[other][3]
        return oy + oh / 2

    for name, lst in attach_order.items():
        lst.sort(key=lambda item: (_other_y(name, item[0]), item[1]))

    def _anchor_y(name: str, key: frozenset, member: int, y: float, h: float) -> float:
        lst = attach_order[name]
        n = len(lst)
        if n <= 1:
            return y + h / 2
        idx = lst.index((key, member))
        margin = h * 0.08
        return y + margin + (h - 2 * margin) * idx / (n - 1)

    # Each LAG gets its own color (cycling a fixed palette, assigned in a
    # stable order) so a given aggregated link can be traced by eye through
    # a diagram where several LAGs cross paths - a single shared teal for
    # every LAG made that impossible. Non-LAG single-physical-link edges
    # stay neutral gray; they're the exception, not what needs disambiguating.
    lag_keys = sorted(
        (key for key, edge in edges.items() if _link_count(edge) > 1),
        key=lambda k: tuple(sorted(k)),
    )
    lag_color = {key: LAG_PALETTE[i % len(LAG_PALETTE)] for i, key in enumerate(lag_keys)}

    # Connector lines are drawn immediately (boxes get drawn on top of their
    # ends afterwards). Labels are collected and placed in a second pass,
    # after lines, so overlapping labels - which happen when several lines
    # attach near the same point - can be nudged apart from each other.
    pending_labels: list[tuple[float, float, float, str, str, bool, float]] = []  # (x, y, w, label, color, bold, sign)

    def _label_point(x1, y1, x2, y2, dist):
        """Point `dist` pixels from (x1,y1) toward (x2,y2) - a FIXED pixel
        offset, not a fraction of the line's length. A percentage-based
        offset put labels farther from the switch the longer/more diagonal
        the line was, scattering them into the busy crossing area in the
        middle of the diagram instead of sitting next to the box they
        belong to."""
        dx, dy = x2 - x1, y2 - y1
        length = max((dx ** 2 + dy ** 2) ** 0.5, 1)
        t = min(dist / length, 0.45)
        return x1 + dx * t, y1 + dy * t

    for key, edge in edges.items():
        # Must match _build_edges' `sorted(key)` convention exactly - this
        # is what determines which switch's real port list ends up in
        # edge["a_ports"] vs ["b_ports"]. tuple(a_frozenset) does NOT
        # reliably preserve sorted order (hash-based iteration), so using
        # it here silently swapped which end got which label whenever the
        # hash order disagreed with sorted order - the real cause of a LAG
        # occasionally showing blank/wrong-side interface numbers.
        a, b = sorted(key)
        if a not in positions or b not in positions:
            continue
        ax, ay, aw, ah = positions[a]
        bx, by, bw, bh = positions[b]
        count = _link_count(edge)
        is_lag = count > 1
        color = lag_color.get(key, LINE_COLOR)

        if abs(ax - bx) < 1:
            # Same column - a co-root pair stacked vertically (after the
            # horizontal mirror, both roots share one x). This is always
            # the core-core link in practice (rare to have a second
            # same-column pair), so it doesn't need the full per-member
            # slot treatment below - connect top/bottom edges straight down
            # the middle (not left/right edges, which would be a full
            # box-width apart despite ax == bx, producing a diagonal line
            # instead of the intended straight vertical one), and spread
            # any LAG members with a small perpendicular offset.
            if ay <= by:
                x1, y1 = ax + aw / 2, ay + ah
                x2, y2 = bx + bw / 2, by
            else:
                x1, y1 = ax + aw / 2, ay
                x2, y2 = bx + bw / 2, by + bh

            if not is_lag:
                svg_parts.append(
                    f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                    f'stroke="{color}" stroke-width="1.5"/>'
                )
                a_txt = (edge["a_ports"] or ["?"])[0]
                b_txt = (edge["b_ports"] or ["?"])[0]
                ax_pt = _label_point(x1, y1, x2, y2, 16)
                bx_pt = _label_point(x2, y2, x1, y1, 16)
                a_sign = -1.0 if ax_pt[0] < ax + aw / 2 else 1.0
                b_sign = -1.0 if bx_pt[0] < bx + bw / 2 else 1.0
                pending_labels.append((ax_pt[0], ax_pt[1], 10 + len(a_txt) * 5.5, a_txt, color, False, a_sign))
                pending_labels.append((bx_pt[0], bx_pt[1], 10 + len(b_txt) * 5.5, b_txt, color, False, b_sign))
                continue

            a_members = sorted(edge.get("a_lag_members") or edge["a_ports"], key=_port_sort_key)
            b_members = sorted(edge.get("b_lag_members") or edge["b_ports"], key=_port_sort_key)
            n = max(len(a_members), len(b_members), 1)
            dx, dy = x2 - x1, y2 - y1
            length = max((dx ** 2 + dy ** 2) ** 0.5, 1)
            px, py = -dy / length, dx / length
            spacing = 8.0
            for i in range(n):
                off = (i - (n - 1) / 2) * spacing
                lx1, ly1 = x1 + px * off, y1 + py * off
                lx2, ly2 = x2 + px * off, y2 + py * off
                svg_parts.append(
                    f'<line x1="{lx1:.1f}" y1="{ly1:.1f}" x2="{lx2:.1f}" y2="{ly2:.1f}" '
                    f'stroke="{color}" stroke-width="1.6"/>'
                )
                a_txt = a_members[i] if i < len(a_members) else "?"
                b_txt = b_members[i] if i < len(b_members) else "?"
                a_base = _label_point(lx1, ly1, lx2, ly2, 16)
                b_base = _label_point(lx2, ly2, lx1, ly1, 16)
                push = off * 4.0
                a_pt = _push_clear_of_box(a_base, px * push, py * push, ax, ay, aw, ah)
                b_pt = _push_clear_of_box(b_base, px * push, py * push, bx, by, bw, bh)
                a_sign = -1.0 if a_pt[0] < ax + aw / 2 else 1.0
                b_sign = -1.0 if b_pt[0] < bx + bw / 2 else 1.0
                pending_labels.append((a_pt[0], a_pt[1], 10 + len(a_txt) * 5.5, a_txt, color, True, a_sign))
                pending_labels.append((b_pt[0], b_pt[1], 10 + len(b_txt) * 5.5, b_txt, color, True, b_sign))
            continue

        # The common case: box A and box B sit side by side (different
        # tiers). Every individual physical line - not just every LAG -
        # gets its own real anchor slot on each box's edge, computed by
        # _anchor_y from the FULL set of lines that box carries (built
        # above in attach_order). That's what actually guarantees no two
        # lines/labels at this box can collide: each one has a genuinely
        # independent, evenly-spaced position across the box's whole
        # height, rather than sharing one slot per LAG and then relying on
        # a local nudge to separate members within it (which could point
        # either toward or away from the box depending on this specific
        # line's slope, and get overridden again by collision resolution
        # against unrelated neighboring labels).
        if ax <= bx:
            ax_edge, bx_edge = ax + aw, bx
        else:
            ax_edge, bx_edge = ax, bx + bw

        if not is_lag:
            y1 = _anchor_y(a, key, 0, ay, ah)
            y2 = _anchor_y(b, key, 0, by, bh)
            x1, x2 = ax_edge, bx_edge
            svg_parts.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="{color}" stroke-width="1.5"/>'
            )
            a_txt = (edge["a_ports"] or ["?"])[0]
            b_txt = (edge["b_ports"] or ["?"])[0]
            ax_pt = _label_point(x1, y1, x2, y2, 16)
            bx_pt = _label_point(x2, y2, x1, y1, 16)
            a_sign = -1.0 if ax_pt[0] < ax + aw / 2 else 1.0
            b_sign = -1.0 if bx_pt[0] < bx + bw / 2 else 1.0
            pending_labels.append((ax_pt[0], ax_pt[1], 10 + len(a_txt) * 5.5, a_txt, color, False, a_sign))
            pending_labels.append((bx_pt[0], bx_pt[1], 10 + len(b_txt) * 5.5, b_txt, color, False, b_sign))
            continue

        a_members = sorted(edge.get("a_lag_members") or edge["a_ports"], key=_port_sort_key)
        b_members = sorted(edge.get("b_lag_members") or edge["b_ports"], key=_port_sort_key)
        n = max(len(a_members), len(b_members), 1)
        for i in range(n):
            ly1 = _anchor_y(a, key, i, ay, ah)
            ly2 = _anchor_y(b, key, i, by, bh)
            lx1, lx2 = ax_edge, bx_edge
            svg_parts.append(
                f'<line x1="{lx1:.1f}" y1="{ly1:.1f}" x2="{lx2:.1f}" y2="{ly2:.1f}" '
                f'stroke="{color}" stroke-width="1.6"/>'
            )
            a_txt = a_members[i] if i < len(a_members) else "?"
            b_txt = b_members[i] if i < len(b_members) else "?"
            a_pt = _label_point(lx1, ly1, lx2, ly2, 16)
            b_pt = _label_point(lx2, ly2, lx1, ly1, 16)
            a_sign = -1.0 if a_pt[0] < ax + aw / 2 else 1.0
            b_sign = -1.0 if b_pt[0] < bx + bw / 2 else 1.0
            pending_labels.append((a_pt[0], a_pt[1], 10 + len(a_txt) * 5.5, a_txt, color, True, a_sign))
            pending_labels.append((b_pt[0], b_pt[1], 10 + len(b_txt) * 5.5, b_txt, color, True, b_sign))

    # Every label reads top-to-bottom (rotated 90 degrees), matching the
    # switch boxes' own text - one consistent orientation across the whole
    # diagram instead of horizontal numbers next to vertical box labels.
    for x, y, extent, label, color, bold, _sign in _resolve_label_collisions(pending_labels):
        rect_w, rect_h = 13, extent
        svg_parts.append(
            f'<rect x="{x - rect_w / 2:.1f}" y="{y - rect_h / 2:.1f}" width="{rect_w:.1f}" height="{rect_h:.1f}" fill="{WHITE}"/>'
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" transform="rotate(90 {x:.1f} {y:.1f})" '
            f'font-family="\'Liberation Sans\', Arial, sans-serif" font-size="7.5" '
            f'font-weight="{700 if bold else 400}" '
            f'fill="{color}">{escape(label)}</text>'
        )

    for name, (x, y, w, h) in positions.items():
        sw = by_name[name]
        is_root = name in root_names
        svg_parts.append(_switch_box(
            x, y, w, h, name, is_root,
            sw.get("model") or "", sw.get("stp_priority") if is_root else None,
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
