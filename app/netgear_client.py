"""Async client for NETGEAR M4300/M4250/M4350 switches.

Two REST API generations exist in the wild and this client speaks both:

- **ConfigAgent** (the original API this client was built against): logs in
  via `POST /login {"login": {"username", "password"}}`, returns a bearer
  token, sent back as `Authorization: Bearer <token>`.
- **AVUI** (a newer, richer API - "NETGEAR AVUI API v2", Swagger 2.0, also
  documented as covering the M4250/M4300/M4350/M4500 AV-Line): logs in via
  `POST /login {"user": {"name", "password"}}`, returns a session token,
  sent back as a `session` header.

`login()` attempts BOTH, independently, rather than picking one - real
firmware has been observed exposing genuinely different subsets of paths
on each: e.g. one unit answered `/device_info`, `/sw_lag_cfg`,
`/fiber_optics`, `/neighbor` and every AVUI-only endpoint (MLAG/PTP/
multicast) over its AVUI session, but 404'd on `/sw_portstats`,
`/poe_config`, `/stp`, `/fdbs`, `/dual_image_status`, `/system_rfc1213`
over that same session - all of which work fine over ConfigAgent's
session on the same switch. So `_request` tries whichever session logged
in successfully, and if that one 404s while the *other* session is also
authenticated, retries the identical path over that one before giving up
- cheaper and more robust than trying to hardcode which path lives on
which API per model/firmware. Methods that are genuinely AVUI-only (e.g.
MLAG status) still raise a clear error up front if no AVUI session
exists, rather than let a 404 round-trip happen for nothing.

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
import re

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


def _describe_error_body(resp: httpx.Response) -> str:
    """Turn a non-2xx response into a short, readable message.

    Some endpoints that aren't implemented on a given switch/firmware
    return an HTML error page (an embedded web server's generic 500/404),
    not a JSON error - dumping that markup straight into the UI is noisy
    and reads like a broken app rather than an unsupported endpoint, so
    pull out just the title/heading when the body looks like HTML."""
    text = resp.text.strip()
    content_type = resp.headers.get("content-type", "")
    if "html" in content_type.lower() or text[:15].lower().lstrip().startswith("<!doctype html") or text[:5].lower() == "<html":
        match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if not match:
            match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.IGNORECASE | re.DOTALL)
        heading = re.sub(r"\s+", " ", match.group(1)).strip() if match else "no further detail"
        return f"HTTP {resp.status_code} ({heading}) - this endpoint may not be supported on this switch/firmware."
    return f"HTTP {resp.status_code}: {text[:300]}"


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
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._token: str | None = None
        self._session_token: str | None = None
        self.auth_mode: str | None = None  # "avui" | "configagent" | "avui+configagent"
        self._http = httpx.AsyncClient(
            base_url=f"https://{host}:{port}/api/v1",
            verify=verify_tls,
            timeout=timeout,
        )
        # AVUI's Swagger spec declares `scheme: https` with no port, which
        # means it defaults to 443 (the switch's normal web-GUI port) - a
        # different port than ConfigAgent's dedicated REST API, which the
        # M4300/M4250 docs put on 8443 (this client's default `port`). If
        # AVUI login fails on the configured port, `_try_avui_login` retries
        # once against 443 using this second client, and every subsequent
        # AVUI request is sent over whichever client actually authenticated.
        self._avui_http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NetgearClient":
        await self.login()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._token or self._session_token:
                await self.logout()
        finally:
            await self._http.aclose()
            if self._avui_http is not None:
                await self._avui_http.aclose()

    # -- core request plumbing -------------------------------------------------

    async def _send(self, http: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
        # base_url already carries /api/v1, but the AVUI spec is
        # inconsistent about whether an endpoint's own path repeats that
        # prefix (e.g. "/api/v1/mlag_show" vs "/device_info" - both appear
        # in the same document) - strip it here so a path can be copied
        # straight from the spec either way without double-prefixing.
        if path.startswith("/api/v1/"):
            path = path[len("/api/v1"):]
        resp = await http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise NetgearAPIError(f"{method} {path} -> {_describe_error_body(resp)}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise NetgearAPIError(f"{method} {path} returned non-JSON response") from exc
        logger.debug("RAW %s %s -> %s", method, path, _redact(data))
        return data

    def _auth_headers_for(self, mode: str) -> dict | None:
        if mode == "avui" and self._session_token:
            return {"session": self._session_token}
        if mode == "configagent" and self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return None

    def _http_for(self, mode: str) -> httpx.AsyncClient:
        if mode == "avui" and self._avui_http is not None:
            return self._avui_http
        return self._http

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        modes = [m for m in ("avui", "configagent") if self._auth_headers_for(m) is not None]
        if not modes:
            raise NetgearAPIError("Not logged in")
        base_headers = kwargs.pop("headers", {}) or {}
        last_exc: NetgearAPIError | None = None
        for i, mode in enumerate(modes):
            headers = {**base_headers, **self._auth_headers_for(mode)}
            try:
                return await self._send(self._http_for(mode), method, path, headers=headers, **kwargs)
            except NetgearAPIError as exc:
                last_exc = exc
                # A 404 here means "this path doesn't exist on this
                # particular API surface", not "this switch doesn't have
                # this data" - worth trying the other logged-in session
                # (if any) before giving up. Any other error (403, 500,
                # non-JSON...) is a real answer from the right place.
                if i < len(modes) - 1 and "HTTP 404" in str(exc):
                    continue
                raise
        raise last_exc  # pragma: no cover - unreachable, satisfies type checkers

    async def login(self) -> None:
        avui_ok = await self._try_avui_login()
        configagent_ok = await self._try_configagent_login()
        if avui_ok and configagent_ok:
            self.auth_mode = "avui+configagent"
        elif avui_ok:
            self.auth_mode = "avui"
        elif configagent_ok:
            self.auth_mode = "configagent"
        else:
            raise NetgearAPIError(
                "Login failed: neither the AVUI nor the ConfigAgent API accepted these credentials."
            )

    async def _try_avui_login(self) -> bool:
        """AVUI login - returns False (rather than raising) on anything that
        looks like "this switch doesn't speak this API", so the caller can
        fall back to ConfigAgent. Tries the switch's configured port first,
        then falls back to 443 (AVUI's likely real home - see __init__)
        before giving up. Per-port connection failures (refused, timeout)
        are treated the same as "not this API" so the 443 fallback still
        gets a chance even if the configured port refuses outright."""
        ports = [self.port] if self.port == 443 else [self.port, 443]
        for port in ports:
            http = self._http if port == self.port else httpx.AsyncClient(
                base_url=f"https://{self.host}:{port}/api/v1",
                verify=self._verify_tls,
                timeout=self._timeout,
            )
            try:
                data = await self._send(
                    http, "POST", "/login",
                    json={"user": {"name": self._username, "password": self._password}},
                )
            except (NetgearAPIError, httpx.TransportError):
                if http is not self._http:
                    await http.aclose()
                continue
            if not _resp_ok(data):
                if http is not self._http:
                    await http.aclose()
                continue
            session = (data.get("user") or {}).get("session")
            if not session:
                if http is not self._http:
                    await http.aclose()
                continue
            self._session_token = session
            self._avui_http = http if http is not self._http else None
            return True
        return False

    async def _try_configagent_login(self) -> bool:
        """ConfigAgent login on the switch's configured port - returns False
        (rather than raising) so it can be attempted independently of the
        AVUI login above; both can succeed on the same switch."""
        body = {"login": {"username": self._username, "password": self._password}}
        try:
            data = await self._send(self._http, "POST", "/login", json=body)
        except NetgearAPIError:
            return False
        if not _resp_ok(data):
            return False
        token = _unwrap(data, "login").get("token")
        if not token:
            return False
        self._token = token
        return True

    async def logout(self) -> None:
        for mode in ("avui", "configagent"):
            headers = self._auth_headers_for(mode)
            if headers is None:
                continue
            try:
                await self._send(self._http_for(mode), "POST", "/logout", headers=headers)
            except NetgearAPIError:
                pass
        self._token = None
        self._session_token = None

    def _require_avui(self, feature: str) -> None:
        if not self._session_token:
            raise NetgearAPIError(
                f"{feature} requires the newer AVUI API - this switch didn't authenticate via "
                "AVUI, so this data isn't available."
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

    # -- PTP (Precision Time Protocol) --------------------------------------

    async def get_ptp_status(self) -> dict:
        """AVUI only. Combines the switch-wide PTP mode with the
        Linux-PTP boundary-clock detail endpoint - the latter 404s on units
        that aren't running as a boundary clock, which is normal, not an
        error, so that half is best-effort."""
        self._require_avui("PTP status")
        sw_cfg = _unwrap(await self._request("GET", "/sw_ptp_cfg"), "switchPtpCfg")
        try:
            bc_data = await self._request("GET", "/api/v1/linuxptp/ptp_bc_cfg")
            bc_cfg = _unwrap(bc_data, "linuxptpConfig")
        except NetgearAPIError:
            bc_cfg = {}
        return {"switchPtpCfg": sw_cfg, "linuxptpConfig": bc_cfg}

    # -- Multicast / IGMP ----------------------------------------------------

    async def get_multicast_groups(self) -> list[dict]:
        """AVUI only. Active IGMP-learned multicast subscriptions - which
        AV stream is being pulled by which port, on which VLAN."""
        self._require_avui("Multicast groups")
        data = await self._request("GET", "/multicast_groups")
        groups = _unwrap(data, "multicastGroups")
        rows = groups.get("rows") if isinstance(groups, dict) else None
        return rows or []

    async def get_multicast_mode(self) -> dict:
        self._require_avui("Multicast mode")
        data = await self._request("GET", "/multicast_mode")
        return _unwrap(data, "multicastModeConfig")

    async def get_multicast_block_list(self) -> list[str]:
        self._require_avui("Multicast block list")
        data = await self._request("GET", "/multicast_block_address")
        return _unwrap(data, "blockAddressList") or []

    # -- VLANs -------------------------------------------------------------

    async def get_vlan(self, vlanid: int) -> dict:
        data = await self._request("GET", "/swcfg_vlan", params={"vlanid": vlanid})
        return _unwrap(data, "switchConfigVlan", "switch_config_vlan")

    async def get_vlan_membership(self, vlanid: int) -> dict:
        data = await self._request("GET", "/swcfg_vlan_membership", params={"vlanid": vlanid})
        return _unwrap(data, "vlanMembership", "vlan_membership")

    async def get_vlan_ip_interfaces(self) -> list[dict]:
        """SVIs - every VLAN with an IP interface configured (routed or
        not), one entry per VLAN, straight from the switch - not looped
        per-VLAN-ID like get_vlan()/get_vlan_membership() above, since
        this endpoint already returns the full set in one call."""
        data = await self._request("GET", "/vlan_ip")
        return _as_list(_unwrap(data, "vlan_ip", "vlanIp"))

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
        if self._session_token:
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
