from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from .modules import Module
from .topology import TopologyDiagram

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
LOGO_PATH = (APP_DIR / "static" / "img" / "diversified-logo.png").as_uri()

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

# Post-rotation budget on a portrait A4 page (content area, in CSS px/SVG
# user units - this codebase treats the two as 1:1 throughout). The topology
# diagram is drawn left-to-right internally, then rotated 90 degrees so its
# left edge (root) lands at the page's top and its right edge (deepest tier)
# lands at the bottom - see app/topology.py for why. That swaps which of the
# diagram's natural dimensions needs to fit which page budget.
_TOPOLOGY_MAX_POST_ROTATION_WIDTH = 660.0
_TOPOLOGY_MAX_POST_ROTATION_HEIGHT = 900.0


def _topology_css(topology: TopologyDiagram) -> dict:
    scale = min(
        _TOPOLOGY_MAX_POST_ROTATION_HEIGHT / topology.width,
        _TOPOLOGY_MAX_POST_ROTATION_WIDTH / topology.height,
        1.0,
    )
    pre_w, pre_h = topology.width * scale, topology.height * scale
    return {"pre_w": pre_w, "pre_h": pre_h, "post_w": pre_h, "post_h": pre_w}


def render_report_pdf(
    *,
    site_name: str,
    client_name: str,
    prepared_by: str,
    notes: str,
    report_date: str,
    modules: list[Module],
    switch_results: list[dict],
    topology: TopologyDiagram | None = None,
) -> bytes:
    template = _env.get_template("report.html")
    html = template.render(
        site_name=site_name or "Untitled Site",
        client_name=client_name,
        prepared_by=prepared_by,
        notes=notes,
        report_date=report_date,
        modules=modules,
        switches=switch_results,
        logo_path=LOGO_PATH,
        topology_svg=topology.svg if topology else None,
        topology_css=_topology_css(topology) if topology else None,
    )
    return HTML(string=html, base_url=str(APP_DIR)).write_pdf()
