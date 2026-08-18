"""Async client for the NETGEAR M4300 ConfigAgent REST API (v2.0.0.59)."""
from __future__ import annotations

import httpx


class NetgearAPIError(RuntimeError):
    """Raised when the switch API returns an error or an unexpected shape."""


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
            return resp.json()
        except ValueError as exc:
            raise NetgearAPIError(f"{method} {path} returned non-JSON response") from exc

    async def login(self) -> None:
        body = {"login": {"username": self._username, "password": self._password}}
        data = await self._request("POST", "/login", json=body)
        resp = data.get("resp", {})
        if resp.get("status") == "failure":
            raise NetgearAPIError(resp.get("respMsg", "Login failed"))
        token = (data.get("login") or {}).get("token")
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
        return (await self._request("GET", "/device_info")).get("device_info", {})

    async def get_dual_image_status(self) -> dict:
        return (await self._request("GET", "/dual_image_status")).get("dualImageStatus", {})

    async def get_active_image(self) -> dict:
        return (await self._request("GET", "/active_image")).get("active_image", {})

    async def get_system_rfc1213(self) -> dict:
        return (await self._request("GET", "/system_rfc1213")).get("system_rfc1213", {})

    async def get_system_config(self) -> dict:
        return (await self._request("GET", "/system_config")).get("system_config", {})

    # -- ports ---------------------------------------------------------------

    async def get_port_stats(self, portid: str | int = "ALL") -> list[dict]:
        data = (await self._request("GET", "/sw_portstats", params={"portid": portid})).get(
            "switchStatsPort", []
        )
        return _as_list(data)

    async def get_port_config(self, portid: int) -> dict:
        return (await self._request("GET", "/swcfg_port", params={"portid": portid})).get(
            "switchPortConfig", {}
        )

    # -- PoE -------------------------------------------------------------------

    async def get_poe_config(self) -> dict:
        return (await self._request("GET", "/poe_config")).get("poe_config", {})

    async def get_poe_ports(self, portid: str | int = "ALL") -> list[dict]:
        data = (await self._request("GET", "/swcfg_poe", params={"portid": portid})).get(
            "poePortConfig", []
        )
        return _as_list(data)

    # -- LAGs ------------------------------------------------------------------

    async def get_lag_groups(self, lag_group: str | int = "ALL") -> list[dict]:
        data = (
            await self._request("GET", "/sw_lag_cfg", params={"lag_group": lag_group})
        ).get("switchConfigLagGroup", [])
        return _as_list(data)

    # -- VLANs -------------------------------------------------------------

    async def get_vlan(self, vlanid: int) -> dict:
        return (await self._request("GET", "/swcfg_vlan", params={"vlanid": vlanid})).get(
            "switchConfigVlan", {}
        )

    async def get_vlan_membership(self, vlanid: int) -> dict:
        return (
            await self._request("GET", "/swcfg_vlan_membership", params={"vlanid": vlanid})
        ).get("vlanMembership", {})

    # -- Spanning Tree -----------------------------------------------------

    async def get_stp(self) -> dict:
        return (await self._request("GET", "/stp")).get("stp", {})

    async def get_dot1s_interfaces(self) -> list[dict]:
        data = (await self._request("GET", "/dot1s_interfaces")).get("dot1s_interfaces", [])
        return _as_list(data)

    # -- LLDP / topology -----------------------------------------------------

    async def get_lldp_neighbors(self) -> list[dict]:
        data = (await self._request("GET", "/lldp_remote_devices")).get(
            "lldp_remote_devices", []
        )
        return _as_list(data)

    # -- Fiber / SFP diagnostics ---------------------------------------------

    async def get_fiber_optics(self) -> list[dict]:
        data = (await self._request("GET", "/fiber_optics")).get("fiber_optics", [])
        return _as_list(data)

    # -- MAC address table (FDB) --------------------------------------------

    async def get_fdbs(self) -> list[dict]:
        data = (await self._request("GET", "/fdbs")).get("fdb_stats", [])
        return _as_list(data)


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
