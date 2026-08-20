# M4300 / M4250 Site Report Generator

A small containerized app that talks to NETGEAR M4300 and M4250 switches
over their REST API (ConfigAgent) and produces a branded, client-ready PDF
site report. Built for site surveys / commissioning reports where you need
to document switch configuration and status across a site with a mix of
switch models.

The M4300 and M4250 REST APIs share the same endpoint set and schemas
(the M4250 doc set adds three extra endpoints: `/device_config`,
`/download`, `/download_status`). In practice, **real firmware doesn't
reliably match either published OpenAPI spec** — see "A note on API
drift" below — so the client is written to tolerate that rather than to
special-case one model over the other. There's no switch-model gate on
which modules you can run; whatever a given switch actually supports,
works.

**Two API generations, one client, both logged in at once.** A separate,
newer "AVUI" API (Swagger 2.0, session-header auth) also exists and
covers the same M4250/M4300/M4350/M4500 line with a richer surface —
notably real MLAG status (vs. inferring a collapsed-core pair from LAG
link counts) and an LLDP endpoint that returns the neighbor's hostname
and management IP directly. AVUI's spec declares `scheme: https` with no
port, which defaults to 443 (the switch's normal web-GUI port) — a
different port than ConfigAgent's dedicated REST API, which normally
sits on 8443 (this app's default per-switch port), so the AVUI login is
tried on the switch's configured port first and then retried once
against 443 before being treated as unavailable.

`NetgearClient.login()` attempts AVUI and ConfigAgent independently
rather than picking one — real firmware has been observed exposing a
genuinely different subset of paths on each. One real switch answered
`/device_info`, `/sw_lag_cfg`, `/fiber_optics`, `/neighbor`, and every
AVUI-only endpoint (MLAG/PTP/multicast) over its AVUI session, but 404'd
on `/sw_portstats`, `/poe_config`, `/stp`, `/fdbs`, `/dual_image_status`,
and `/system_rfc1213` over that same session — all of which worked fine
over ConfigAgent's session on the same switch a moment later. So every
request tries whichever session is available, and if it 404s while the
*other* session is also logged in, retries the identical path there
before giving up — cheaper and more robust than hardcoding which path
lives on which API per model/firmware. Test Connection reports which
session(s) a given switch accepted (`auth_mode`: `avui`, `configagent`,
or `avui+configagent`); AVUI-only modules (MLAG, PTP, Multicast) raise a
clear "not available on this switch" error only when no AVUI session
exists at all.

## What it does

The app has three tabs:

1. **Switches** — add every switch at the site (label, host/IP, port,
   username, password). Credentials are kept in your browser's
   `localStorage` only; they're sent to the backend solely when you click
   Test, Fetch, or Generate, and the server never writes them to disk.
   Click **Test** to confirm the app can log in and read `/device_info` —
   the switch's reported model and firmware version are then shown in the
   table and carried through to the Explorer and Report Builder tabs, so
   you always know which physical unit (M4300, M4350, M4250, ...) you're
   looking at.
2. **Data Explorer** — pick a saved switch and a data module (ports, PoE,
   VLANs, STP, LLDP neighbors, fiber diagnostics, etc.) and fetch it live
   to sanity-check the API before building a report.
3. **Report Builder** — pick which switches and which data modules go into
   the report (checkboxes, customer-by-customer), fill in site/client
   metadata, and click **Generate PDF Report**. The app logs into every
   selected switch, pulls the selected data, and streams back a PDF
   styled with Diversified branding (cover page, page numbers, section
   layout) — nothing is saved server-side. An **Abridged report** toggle
   drops low-value per-interface detail that's mostly defaults on a typical
   switch (currently: the STP per-interface Guard Mode/Edge Port/BPDU
   table) — off by default, so a fully detailed report is always still one
   click away.

Report content is cleaned up automatically regardless of the abridged
toggle: down ports with no description and no LLDP neighbor (i.e.
genuinely unused, not just temporarily down) are rolled into a single
summary line instead of one dead row per port; LAG groups with no member
ports (every switch reports all 64 possible slots, almost all empty) are
dropped from the table; and the per-unit "Stack Members" table only
appears for switches that are actually stacked (more than one unit) since
a single-unit switch just duplicates the fields already shown above it.

When a report covers 2+ switches, a **site topology** page is inserted
right after the cover page: a hierarchical diagram of switch-to-switch
links built from LLDP data (`app/topology.py`), regardless of whether the
LLDP Neighbors module was checked for the report body. Non-switch LLDP
neighbors (APs, phones, etc.) are filtered out — only links between two
switches that are both in the report are drawn. The diagram reads right
to left: root/core switch(es) are pinned to the right edge, each hop
toward the access layer sits further left, and the legend is ordered to
match (access/other switches first, core switches second). Boxes are
narrow and tall rather than wide and short, with text stacked
top-to-bottom inside each one — hostname band at the top (sized to fit a
full switch name, not just enough to be recognizable), model below that,
and (root/core switches only) STP root bridge priority at the bottom —
each band's text individually rotated 90 degrees to read top-to-bottom,
the same convention every other label on the diagram uses. That's what
lets the diagram use the *page's* full height: box height is sized
dynamically against a page-height budget (shrinking as more switches
share a tier, growing when there are few), and the whole thing is
rendered at that exact pixel size — not auto-scaled — so it fills the
page instead of sitting at whatever size its content naturally needs.

Root selection: real MLAG status (domain ID + system MAC, from the AVUI
API) is authoritative when available — two switches reporting the same
enabled domain/MAC are, by definition, the two peers of that MLAG, i.e.
the actual collapsed core, regardless of how the rest of the topology
happens to be wired. Without that data it falls back to graph shape: a
switch pair with a LAG between them (from real LAG config when available,
otherwise more than one physical LLDP link between them — LLDP is
per-port, so an aggregated link shows up as multiple neighbor entries) is
treated as a collapsed/dual-core pair *if* the LAG'd partner also looks
like a hub (connects to more than just this one switch) — this matters
because once every uplink is commonly a LAG, not just the core-core link,
a leaf's LAG to its core is otherwise structurally identical to the real
core-core LAG. Both become roots, drawn side by side. Otherwise the single
switch with the most inter-switch links becomes the root. Each connected
group of switches lays out as its own tree; any switch with no detected
link to another switch in the report still gets shown, in an "unlinked"
column, rather than silently disappearing.

A LAG is drawn as N straight lines, one per physical member (real member
ports from LAG config when available, accurate even when LLDP only
reports one summarized entry for the whole port-channel, not one per
member) - each in the LAG's own color (cycled from a fixed 10-color
palette, so a given LAG can be traced by eye through a diagram where
several cross paths) and each with its own interface-number label
sitting right beside that specific line, rather than one combined "1,4"
label stranded at the line's midpoint that can't tell you which number
belongs to which physical cable. Every interface-number label reads
top-to-bottom, rotated 90 degrees the same as the switch boxes' own text,
so the whole diagram shares one text orientation. A plain (non-LAG) link
is one gray line with a single port label at each end.

Every individual physical line touching a box - not just every LAG as a
whole - gets its own independent, evenly-spaced slot along that box's
full edge (ordered by the other end's position, then by member index so
a LAG's members still land adjacent to each other), the same mechanism
that keeps different switches' connections apart extended down to
individual cables. A box with four edges' worth of 2-member LAGs plus a
core-core LAG has nine lines to place; giving each one a real slot across
the box's whole height is what actually guarantees none of them, or
their labels, can collide - sharing one slot per LAG and nudging members
apart locally could still drift a label into a neighboring box or a
different LAG's label depending on that particular line's angle, which a
truly independent slot per line can't do. Root switches also show
their STP root bridge priority, when the switch reports one, so you can
see at a glance which core actually won root election.

## Data pulled from the switch

| Module | Source endpoints |
|---|---|
| Device Information | `/device_info` |
| Firmware & Boot Images | `/dual_image_status`, `/active_image` |
| System Identity & Access | `/system_rfc1213`, `/system_config` |
| Port Status & Statistics | `/sw_portstats?portid=ALL` |
| Port Configuration | `/swcfg_port` (per port) |
| Power over Ethernet | `/poe_config`, `/swcfg_poe?portid=ALL` |
| Link Aggregation Groups | `/sw_lag_cfg?lag_group=ALL` |
| MLAG Status | `/mlag_show` — AVUI only, off by default |
| VLANs & Port Membership | `/swcfg_vlan`, `/swcfg_vlan_membership` (VLAN IDs discovered from port data) |
| Spanning Tree Protocol | `/stp`, `/dot1s_interfaces` |
| LLDP Neighbors | `/neighbor` on AVUI (hostname + management IP), `/lldp_remote_devices` on ConfigAgent |
| Fiber / SFP Diagnostics | `/fiber_optics` |
| MAC Address Table (FDB) | `/fdbs` (off by default in reports — can be large) |
| PTP Status | `/sw_ptp_cfg`, `/linuxptp/ptp_bc_cfg` — AVUI only, off by default |
| Multicast / IGMP | `/multicast_groups`, `/multicast_mode`, `/multicast_block_address` — AVUI only, off by default |
| Running Configuration | `/device_config?file=running-config` (off by default — not every model/firmware supports it; fails gracefully if not) |

Adding a new endpoint is a matter of adding one client method in
[`app/netgear_client.py`](app/netgear_client.py), one entry in
[`app/modules.py`](app/modules.py), and one template branch in
[`app/templates/report.html`](app/templates/report.html).

## A note on API drift

The M4300 unit this app was first validated against (firmware 14.0.6.19)
returns response envelopes in `camelCase` (`{"deviceInfo": {...}}`) even
though the M4300 OpenAPI spec we were given documents `snake_case`
(`{"device_info": {...}}`) — and the M4250 spec documents the same
snake_case convention. Rather than branch behavior per model/firmware
version, `netgear_client.py`'s `_unwrap()` helper just takes whatever
data key is actually present in the response (trying documented spellings
first, falling back to "the other key besides `resp`"). This is why the
app doesn't gate which GET modules are available per switch model — model
is surfaced for your own reference, but every module is attempted
against every switch, and failures are reported per-module rather than
blocking the whole report.

## Running it

Requires Docker.

```bash
docker compose up --build
```

Then open **http://localhost:8080**.

Or without compose:

```bash
docker build -t m4300-site-report .
docker run -p 8080:8080 m4300-site-report
```

### Notes on switch connectivity

- The switch API listens on `https://<ip>:8443/api/v1` by default (port is
  editable per-switch in the UI).
- Switch web UIs typically use a self-signed certificate. **Verify TLS**
  is off by default per switch — turn it on only if you've installed a
  trusted cert on the switch.
- If the switch is on a different network/VLAN than wherever this
  container runs, make sure that network path exists (routing, firewall,
  same Docker network, etc.).

## Project layout

```
app/
  main.py             FastAPI routes (test-connection, explore, report)
  models.py            Pydantic request/response models
  netgear_client.py    Async REST client for the M4300 API (login, GETs)
  modules.py            Registry of data modules shared by Explorer + Report Builder
  enums.py               Human-readable decodes for the API's numeric enums
  report.py                Jinja2 -> WeasyPrint PDF rendering
  templates/report.html      The PDF layout itself
  static/                   Front-end (vanilla HTML/CSS/JS, no build step)
```

## Security notes

- No database, no server-side credential storage. Each request carries the
  credentials it needs and they live only for the lifetime of that request.
- The container runs as a non-root user.
- This tool is intended for use on trusted internal/management networks
  against switches you're authorized to administer.
