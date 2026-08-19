from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from .modules import Module

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
LOGO_PATH = (APP_DIR / "static" / "img" / "diversified-logo.png").as_uri()

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

# Post-rotation budget on a portrait A4 page (content area, in CSS px/SVG
# user units - this codebase treats the two as 1:1 throughout). The
# topology diagram is drawn right-to-left internally (root/cores on the
# right, access layer to the left) then rotated 90 degrees counter-
# clockwise for the page: the right edge (cores) lands at the top, the
# left edge (access layer) lands at the bottom.
_TOPOLOGY_MAX_POST_ROTATION_WIDTH = 660.0
_TOPOLOGY_MAX_POST_ROTATION_HEIGHT = 900.0
_VIEWBOX_RE = re.compile(r'viewBox="0 0 (\d+(?:\.\d+)?) (\d+(?:\.\d+)?)"')


def _topology_css(svg: str) -> dict | None:
    match = _VIEWBOX_RE.search(svg)
    if not match:
        return None
    natural_w, natural_h = float(match.group(1)), float(match.group(2))
    scale = min(
        _TOPOLOGY_MAX_POST_ROTATION_HEIGHT / natural_w,
        _TOPOLOGY_MAX_POST_ROTATION_WIDTH / natural_h,
        1.0,
    )
    pre_w, pre_h = natural_w * scale, natural_h * scale
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
    topology_svg: str | None = None,
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
        topology_svg=topology_svg,
        topology_css=_topology_css(topology_svg) if topology_svg else None,
    )
    return HTML(string=html, base_url=str(APP_DIR)).write_pdf()
