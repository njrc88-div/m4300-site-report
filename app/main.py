from __future__ import annotations

import io
import logging
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    ExploreRequest,
    ReportRequest,
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

STATIC_DIR = "app/static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
    )

    filename = f"{req.site_name or 'site'}-switch-report.pdf".replace(" ", "-")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
