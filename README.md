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
   layout) — nothing is saved server-side.

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
| VLANs & Port Membership | `/swcfg_vlan`, `/swcfg_vlan_membership` (VLAN IDs discovered from port data) |
| Spanning Tree Protocol | `/stp`, `/dot1s_interfaces` |
| LLDP Neighbors | `/lldp_remote_devices` |
| Fiber / SFP Diagnostics | `/fiber_optics` |
| MAC Address Table (FDB) | `/fdbs` (off by default in reports — can be large) |
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
