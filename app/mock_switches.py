"""Fixture data for the built-in "Demo Switches" (see app/main.py's
_client_for and the "+ Add Demo Switches" button on the Switches tab).

Lets the whole app - Data Explorer, single- and multi-switch reports, and
the site topology diagram - be exercised with no real switch on the
network. Three switches, wired the way a small site actually would be:
two MLAG-peered cores (Demo Core A / Demo Core B) plus one edge switch
(Demo Edge A) with an independent uplink to each core. Every module's
data is internally consistent across the three - the same VLANs, the
same LLDP neighbor relationships, the same LAG membership - so the
topology diagram, VLAN discovery, and report all resolve the way a real
site's would, not just render in isolation.

MockNetgearClient implements the same async surface as NetgearClient
(app/netgear_client.py) so modules.py's fetch functions and main.py's
report/topology assembly run completely unmodified against it - only
_client_for() in main.py knows the difference.
"""
from __future__ import annotations

import copy

from .netgear_client import NetgearAPIError

VLAN_NAMES = {1: "Default", 10: "Data", 20: "Voice", 99: "Management"}


def _plain_port(port_id: str) -> dict:
    return {
        "portId": port_id, "status": 1, "speed": 1, "duplex": 65535, "mode": 2,
        "poeStatus": 0, "portState": 4, "portAuthState": 3, "adminMode": True,
        "myDesc": "", "vlans": [1], "neighborInfo": {}, "rxMbps": 0, "txMbps": 0,
    }


def _port_range(count: int) -> dict[str, dict]:
    return {f"0/{i}": _plain_port(f"0/{i}") for i in range(1, count + 1)}


def _set(ports: dict[str, dict], port_id: str, **fields) -> None:
    ports[port_id].update(fields)


def _core_switch(name: str, peer_name: str, mac: str, serial: str, ip: str,
                  edge_remote_port: str, stp_priority: int, mlag_role: str, peer_role: str,
                  ptp_boundary: bool) -> dict:
    ports = _port_range(28)  # M4300-28G: 24x 1G copper + 4x SFP+
    _set(ports, "0/3", status=0, speed=7, duplex=1, mode=3,
         myDesc="Uplink to Demo Edge A", vlans=[1, 10, 20, 99],
         neighborInfo={"name": "Demo Edge A", "portId": edge_remote_port},
         rxMbps=112, txMbps=88)
    for member in ("0/25", "0/26"):
        _set(ports, member, status=0, speed=8, duplex=1, mode=3,
             myDesc=f"LAG to {peer_name}", vlans=[1, 10, 20, 99],
             neighborInfo={"name": peer_name, "portId": member},
             rxMbps=340, txMbps=298)

    peer_mac = "BC:A5:11:00:00:02" if peer_name.endswith("B") else "BC:A5:11:00:00:01"
    peer_ip = "10.99.0.12" if peer_name.endswith("B") else "10.99.0.11"
    # chassisIdSubtype 4 = MAC address - matches topology.py's primary
    # (MAC-based) matching path, same as real LLDP data, so the diagram
    # still resolves correctly even if a demo switch's editable Label is
    # renamed away from its default "Demo Core A"-style name.
    lldp = [
        {"ifIndex": "0/3", "chassisId": "BC:A5:11:00:00:03", "chassisIdSubtype": 4,
         "remotePortId": edge_remote_port, "remoteSysName": "Demo Edge A",
         "remoteSysDesc": "NETGEAR M4300-12MP", "mgmtAddresses": [{"type": "IPv4", "address": "10.99.0.21"}]},
    ] + [
        {"ifIndex": member, "chassisId": peer_mac, "chassisIdSubtype": 4,
         "remotePortId": member, "remoteSysName": peer_name,
         "remoteSysDesc": "NETGEAR M4300-28G", "mgmtAddresses": [{"type": "IPv4", "address": peer_ip}]}
        for member in ("0/25", "0/26")
    ]

    boundary_clock = {}
    ptp_mode = 1  # Transparent Clock
    if ptp_boundary:
        ptp_mode = 2  # Boundary Clock
        boundary_clock = {
            "adminMode": True, "processRunningStatus": True,
            "identity": mac.replace(":", "").lower() + "0000",
            "clockRole": 1, "clockOperMode": 2, "sourceIPv4Addr": ip,
            "domain": 0, "priority1": 128, "priority2": 128,
            "syncInterval": -3, "announceInterval": 1, "delayReqInterval": -3,
        }

    return {
        "auth_mode": "avui+configagent",
        "avui": True,
        "device_info": {
            "name": name, "model": "M4300-28G", "serialNumber": serial, "macAddr": mac,
            "lanIpAddress": ip, "swVer": "14.0.6.19", "bootVersion": "10.0.5.4",
            "upTime": "142 days, 03:12:44", "lastReboot": "Software Reset",
            "numOfPorts": 28, "numOfActivePorts": 3, "cpuUsage": "4%", "memoryUsage": "31%",
            "memoryUsed": "289 MB", "fanState": "Operational", "poeState": False,
            "temperatureSensors": [{"sensorNum": 1, "sensorDesc": "Main Board", "sensorTemp": 41}],
        },
        "ports": ports,
        "lag_groups": [
            {"groupId": 1, "name": "core-peer", "type": 0, "adminMode": True, "members": ["0/25", "0/26"]},
        ],
        "mlag": {
            "domainId": 1, "adminStatus": "enabled", "operStatus": "up",
            "selfRole": mlag_role, "peerRole": peer_role, "peerDetectionStatus": "up",
            "mac": "AA:BB:CC:00:10:00", "mlagSystemPrio": 32768,
            "peerLinkInfo": {
                "portChannelId": 1, "adminStatus": "enabled", "type": "Static",
                "portList": ["0/25", "0/26"], "vlanList": [1, 10, 20, 99],
            },
        },
        "lldp_neighbors": lldp,
        "vlan_ports": ports,  # for deriving membership/discovery
        "svi": [
            {"vlanId": 99, "ipAddr": ip, "ipMask": "255.255.255.0", "ipMtu": 1500,
             "vlanRouting": True, "dhcpStatus": False},
        ],
        "stp_global": {
            "status": 1, "stpMode": 2, "forwardDelay": 15, "helloTime": 2, "maxAge": 20,
            "rootBridgePriority": stp_priority, "rootBridgeId": f"{stp_priority}.{mac.replace(':', '').lower()}",
        },
        "stp_interfaces": [
            {"interface": "0/3", "intfGuardMode": 2, "intfEdgePortMode": False, "bpduFilterMode": False, "bpduFloodMode": False},
            {"interface": "0/25", "intfGuardMode": 2, "intfEdgePortMode": False, "bpduFilterMode": False, "bpduFloodMode": False},
            {"interface": "0/26", "intfGuardMode": 2, "intfEdgePortMode": False, "bpduFilterMode": False, "bpduFloodMode": False},
        ],
        "poe_global": {"pseMainOperationStatus": "Not Supported", "totalPowerConsumedWatts": 0,
                       "powerManagmentMode": "-", "firmwareVersion": "-"},
        "poe_ports": [],
        "fiber": [
            {"port": member, "vendorName": "NETGEAR", "partNumber": "AXM761",
             "temp": 38 + i, "voltage": "3.31 V", "outputPower": -2.1, "inputPower": -3.4, "faultStatus": "None"}
            for i, member in enumerate(("0/25", "0/26"))
        ],
        "fdb": [
            {"interface": "0/3", "vlanId": 10, "mac": "AA:00:11:22:33:44", "entryType": 1},
            {"interface": "0/3", "vlanId": 20, "mac": "AA:00:11:22:33:45", "entryType": 1},
            {"interface": "0/25", "vlanId": 99, "mac": mac, "entryType": 4},
        ],
        "ptp": {"switchPtpCfg": {"ptpMode": ptp_mode}, "boundary_clock": boundary_clock},
        "multicast": {
            # Reuses ptp_boundary just as "is this the more active core" -
            # Core A gets one active subscription, Core B stays empty, so
            # the Explorer/report both show the real vs. empty-state UI.
            "groups": [
                {"unit": 1, "port": "0/3", "vlanId": 20, "multicastAddress": "239.1.1.10",
                 "subscriberAddress": "10.99.20.50", "type": "IGMPv3"},
            ] if ptp_boundary else [],
            "mode": {"physicalPort": [{"portNum": "0/3", "multicastMode": 1}]},
            "block_list": [],
        },
        "firmware": {
            "dual_image": {"activatedImgLabel": "1", "image1Version": "14.0.6.19", "image1Label": "1",
                            "image2Version": "14.0.4.10", "image2Label": "2"},
            "active_image": {"imageDescr": "Active"},
        },
        "identity": {
            "rfc1213": {"sysName": name, "sysDescr": "NETGEAR M4300-28G, Rapid City Software",
                        "sysLocation": "Demo Site - MDF", "sysContact": "netops@example.com"},
            "system_config": {"sysAccessLine": "Enabled", "sysTelnetServerAdminMode": "Disabled"},
        },
        "running_config": [
            f"! Demo running-config for {name}", "vlan database", "vlan 10,20,99",
            "exit", "interface 0/3", " switchport mode trunk", "exit",
        ],
    }


def _edge_switch() -> dict:
    ports = _port_range(12)  # M4300-12MP: 8x PoE+ copper + 4x SFP combo
    _set(ports, "0/1", status=0, speed=7, duplex=1, mode=2, poeStatus=2,
         myDesc="AP - Lobby", vlans=[20], neighborInfo={"name": "AP-Lobby-01", "portId": "eth0"},
         rxMbps=8, txMbps=22)
    _set(ports, "0/2", status=0, speed=7, duplex=1, mode=2, poeStatus=2,
         myDesc="AP - Warehouse", vlans=[20], neighborInfo={"name": "AP-Warehouse-01", "portId": "eth0"},
         rxMbps=6, txMbps=15)
    _set(ports, "0/3", status=0, speed=7, duplex=1, mode=2, poeStatus=2,
         myDesc="Camera - Entrance", vlans=[10], neighborInfo={"name": "IPCam-Entrance-01", "portId": "eth0"},
         rxMbps=4, txMbps=1)
    _set(ports, "0/4", status=0, speed=7, duplex=1, mode=2, poeStatus=2,
         myDesc="Camera - Loading Dock", vlans=[10], neighborInfo={"name": "IPCam-LoadingDock-01", "portId": "eth0"},
         rxMbps=4, txMbps=1)
    _set(ports, "0/9", status=0, speed=7, duplex=1, mode=3,
         myDesc="Uplink to Demo Core A", vlans=[1, 10, 20, 99],
         neighborInfo={"name": "Demo Core A", "portId": "0/3"}, rxMbps=90, txMbps=104)
    _set(ports, "0/10", status=0, speed=7, duplex=1, mode=3,
         myDesc="Uplink to Demo Core B", vlans=[1, 10, 20, 99],
         neighborInfo={"name": "Demo Core B", "portId": "0/3"}, rxMbps=22, txMbps=18)

    lldp = [
        {"ifIndex": "0/1", "chassisId": None, "chassisIdSubtype": 7, "remotePortId": "eth0",
         "remoteSysName": "AP-Lobby-01", "remoteSysDesc": "Wireless Access Point", "mgmtAddresses": []},
        {"ifIndex": "0/3", "chassisId": None, "chassisIdSubtype": 7, "remotePortId": "eth0",
         "remoteSysName": "IPCam-Entrance-01", "remoteSysDesc": "IP Camera", "mgmtAddresses": []},
        {"ifIndex": "0/9", "chassisId": "BC:A5:11:00:00:01", "chassisIdSubtype": 4, "remotePortId": "0/3",
         "remoteSysName": "Demo Core A", "remoteSysDesc": "NETGEAR M4300-28G",
         "mgmtAddresses": [{"type": "IPv4", "address": "10.99.0.11"}]},
        {"ifIndex": "0/10", "chassisId": "BC:A5:11:00:00:02", "chassisIdSubtype": 4, "remotePortId": "0/3",
         "remoteSysName": "Demo Core B", "remoteSysDesc": "NETGEAR M4300-28G",
         "mgmtAddresses": [{"type": "IPv4", "address": "10.99.0.12"}]},
    ]

    return {
        "auth_mode": "configagent",
        "avui": False,  # no AVUI session - MLAG/PTP/Multicast correctly unavailable, same as real firmware without it
        "device_info": {
            "name": "Demo Edge A", "model": "M4300-12MP", "serialNumber": "5CJ2A0003",
            "macAddr": "BC:A5:11:00:00:03", "lanIpAddress": "10.99.0.21", "swVer": "14.0.6.19",
            "bootVersion": "10.0.5.4", "upTime": "88 days, 19:40:02", "lastReboot": "Power Cycle",
            "numOfPorts": 12, "numOfActivePorts": 6, "cpuUsage": "7%", "memoryUsage": "38%",
            "memoryUsed": "142 MB", "fanState": "Operational", "poeState": True,
            "temperatureSensors": [{"sensorNum": 1, "sensorDesc": "Main Board", "sensorTemp": 44}],
        },
        "ports": ports,
        "lag_groups": [],  # each uplink is an independent link, not LACP-bonded - common on non-MLAG-aware edge gear
        "mlag": None,
        "lldp_neighbors": lldp,
        "vlan_ports": ports,
        "svi": [
            {"vlanId": 99, "ipAddr": "10.99.0.21", "ipMask": "255.255.255.0", "ipMtu": 1500,
             "vlanRouting": True, "dhcpStatus": False},
        ],
        "stp_global": {
            "status": 1, "stpMode": 2, "forwardDelay": 15, "helloTime": 2, "maxAge": 20,
            "rootBridgePriority": 32768, "rootBridgeId": "4096.bca511000001",
        },
        "stp_interfaces": [
            {"interface": "0/1", "intfGuardMode": 1, "intfEdgePortMode": True, "bpduFilterMode": True, "bpduFloodMode": False},
            {"interface": "0/9", "intfGuardMode": 2, "intfEdgePortMode": False, "bpduFilterMode": False, "bpduFloodMode": False},
            {"interface": "0/10", "intfGuardMode": 2, "intfEdgePortMode": False, "bpduFilterMode": False, "bpduFloodMode": False},
        ],
        "poe_global": {"pseMainOperationStatus": "On", "totalPowerConsumedWatts": 46.5,
                       "powerManagmentMode": "Class Based", "firmwareVersion": "1.0.0.3"},
        "poe_ports": [
            {"portid": "0/1", "enable": True, "status": 2, "classification": 4, "powerLimitMode": 1, "currentPower": 12900, "powerLimit": 30000},
            {"portid": "0/2", "enable": True, "status": 2, "classification": 4, "powerLimitMode": 1, "currentPower": 12100, "powerLimit": 30000},
            {"portid": "0/3", "enable": True, "status": 2, "classification": 2, "powerLimitMode": 1, "currentPower": 4900, "powerLimit": 7000},
            {"portid": "0/4", "enable": True, "status": 2, "classification": 2, "powerLimitMode": 1, "currentPower": 4700, "powerLimit": 7000},
        ] + [
            {"portid": f"0/{i}", "enable": False, "status": 0, "classification": 0, "powerLimitMode": 3, "currentPower": 0, "powerLimit": 0}
            for i in range(5, 9)
        ],
        "fiber": [],
        "fdb": [
            {"interface": "0/1", "vlanId": 20, "mac": "AA:00:AP:00:00:01", "entryType": 1},
            {"interface": "0/3", "vlanId": 10, "mac": "AA:00:CA:00:00:01", "entryType": 1},
            {"interface": "0/9", "vlanId": 99, "mac": "BC:A5:11:00:00:03", "entryType": 4},
        ],
        "ptp": None,  # AVUI-only, unavailable on this switch
        "multicast": None,  # AVUI-only, unavailable on this switch
        "firmware": {
            "dual_image": {"activatedImgLabel": "1", "image1Version": "14.0.6.19", "image1Label": "1",
                            "image2Version": "14.0.2.6", "image2Label": "2"},
            "active_image": {"imageDescr": "Active"},
        },
        "identity": {
            "rfc1213": {"sysName": "Demo Edge A", "sysDescr": "NETGEAR M4300-12MP, Rapid City Software",
                        "sysLocation": "Demo Site - Wiring Closet 2", "sysContact": "netops@example.com"},
            "system_config": {"sysAccessLine": "Enabled", "sysTelnetServerAdminMode": "Disabled"},
        },
        "running_config": None,  # not every model/firmware supports config export - shown as unsupported, same as real gear
    }


_FIXTURES: dict[str, dict] = {
    "core-a": _core_switch(
        "Demo Core A", "Demo Core B", "BC:A5:11:00:00:01", "5CJ2A0001", "10.99.0.11",
        edge_remote_port="0/9", stp_priority=4096, mlag_role="Primary", peer_role="Secondary",
        ptp_boundary=True,
    ),
    "core-b": _core_switch(
        "Demo Core B", "Demo Core A", "BC:A5:11:00:00:02", "5CJ2A0002", "10.99.0.12",
        edge_remote_port="0/10", stp_priority=8192, mlag_role="Secondary", peer_role="Primary",
        ptp_boundary=False,
    ),
    "edge-a": _edge_switch(),
}

DEMO_SWITCH_LIST = [
    {"id": "core-a", "name": "Demo Core A", "host": "10.99.0.11", "model": "M4300-28G"},
    {"id": "core-b", "name": "Demo Core B", "host": "10.99.0.12", "model": "M4300-28G"},
    {"id": "edge-a", "name": "Demo Edge A", "host": "10.99.0.21", "model": "M4300-12MP"},
]


def _vlan_membership(ports: dict[str, dict], vlanid: int) -> dict:
    members = []
    for port_id, port in ports.items():
        if vlanid in (port.get("vlans") or []):
            members.append({"port": port_id, "tagged": port.get("mode") == 3})
    return {"portMembers": members}


class MockNetgearClient:
    """Stand-in for NetgearClient backed by _FIXTURES - same async method
    surface, no network calls. See this module's docstring."""

    def __init__(self, demo_id: str) -> None:
        if demo_id not in _FIXTURES:
            raise NetgearAPIError(f"Unknown demo switch '{demo_id}'")
        self._demo_id = demo_id
        self._fixture = _FIXTURES[demo_id]
        self.host = next(d["host"] for d in DEMO_SWITCH_LIST if d["id"] == demo_id)
        self.auth_mode: str | None = None

    def _data(self, key: str):
        return copy.deepcopy(self._fixture[key])

    async def __aenter__(self) -> "MockNetgearClient":
        await self.login()
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def login(self) -> None:
        self.auth_mode = self._fixture["auth_mode"]

    async def logout(self) -> None:
        return None

    def _require_avui(self, feature: str) -> None:
        if not self._fixture["avui"]:
            raise NetgearAPIError(
                f"{feature} requires the newer AVUI API - this switch didn't authenticate via "
                "AVUI, so this data isn't available."
            )

    # -- device / system ---------------------------------------------------

    async def get_device_info(self) -> dict:
        return self._data("device_info")

    async def get_dual_image_status(self) -> dict:
        return self._data("firmware")["dual_image"]

    async def get_active_image(self) -> dict:
        return self._data("firmware")["active_image"]

    async def get_system_rfc1213(self) -> dict:
        return self._data("identity")["rfc1213"]

    async def get_system_config(self) -> dict:
        return self._data("identity")["system_config"]

    # -- ports ---------------------------------------------------------------

    async def get_port_stats(self, portid: str | int = "ALL") -> list[dict]:
        ports = self._data("ports")
        if portid == "ALL":
            return list(ports.values())
        return [ports[str(portid)]] if str(portid) in ports else []

    async def get_port_config(self, portid: int) -> dict:
        ports = self._fixture["ports"]
        port = ports.get(str(portid))
        if not port:
            return {}
        speed_code = {1: 0, 7: 3, 8: 4}.get(port["speed"], 0)
        poe_ports = {p["portid"] for p in self._fixture["poe_ports"]}
        return {
            "ID": str(portid), "description": port["myDesc"], "adminMode": port["adminMode"],
            "portType": port["mode"], "portSpeed": speed_code,
            "portVlanId": (port["vlans"] or [1])[0], "maxFrameSize": 9216,
            "isPoE": str(portid) in poe_ports,
        }

    # -- PoE -------------------------------------------------------------------

    async def get_poe_config(self) -> dict:
        return self._data("poe_global")

    async def get_poe_ports(self, portid: str | int = "ALL") -> list[dict]:
        ports = self._data("poe_ports")
        if portid == "ALL":
            return ports
        return [p for p in ports if p["portid"] == str(portid)]

    # -- LAGs ------------------------------------------------------------------

    async def get_lag_groups(self, lag_group: str | int = "ALL") -> list[dict]:
        return self._data("lag_groups")

    async def get_mlag_status(self) -> dict:
        self._require_avui("MLAG status")
        mlag = self._fixture["mlag"]
        if mlag is None:
            raise NetgearAPIError("MLAG is not configured on this switch.")
        return copy.deepcopy(mlag)

    # -- PTP -------------------------------------------------------------------

    async def get_ptp_status(self) -> dict:
        self._require_avui("PTP status")
        return self._data("ptp")

    # -- Multicast / IGMP ----------------------------------------------------

    async def get_multicast_groups(self) -> list[dict]:
        self._require_avui("Multicast groups")
        return self._data("multicast")["groups"]

    async def get_multicast_mode(self) -> dict:
        self._require_avui("Multicast mode")
        return self._data("multicast")["mode"]

    async def get_multicast_block_list(self) -> list[str]:
        self._require_avui("Multicast block list")
        return self._data("multicast")["block_list"]

    # -- VLANs -------------------------------------------------------------

    async def get_vlan(self, vlanid: int) -> dict:
        return {
            "vlanId": vlanid, "name": VLAN_NAMES.get(vlanid, f"VLAN{vlanid}"),
            "igmpConfig": {"igmpState": vlanid == 20}, "voiceVlanState": vlanid == 20,
        }

    async def get_vlan_membership(self, vlanid: int) -> dict:
        return _vlan_membership(self._fixture["vlan_ports"], vlanid)

    async def get_vlan_ip_interfaces(self) -> list[dict]:
        return self._data("svi")

    # -- Spanning Tree -----------------------------------------------------

    async def get_stp(self) -> dict:
        return self._data("stp_global")

    async def get_dot1s_interfaces(self) -> list[dict]:
        return self._data("stp_interfaces")

    # -- LLDP / topology -----------------------------------------------------

    async def get_lldp_neighbors(self) -> list[dict]:
        return self._data("lldp_neighbors")

    # -- Fiber / SFP diagnostics ---------------------------------------------

    async def get_fiber_optics(self) -> list[dict]:
        return self._data("fiber")

    # -- MAC address table (FDB) --------------------------------------------

    async def get_fdbs(self) -> list[dict]:
        return self._data("fdb")

    # -- Running configuration export ----------------------------------------

    async def get_device_config(self, file: str = "running-config") -> list[str]:
        cfg = self._fixture["running_config"]
        if cfg is None:
            raise NetgearAPIError(
                "GET /device_config -> HTTP 404 (no further detail) - this endpoint may not be "
                "supported on this switch/firmware."
            )
        return list(cfg)
