"""Async client for the NETGEAR M4300 ConfigAgent REST API.

The published OpenAPI spec (v2.0.0.59) documents most response envelopes
with snake_case wrapper keys (e.g. `{"device_info": {...}}`), but real
firmware in the field has been observed returning camelCase instead
(`{"deviceInfo": {...}}`) - and this can vary by model/firmware version.
Every response is a `{"resp": {...}, "<one data key>": ...}` envelope, so
`_unwrap` tries the documented key first, then falls back to "whatever the
other key is" rather than silently returning nothing.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("m4300_report.client")

_REDACT_KEYS = {"token", "password"}


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

    Use as an async context manager so the bearer token is always
    acquired on entry and the switch-side session is always released
    on exit, even if a report module fails partway through:

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
            if self._token:
                await self.logout()
        finally:
            await self._http.aclose()

    # -- core request plumbing -------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        if self._token:
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
        body = {"login": {"username": self._username, "password": self._password}}
        data = await self._request("POST", "/login", json=body)
        resp = data.get("resp", {})
        if resp.get("status") == "failure":
            raise NetgearAPIError(resp.get("respMsg", "Login failed"))
        token = _unwrap(data, "login").get("token")
        if not token:
            raise NetgearAPIError("Login response did not include a token")
        self._token = token

    async def logout(self) -> None:
        try:
            await self._request("POST", "/logout")
        finally:
            self._token = None

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
        data = await self._request("GET", "/lldp_remote_devices")
        return _as_list(_unwrap(data, "lldp_remote_devices", "lldpRemoteDevices"))

    # -- Fiber / SFP diagnostics ---------------------------------------------

    async def get_fiber_optics(self) -> list[dict]:
        data = await self._request("GET", "/fiber_optics")
        return _as_list(_unwrap(data, "fiber_optics", "fiberOptics"))

    # -- MAC address table (FDB) --------------------------------------------

    async def get_fdbs(self) -> list[dict]:
        data = await self._request("GET", "/fdbs")
        return _as_list(_unwrap(data, "fdb_stats", "fdbStats"))


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
