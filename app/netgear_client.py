"""Async client for NETGEAR M4300/M4250/M4350 switches.

Two REST API generations exist in the wild and this client speaks both:

- **ConfigAgent** (the original API this client was built against): logs in
  via `POST /login {"login": {"username", "password"}}`, returns a bearer
  token, sent back as `Authorization: Bearer <token>`.
- **AVUI** (a newer, richer API - "NETGEAR AVUI API v2", Swagger 2.0, also
  documented as covering the M4250/M4300/M4350/M4500 AV-Line): logs in via
  `POST /login {"user": {"name", "password"}}`, returns a session token,
  sent back as a `session` header.

`login()` tries AVUI first and falls back to ConfigAgent - whichever
succeeds sets `self.auth_mode`, and every subsequent request is signed
accordingly. Methods that only exist on AVUI (e.g. MLAG status) raise a
clear error if login ended up on ConfigAgent instead of silently failing.

The published OpenAPI/Swagger specs are also not fully reliable on field
naming - e.g. the ConfigAgent spec documents most response envelopes with
snake_case wrapper keys (`{"device_info": {...}}`), but real firmware has
been observed returning camelCase instead (`{"deviceInfo": {...}}`), and
this can vary by model/firmware version. Every response is a
`{"resp": {...}, "<one data key>": ...}` envelope, so `_unwrap` tries the
documented key(s) first, then falls back to "whatever the other key is"
rather than silently returning nothing.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("m4300_report.client")

_REDACT_KEYS = {"token", "password", "session"}


class NetgearAPIError(RuntimeError):
    """Raised when the switch API returns an error or an unexpected shape."""


def _unwrap(data: dict, *preferred_keys: str):
    """Pull the payload out of a `{"resp": ..., "<data key>": ...}` envelope.

    Tries each of `preferred_keys` (spec-documented name(s), in order),
    then falls back to the first key that isn't "resp" - this is what
    makes the client tolerant of firmware that doesn't match the docs.
    """
    for key in preferred_keys:
        if key in data:
            return data[key]
    for key, value in data.items():
        if key != "resp":
            return value
    return {}


def _resp_ok(data: dict) -> bool:
    status = (data.get("resp") or {}).get("status")
    return status != "failure" and status != "fail" and status != "error"


def _redact(obj):
    if isinstance(obj, dict):
        return {
            k: ("***REDACTED***" if k in _REDACT_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class NetgearClient:
    """Thin async wrapper around one switch's REST API session.

    Use as an async context manager so the token/session is always
    acquired on entry and released on exit, even if a report module fails
    partway through:

        async with NetgearClient(host, username, password) as client:
            info = await client.get_device_info()
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 8443,
        verify_tls: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self._username = username
        self._password = password
        self._token: str | None = None
        self._session_token: str | None = None
        self.auth_mode: str | None = None  # "avui" | "configagent"
        self._http = httpx.AsyncClient(
            base_url=f"https://{host}:{port}/api/v1",
            verify=verify_tls,
            timeout=timeout,
        )

    async def __aenter__(self) -> "NetgearClient":
        await self.login()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self.auth_mode:
                await self.logout()
        finally:
            await self._http.aclose()

    # -- core request plumbing -------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        # base_url already carries /api/v1, but the AVUI spec is
        # inconsistent about whether an endpoint's own path repeats that
        # prefix (e.g. "/api/v1/mlag_show" vs "/device_info" - both appear
        # in the same document) - strip it here so a path can be copied
        # straight from the spec either way without double-prefixing.
        if path.startswith("/api/v1/"):
            path = path[len("/api/v1"):]
        headers = kwargs.pop("headers", {})
        if self.auth_mode == "avui" and self._session_token:
            headers["session"] = self._session_token
        elif self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise NetgearAPIError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise NetgearAPIError(f"{method} {path} returned non-JSON response") from exc
        logger.debug("RAW %s %s -> %s", method, path, _redact(data))
        return data

    async def login(self) -> None:
        if await self._try_avui_login():
            self.auth_mode = "avui"
            return
        await self._configagent_login()
        self.auth_mode = "configagent"

    async def _try_avui_login(self) -> bool:
        """AVUI login - returns False (rather than raising) on anything that
        looks like "this switch doesn't speak this API", so the caller can
        fall back to ConfigAgent. Connection-level failures (unreachable
        host, TLS errors) are left to propagate - retrying with a different
        login body won't fix those."""
        try:
            data = await self._request(
                "POST", "/login", json={"user": {"name": self._username, "password": self._password}}
            )
        except NetgearAPIError:
            return False
        if not _resp_ok(data):
            return False
        session = (data.get("user") or {}).get("session")
        if not session:
            return False
        self._session_token = session
        return True

    async def _configagent_login(self) -> None:
        body = {"login": {"username": self._username, "password": self._password}}
        data = await self._request("POST", "/login", json=body)
        if not _resp_ok(data):
            raise NetgearAPIError((data.get("resp") or {}).get("respMsg", "Login failed"))
        token = _unwrap(data, "login").get("token")
        if not token:
            raise NetgearAPIError("Login response did not include a token")
        self._token = token

    async def logout(self) -> None:
        try:
            await self._request("POST", "/logout")
        finally:
            self._token = None
            self._session_token = None

    def _require_avui(self, feature: str) -> None:
        if self.auth_mode != "avui":
            raise NetgearAPIError(
                f"{feature} requires the newer AVUI API - this switch logged in via the older "
                "ConfigAgent API instead, so this data isn't available."
            )

    # -- device / system ---------------------------------------------------

    async def get_device_info(self) -> dict:
        data = await self._request("GET", "/device_info")
        return _unwrap(data, "device_info", "deviceInfo")

    async def get_dual_image_status(self) -> dict:
        data = await self._request("GET", "/dual_image_status")
        return _unwrap(data, "dualImageStatus", "dual_image_status")

    async def get_active_image(self) -> dict:
        data = await self._request("GET", "/active_image")
        return _unwrap(data, "active_image", "activeImage")

    async def get_system_rfc1213(self) -> dict:
        data = await self._request("GET", "/system_rfc1213")
        return _unwrap(data, "system_rfc1213", "systemRfc1213")

    async def get_system_config(self) -> dict:
        data = await self._request("GET", "/system_config")
        return _unwrap(data, "system_config", "systemConfig")

    # -- ports ---------------------------------------------------------------

    async def get_port_stats(self, portid: str | int = "ALL") -> list[dict]:
        data = await self._request("GET", "/sw_portstats", params={"portid": portid})
        return _as_list(_unwrap(data, "switchStatsPort", "switch_stats_port"))

    async def get_port_config(self, portid: int) -> dict:
        data = await self._request("GET", "/swcfg_port", params={"portid": portid})
        return _unwrap(data, "switchPortConfig", "switch_port_config")

    # -- PoE -------------------------------------------------------------------

    async def get_poe_config(self) -> dict:
        data = await self._request("GET", "/poe_config")
        return _unwrap(data, "poe_config", "poeConfig")

    async def get_poe_ports(self, portid: str | int = "ALL") -> list[dict]:
        data = await self._request("GET", "/swcfg_poe", params={"portid": portid})
        return _as_list(_unwrap(data, "poePortConfig", "poe_port_config"))

    # -- LAGs ------------------------------------------------------------------

    async def get_lag_groups(self, lag_group: str | int = "ALL") -> list[dict]:
        data = await self._request("GET", "/sw_lag_cfg", params={"lag_group": lag_group})
        return _as_list(_unwrap(data, "switchConfigLagGroup", "switch_config_lag_group"))

    async def get_mlag_status(self) -> dict:
        """AVUI only - MLAG domain/role/peer-link status. Far more
        authoritative than inferring a collapsed-core pair from LLDP link
        counts (see app/topology.py), though nothing consumes this yet."""
        self._require_avui("MLAG status")
        data = await self._request("GET", "/api/v1/mlag_show")
        return _unwrap(data, "mlag", "Mlag")

    # -- VLANs -------------------------------------------------------------

    async def get_vlan(self, vlanid: int) -> dict:
        data = await self._request("GET", "/swcfg_vlan", params={"vlanid": vlanid})
        return _unwrap(data, "switchConfigVlan", "switch_config_vlan")

    async def get_vlan_membership(self, vlanid: int) -> dict:
        data = await self._request("GET", "/swcfg_vlan_membership", params={"vlanid": vlanid})
        return _unwrap(data, "vlanMembership", "vlan_membership")

    # -- Spanning Tree -----------------------------------------------------

    async def get_stp(self) -> dict:
        data = await self._request("GET", "/stp")
        return _unwrap(data, "stp")

    async def get_dot1s_interfaces(self) -> list[dict]:
        data = await self._request("GET", "/dot1s_interfaces")
        return _as_list(_unwrap(data, "dot1s_interfaces", "dot1sInterfaces"))

    # -- LLDP / topology -----------------------------------------------------

    async def get_lldp_neighbors(self) -> list[dict]:
        """Normalizes to the ConfigAgent shape (chassisId/chassisIdSubtype/
        ifIndex/remotePortId/remoteSysName/mgmtAddresses) regardless of
        which API actually served it, so modules.py and topology.py don't
        need to know which one ran - they just get better-populated fields
        (a real management IP, a real hostname) when AVUI is available."""
        if self.auth_mode == "avui":
            try:
                return await self._get_lldp_neighbors_avui()
            except NetgearAPIError:
                pass  # fall through to the ConfigAgent endpoint below
        data = await self._request("GET", "/lldp_remote_devices")
        return _as_list(_unwrap(data, "lldp_remote_devices", "lldpRemoteDevices"))

    async def _get_lldp_neighbors_avui(self) -> list[dict]:
        data = await self._request(
            "GET", "/neighbor", params={"indexPage": 1, "pageSize": 99999}
        )
        rows = _unwrap(data, "lldpRemoteDevice", "lldpRemoteDevices").get("rows", [])
        normalized = []
        for row in rows:
            mac = row.get("hostMacAddress")
            ip = row.get("hostIpAddress")
            normalized.append({
                "ifIndex": row.get("portNum"),
                "chassisId": mac,
                "chassisIdSubtype": 4 if mac else None,
                "remotePortId": row.get("remotePortId"),
                "remoteSysName": row.get("hostName"),
                "remoteSysDesc": row.get("systemDescription"),
                "mgmtAddresses": [{"type": "IPv4", "address": ip}] if ip else [],
            })
        return normalized

    # -- Fiber / SFP diagnostics ---------------------------------------------

    async def get_fiber_optics(self) -> list[dict]:
        data = await self._request("GET", "/fiber_optics")
        return _as_list(_unwrap(data, "fiber_optics", "fiberOptics"))

    # -- MAC address table (FDB) --------------------------------------------

    async def get_fdbs(self) -> list[dict]:
        data = await self._request("GET", "/fdbs")
        return _as_list(_unwrap(data, "fdb_stats", "fdbStats"))

    # -- Running configuration export ----------------------------------------
    # Documented for the M4250 API (not the M4300 doc set we started from),
    # but real firmware doesn't reliably match either spec - callers should
    # treat failure here as "not supported on this switch", not a hard error.

    async def get_device_config(self, file: str = "running-config") -> list[str]:
        data = await self._request("GET", "/device_config", params={"file": file})
        wrapper = _unwrap(data, "Device-Config", "deviceConfig", "device_config")
        if isinstance(wrapper, list):
            return wrapper
        if isinstance(wrapper, dict):
            for key in ("Device-config", "Device-Config", "deviceConfig", "device_config"):
                if key in wrapper:
                    return wrapper[key]
            for value in wrapper.values():
                if isinstance(value, list):
                    return value
        return []


def _as_list(data) -> list[dict]:
    """The spec is inconsistent about whether list endpoints return an
    object, a single object, or an array. Normalize to a list."""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [] if not data else [data]
    return []
