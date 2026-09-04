from __future__ import annotations

import io
import logging
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import audit, auth, users
from .models import (
    CreateUserRequest,
    ExploreRequest,
    ReportRequest,
    ResetPasswordRequest,
    SetRoleRequest,
    SwitchCredential,
    TestConnectionRequest,
    TestConnectionResponse,
)
from .modules import MODULES, MODULES_BY_ID, merge_avui_device_info, stp_priority_from
from .netgear_client import NetgearAPIError, NetgearClient
from .report import render_report_pdf
from .topology import build_switch_topology

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("m4300_report")

app = FastAPI(title="M4300 Site Report Generator")

# Entirely opt-in - see app/auth.py's module docstring. Adds nothing to
# the request path unless SESSION_SECRET_KEY is set.
if auth.AUTH_ENABLED:
    users.init_db()
    if users.user_count() == 0:
        if auth.INITIAL_ADMIN_USERNAME and auth.INITIAL_ADMIN_PASSWORD:
            users.bootstrap_initial_admin(auth.INITIAL_ADMIN_USERNAME, auth.INITIAL_ADMIN_PASSWORD)
            logger.info("Created initial admin account %r from INITIAL_ADMIN_* env vars.", auth.INITIAL_ADMIN_USERNAME)
        else:
            logger.warning(
                "Login is enabled but no accounts exist yet, and INITIAL_ADMIN_USERNAME/"
                "INITIAL_ADMIN_PASSWORD aren't set - nobody can sign in until one is created."
            )
    # Starlette wraps middleware in the order added, with the *last* one
    # added ending up outermost (runs first on the way in) - AuthGateMiddleware
    # reads request.session, so SessionMiddleware must be added second to
    # actually execute first and populate it before the gate checks it.
    app.add_middleware(auth.AuthGateMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=auth.SESSION_SECRET_KEY, same_site="lax", https_only=auth.SESSION_COOKIE_SECURE)
    app.include_router(auth.router)
    logger.info("Local login enabled (%d account(s)).", users.user_count())
else:
    logger.warning("Login NOT configured (SESSION_SECRET_KEY unset) - this app is reachable without any login.")

STATIC_DIR = "app/static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/me")
async def whoami(request: Request) -> dict:
    if not auth.AUTH_ENABLED:
        return {"auth_enabled": False}
    return {"auth_enabled": True, "user": auth.current_user(request)}


@app.get("/api/audit")
async def audit_log(request: Request) -> dict:
    auth.require_admin(request)
    return {"events": audit.read_events()}


# -- Admin: account management (see app/users.py) --------------------------

@app.get("/api/admin/users")
async def admin_list_users(request: Request) -> dict:
    auth.require_admin(request)
    return {"users": users.list_users()}


@app.post("/api/admin/users")
async def admin_create_user(request: Request, body: CreateUserRequest) -> dict:
    auth.require_admin(request)
    try:
        users.create_user(body.username, body.password, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/admin/users/{username}/role")
async def admin_set_role(request: Request, username: str, body: SetRoleRequest) -> dict:
    admin = auth.require_admin(request)
    if username == admin["username"] and body.role != "admin" and users.admin_count() <= 1:
        raise HTTPException(status_code=400, detail="Can't demote the only remaining admin.")
    try:
        users.set_role(username, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/admin/users/{username}/reset-password")
async def admin_reset_password(request: Request, username: str, body: ResetPasswordRequest) -> dict:
    auth.require_admin(request)
    try:
        users.reset_password(username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(request: Request, username: str) -> dict:
    admin = auth.require_admin(request)
    if username == admin["username"]:
        raise HTTPException(status_code=400, detail="Can't delete your own account while signed in as it.")
    try:
        users.delete_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


def _client_for(switch: SwitchCredential) -> NetgearClient:
    return NetgearClient(
        host=switch.host,
        username=switch.username,
        password=switch.password,
        port=switch.port,
        verify_tls=switch.verify_tls,
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(f"{STATIC_DIR}/index.html")


@app.get("/api/modules")
async def list_modules() -> list[dict]:
    return [
        {
            "id": m.id,
            "label": m.label,
            "category": m.category,
            "description": m.description,
            "default_in_report": m.default_in_report,
        }
        for m in MODULES
    ]


@app.post("/api/test-connection", response_model=TestConnectionResponse)
async def test_connection(req: TestConnectionRequest) -> TestConnectionResponse:
    try:
        async with _client_for(req.switch) as client:
            info = merge_avui_device_info(dict(await client.get_device_info()))
            auth_mode = client.auth_mode
    except NetgearAPIError as exc:
        return TestConnectionResponse(success=False, message=str(exc))
    except Exception as exc:  # connection refused, TLS error, timeout, DNS failure...
        return TestConnectionResponse(success=False, message=f"Connection failed: {exc}")

    return TestConnectionResponse(
        success=True,
        message="Connected successfully.",
        device_name=info.get("name") or req.switch.name,
        model=info.get("model"),
        firmware=info.get("swVer"),
        serial_number=info.get("serialNumber"),
        auth_mode=auth_mode,
    )


@app.post("/api/explore")
async def explore(req: ExploreRequest) -> dict:
    module = MODULES_BY_ID.get(req.module)
    if module is None:
        raise HTTPException(status_code=404, detail=f"Unknown module '{req.module}'")
    try:
        async with _client_for(req.switch) as client:
            data = await module.fetch(client)
    except NetgearAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Connection failed: {exc}") from exc
    return {"module": module.id, "label": module.label, "data": data}


@app.post("/api/report")
async def generate_report(req: ReportRequest) -> StreamingResponse:
    if not req.switches:
        raise HTTPException(status_code=400, detail="At least one switch is required.")

    selected_modules = [MODULES_BY_ID[m] for m in req.modules if m in MODULES_BY_ID]
    if not selected_modules:
        raise HTTPException(status_code=400, detail="At least one data module is required.")

    switch_results = []
    for switch in req.switches:
        entry: dict = {
            "switch": switch, "error": None, "modules": {},
            "_lldp_neighbors": [], "_lag_groups": [], "_mlag": None, "_stp_priority": None,
        }
        try:
            async with _client_for(switch) as client:
                # Always pull device_info first - every section header needs it,
                # and it doubles as the connectivity check for this switch.
                entry["device_info"] = merge_avui_device_info(dict(await client.get_device_info()))
                for module in selected_modules:
                    try:
                        entry["modules"][module.id] = await module.fetch(client)
                    except Exception as exc:
                        logger.warning("Module %s failed for %s: %s", module.id, switch.host, exc)
                        entry["modules"][module.id] = {"error": str(exc)}
                # Also pull LLDP and LAG data for the topology diagram even if
                # those modules weren't selected for the report body - LAG
                # membership is what lets the diagram tell a real
                # aggregated link from two independent physical links.
                lldp_result = entry["modules"].get("lldp")
                if lldp_result is not None and not lldp_result.get("error"):
                    entry["_lldp_neighbors"] = lldp_result.get("neighbors", [])
                else:
                    try:
                        entry["_lldp_neighbors"] = await client.get_lldp_neighbors()
                    except Exception as exc:
                        logger.warning("Topology LLDP fetch failed for %s: %s", switch.host, exc)

                lag_result = entry["modules"].get("lag")
                if lag_result is not None and not lag_result.get("error"):
                    entry["_lag_groups"] = lag_result.get("groups", [])
                else:
                    try:
                        entry["_lag_groups"] = [dict(g) for g in await client.get_lag_groups("ALL")]
                    except Exception as exc:
                        logger.warning("Topology LAG fetch failed for %s: %s", switch.host, exc)

                # MLAG status is the authoritative signal for which pair of
                # switches is the real collapsed core - see topology.py. Most
                # switches won't have it (AVUI-only, and only when actually
                # configured as an MLAG peer), so this is silently
                # best-effort, same as the running-config module.
                mlag_result = entry["modules"].get("mlag")
                if mlag_result is not None and not mlag_result.get("error"):
                    entry["_mlag"] = mlag_result.get("mlag")
                else:
                    try:
                        entry["_mlag"] = await client.get_mlag_status()
                    except Exception:
                        pass

                stp_result = entry["modules"].get("stp")
                if stp_result is not None and not stp_result.get("error"):
                    entry["_stp_priority"] = stp_priority_from(stp_result.get("global"))
                else:
                    try:
                        entry["_stp_priority"] = stp_priority_from(dict(await client.get_stp()))
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Switch %s unreachable: %s", switch.host, exc)
            entry["error"] = str(exc)
        switch_results.append(entry)

    topology_svg = build_switch_topology([
        {
            "name": r["switch"].name,
            "mac": (r.get("device_info") or {}).get("macAddr"),
            "model": (r.get("device_info") or {}).get("model", ""),
            "neighbors": r.get("_lldp_neighbors") or [],
            "lag_groups": r.get("_lag_groups") or [],
            "mlag": r.get("_mlag"),
            "stp_priority": r.get("_stp_priority"),
        }
        for r in switch_results
        if not r["error"]
    ])

    pdf_bytes = render_report_pdf(
        site_name=req.site_name,
        client_name=req.client_name,
        prepared_by=req.prepared_by,
        notes=req.notes,
        report_date=date.today().strftime("%d %B %Y"),
        modules=selected_modules,
        switch_results=switch_results,
        topology_svg=topology_svg,
        abridged=req.abridged,
        vlan_info_image=req.vlan_info_image,
    )

    filename = f"{req.site_name or 'site'}-switch-report.pdf".replace(" ", "-")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
