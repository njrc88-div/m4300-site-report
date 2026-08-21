"""Registry of data modules the app can pull from a switch.

This one list drives three things: the Explorer tab (ad-hoc fetch/view of
a single module), the Report Builder checklist (which modules to bake into
the PDF), and the PDF itself (module id -> template partial). Adding a new
endpoint to the app means adding one entry here and one template partial.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import enums
from .netgear_client import NetgearClient

FetchFn = Callable[[NetgearClient], Awaitable[dict]]


@dataclass
class Module:
    id: str
    label: str
    category: str
    description: str
    default_in_report: bool
    fetch: FetchFn


def _fan_status_text(fan_state) -> str:
    """fanState varies by firmware: a plain string on some, or a list
    containing one {fan_name: status} dict per fan module on others."""
    if not fan_state:
        return "-"
    if isinstance(fan_state, str):
        return fan_state
    if isinstance(fan_state, list):
        parts = []
        for item in fan_state:
            if isinstance(item, dict):
                parts.extend(f"{name}: {status}" for name, status in item.items())
            else:
                parts.append(str(item))
        return ", ".join(parts) if parts else "-"
    return str(fan_state)


def _avui_fan_status_text(fan_units: list) -> str:
    parts = []
    for unit in fan_units or []:
        for d in unit.get("details") or []:
            parts.append(f"{d.get('desc', 'fan')}: {d.get('speed', '?')} RPM")
    return ", ".join(parts) if parts else "-"


def _is_real_ip(value) -> bool:
    """0.0.0.0 shows up from AVUI's servicePortIP when the switch is
    managed over a VLAN interface rather than the dedicated service port -
    it's not a real address, so treat it the same as missing."""
    return bool(value) and value != "0.0.0.0"


def merge_avui_device_info(info: dict) -> dict:
    """AVUI's /device_info is shaped very differently from ConfigAgent's -
    stacking-aware (a `details` list, one entry per physical unit) rather
    than flat fields for a single unit. Fill in the flat keys the existing
    report table expects from the primary/first unit, and keep the raw
    per-unit/fan/sensor/cpu/memory arrays too so a stacking-aware switch
    can show all of that, not just unit 1."""
    if "details" not in info:
        return info
    units = info.get("details") or []
    primary = next((u for u in units if u.get("management")), units[0] if units else {})

    info.setdefault("macAddr", info.get("mac"))
    if _is_real_ip(info.get("servicePortIP")):
        info.setdefault("lanIpAddress", info.get("servicePortIP"))
    info.setdefault("model", primary.get("model"))
    info.setdefault("serialNumber", primary.get("sn"))
    info.setdefault("swVer", primary.get("fwVer"))
    info.setdefault("bootVersion", primary.get("bootVer"))
    info.setdefault("upTime", primary.get("upTime"))
    if info.get("poe") is not None:
        info.setdefault("poeState", info["poe"])

    cpu = info.get("cpu") or []
    if cpu:
        info.setdefault("cpuUsage", cpu[0].get("usage"))
    memory = info.get("memory") or []
    if memory:
        info.setdefault("memoryUsage", memory[0].get("usage"))

    # Normalize the nested per-unit sensor list into the flat
    # {sensorNum, sensorDesc, sensorTemp} shape the template already knows
    # how to render, so no template change is needed for the common case.
    if not info.get("temperatureSensors"):
        flat_sensors = []
        for unit in info.get("sensor") or []:
            for d in unit.get("details") or []:
                flat_sensors.append({
                    "sensorNum": d.get("id"), "sensorDesc": d.get("desc"), "sensorTemp": d.get("temp"),
                })
        if flat_sensors:
            info["temperatureSensors"] = flat_sensors

    return info


async def _device_info(client: NetgearClient) -> dict:
    info = dict(await client.get_device_info())
    info = merge_avui_device_info(info)
    info["fanState_text"] = (
        _avui_fan_status_text(info.get("fan")) if "fan" in info else _fan_status_text(info.get("fanState"))
    )
    # Some firmware doesn't populate lanIpAddress at all, or reports
    # 0.0.0.0 (observed on a 14.0.6.19 M4350 managed over a VLAN interface
    # rather than the dedicated service port). The address we used to log
    # in IS the management IP - it's a reliable fallback either way.
    if not _is_real_ip(info.get("lanIpAddress")):
        info["lanIpAddress"] = client.host
    return info


async def _firmware(client: NetgearClient) -> dict:
    dual = await client.get_dual_image_status()
    active = await client.get_active_image()
    return {"dual_image": dual, "active_image": active}


async def _identity(client: NetgearClient) -> dict:
    rfc1213 = await client.get_system_rfc1213()
    sys_config = await client.get_system_config()
    return {"rfc1213": rfc1213, "system_config": sys_config}


def _get_any(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _compress_ranges(nums: list[int]) -> str:
    """[9, 10, 11, 13, 17, 18] -> '9-11, 13, 17-18'."""
    nums = sorted(set(nums))
    if not nums:
        return ""
    ranges: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(ranges)


def _decorate_port_stat(p: dict) -> dict:
    p = dict(p)
    # Observed field name varies by firmware (portId vs portid vs id).
    p["portId"] = _get_any(p, "portId", "portid", "id", "port")
    p["status_text"] = enums.decode(enums.PORT_LINK_STATUS, p.get("status"))
    p["speed_text"] = enums.decode(enums.PORT_SPEED, p.get("speed"))
    p["duplex_text"] = enums.decode(enums.PORT_DUPLEX, p.get("duplex"))
    p["mode_text"] = enums.decode(enums.PORT_MODE, p.get("mode"))
    p["poe_status_text"] = enums.decode(enums.POE_STATUS, p.get("poeStatus"))
    p["stp_state_text"] = enums.decode(enums.STP_PORT_STATE, p.get("portState"))
    p["auth_state_text"] = enums.decode(enums.PORT_AUTH_STATE, p.get("portAuthState"))
    p["admin_text"] = enums.fmt_bool(p.get("adminMode"), "Enabled", "Disabled")
    return p


async def _ports(client: NetgearClient) -> dict:
    stats = await client.get_port_stats("ALL")
    decorated = sorted(
        (_decorate_port_stat(p) for p in stats), key=lambda p: p.get("portId") or 0
    )

    # A down port with no description and no LLDP neighbor carries zero
    # information beyond "it's down" - one line per port for dozens of
    # unused ports just buries the ports that actually matter. Roll those
    # into a single summary line; keep anything with real data in the table
    # even if it happens to be down right now.
    shown, down_ids = [], []
    for p in decorated:
        has_info = bool(p.get("myDesc")) or bool((p.get("neighborInfo") or {}).get("name"))
        if p.get("status") == 1 and not has_info:
            try:
                down_ids.append(int(p.get("portId")))
            except (TypeError, ValueError):
                shown.append(p)  # non-numeric port id - can't range-compress, keep it visible
        else:
            shown.append(p)

    return {
        "ports": shown,
        "down_count": len(down_ids),
        "down_ports_summary": _compress_ranges(down_ids),
    }


async def _port_config(client: NetgearClient) -> dict:
    stats = await client.get_port_stats("ALL")
    port_ids = sorted({p.get("portId") for p in stats if p.get("portId") is not None})
    configs = []
    for pid in port_ids:
        try:
            cfg = await client.get_port_config(pid)
        except Exception:
            continue
        if cfg:
            cfg = dict(cfg)
            cfg["admin_text"] = enums.fmt_bool(cfg.get("adminMode"), "Enabled", "Disabled")
            cfg["mode_text"] = enums.decode(enums.PORT_MODE, cfg.get("portType"))
            cfg["speed_text"] = enums.decode(
                {0: "Auto", 1: "10 Mbps", 2: "100 Mbps", 3: "1 Gbps", 4: "10 Gbps"},
                cfg.get("portSpeed"),
            )
            configs.append(cfg)
    return {"port_configs": configs}


async def _poe(client: NetgearClient) -> dict:
    global_cfg = await client.get_poe_config()
    ports = await client.get_poe_ports("ALL")
    decorated = []
    for p in sorted(ports, key=lambda x: x.get("portid", 0)):
        p = dict(p)
        p["status_text"] = enums.decode(enums.POE_STATUS, p.get("status"))
        p["limit_mode_text"] = enums.decode(enums.POE_POWER_LIMIT_MODE, p.get("powerLimitMode"))
        p["class_text"] = enums.decode(enums.POE_CLASS, p.get("classification"))
        p["enabled_text"] = enums.fmt_bool(p.get("enable"))
        decorated.append(p)
    return {"global": global_cfg, "ports": decorated}


async def _lag(client: NetgearClient) -> dict:
    groups = await client.get_lag_groups("ALL")
    decorated = []
    for g in groups:
        if not g.get("members"):
            continue  # unused LAG slot - every switch reports all 64, most are empty
        g = dict(g)
        g["type_text"] = enums.decode(enums.LAG_TYPE, g.get("type"))
        g["admin_text"] = enums.fmt_bool(g.get("adminMode"), "Enabled", "Disabled")
        decorated.append(g)
    return {"groups": decorated}


async def _vlans(client: NetgearClient) -> dict:
    stats = await client.get_port_stats("ALL")
    vlan_ids: set[int] = {1}
    for p in stats:
        for v in p.get("vlans") or []:
            try:
                vlan_ids.add(int(v))
            except (TypeError, ValueError):
                pass

    vlans = []
    for vid in sorted(vlan_ids):
        try:
            info = await client.get_vlan(vid)
        except Exception:
            info = {}
        try:
            membership = await client.get_vlan_membership(vid)
        except Exception:
            membership = {}
        if not info and not membership:
            continue
        vlans.append({"vlanId": vid, "info": info, "membership": membership})
    return {"vlans": vlans}


async def _svi(client: NetgearClient) -> dict:
    return {"interfaces": await client.get_vlan_ip_interfaces()}


def stp_priority_from(global_stp: dict) -> int | None:
    """Best-effort read of the root bridge priority out of a switch's STP
    global data, for the topology diagram to show next to the core
    switches (helps confirm which one is actually the STP root). Field
    name is a straight pass-through from the switch (see _stp below), so
    this tries the documented name plus the usual drift variants."""
    value = _get_any(global_stp or {}, "rootBridgePriority", "bridgePriority", "priority", "stpPriority")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _stp(client: NetgearClient) -> dict:
    global_stp = await client.get_stp()
    global_stp = dict(global_stp)
    global_stp["status_text"] = enums.fmt_bool(global_stp.get("status"), "Enabled", "Disabled")
    global_stp["mode_text"] = enums.decode(enums.STP_MODE, global_stp.get("stpMode"))

    interfaces = await client.get_dot1s_interfaces()
    decorated = []
    for i in sorted(interfaces, key=lambda x: x.get("interface", 0)):
        i = dict(i)
        i["guard_text"] = enums.decode(enums.STP_GUARD_MODE, i.get("intfGuardMode"))
        i["edge_text"] = enums.fmt_bool(i.get("intfEdgePortMode"))
        decorated.append(i)
    return {"global": global_stp, "interfaces": decorated}


async def _lldp(client: NetgearClient) -> dict:
    neighbors = await client.get_lldp_neighbors()
    decorated = []
    for n in neighbors:
        n = dict(n)
        n["chassis_subtype_text"] = enums.decode(
            enums.CHASSIS_ID_SUBTYPE, n.get("chassisIdSubtype")
        )
        # Prefer the neighbor's advertised system name over its raw chassis
        # ID (usually a MAC) - fall back through what's actually populated.
        n["neighbor_name"] = n.get("remoteSysName") or n.get("chassisId") or "-"
        n["neighbor_port"] = n.get("remotePortDesc") or n.get("remotePortId") or "-"
        mgmt_addrs = n.get("mgmtAddresses") or []
        if isinstance(mgmt_addrs, dict):
            mgmt_addrs = [mgmt_addrs]
        n["mgmt_ip"] = next((a.get("address") for a in mgmt_addrs if a.get("address")), "-")
        decorated.append(n)
    return {"neighbors": decorated}


async def _fiber(client: NetgearClient) -> dict:
    modules = await client.get_fiber_optics()
    return {"modules": modules}


async def _fdb(client: NetgearClient) -> dict:
    entries = await client.get_fdbs()
    entry_type_text = {
        0: "Static", 1: "Learned", 2: "Management", 3: "GMRP Learned", 4: "Self",
        5: "Dot1x Static", 6: "Dot1ag Static", 7: "Routing Interface", 8: "Learned (SW)",
        9: "FIP Snooping", 10: "CP Client", 11: "ethcfm Static", 12: "Y.1731 Static",
    }
    decorated = []
    for e in entries:
        e = dict(e)
        e["entry_type_text"] = enums.decode(entry_type_text, e.get("entryType"))
        decorated.append(e)
    return {"entries": decorated}


async def _running_config(client: NetgearClient) -> dict:
    lines = await client.get_device_config("running-config")
    return {"text": "\n".join(str(line) for line in lines)}


async def _mlag(client: NetgearClient) -> dict:
    mlag = dict(await client.get_mlag_status())
    peer_link = dict(mlag.get("peerLinkInfo") or {})
    mlag["peerLinkInfo"] = peer_link
    return {"mlag": mlag}


async def _ptp(client: NetgearClient) -> dict:
    status = await client.get_ptp_status()
    sw = dict(status.get("switchPtpCfg") or {})
    sw["ptpMode_text"] = enums.decode(enums.PTP_MODE, sw.get("ptpMode"))
    bc = dict(status.get("linuxptpConfig") or {})
    if bc:
        bc["clockOperMode_text"] = enums.decode(enums.PTP_CLOCK_OPER_MODE, bc.get("clockOperMode"))
    return {"switch": sw, "boundary_clock": bc}


async def _multicast(client: NetgearClient) -> dict:
    return {
        "groups": await client.get_multicast_groups(),
        "mode": await client.get_multicast_mode(),
        "block_list": await client.get_multicast_block_list(),
    }


MODULES: list[Module] = [
    Module("device_info", "Device Information", "Overview",
           "Model, serial, firmware, uptime, CPU/memory, fan and temperature status.",
           True, _device_info),
    Module("firmware", "Firmware & Boot Images", "Overview",
           "Dual flash image status and the currently active image.",
           False, _firmware),
    Module("identity", "System Identity & Access", "Overview",
           "System name, location, contact, and console/telnet access settings.",
           True, _identity),
    Module("ports", "Port Status & Statistics", "Ports",
           "Per-port link state, speed/duplex, VLANs, traffic counters, and LLDP neighbor.",
           True, _ports),
    Module("port_config", "Port Configuration", "Ports",
           "Per-port administrative configuration: mode, admin state, rate limiting, PVID.",
           False, _port_config),
    Module("poe", "Power over Ethernet", "Power",
           "Switch-wide PoE budget/usage and per-port PoE draw, class, and status.",
           True, _poe),
    Module("lag", "Link Aggregation Groups", "Ports",
           "Configured LAG/LACP groups and their member ports.",
           True, _lag),
    Module("mlag", "MLAG Status", "Ports",
           "Multi-chassis LAG domain, role, and peer-link status. Requires the newer "
           "AVUI API - not available on every switch/firmware.",
           False, _mlag),
    Module("vlans", "VLANs & Port Membership", "VLANs",
           "VLANs in use (discovered from port data) with tagged/untagged port membership.",
           False, _vlans),
    Module("svi", "SVI / VLAN Routing Interfaces", "VLANs",
           "Every VLAN with an IP interface configured - address, mask, MTU, DHCP, and "
           "whether routing is enabled for it.",
           True, _svi),
    Module("stp", "Spanning Tree Protocol", "Ports",
           "Global STP/RSTP/MSTP state plus per-interface guard/edge settings.",
           True, _stp),
    Module("lldp", "LLDP Neighbors", "Topology",
           "Directly connected neighbor devices discovered via LLDP.",
           True, _lldp),
    Module("fiber", "Fiber / SFP Diagnostics", "Ports",
           "DDM diagnostics (temp, voltage, Tx/Rx power) for installed SFP/SFP+ modules.",
           False, _fiber),
    Module("fdb", "MAC Address Table (FDB)", "Topology",
           "Full forwarding database - can be large; off by default.",
           False, _fdb),
    Module("ptp", "PTP Status", "PTP",
           "Precision Time Protocol mode and boundary-clock detail (role, source, intervals). "
           "Requires the newer AVUI API - not available on every switch/firmware.",
           True, _ptp),
    Module("multicast", "Multicast / IGMP", "Multicast",
           "Active IGMP-learned multicast group subscriptions, per-port multicast mode, and "
           "the multicast block list. Requires the newer AVUI API - not available on every "
           "switch/firmware.",
           False, _multicast),
    Module("running_config", "Running Configuration", "Config",
           "Full running-config text export. Not supported on every model/firmware - "
           "off by default and skipped gracefully if the switch rejects it.",
           False, _running_config),
]

MODULES_BY_ID: dict[str, Module] = {m.id: m for m in MODULES}
