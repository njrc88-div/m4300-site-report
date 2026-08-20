from __future__ import annotations

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
    abridged: bool = False,
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
        abridged=abridged,
    )
    return HTML(string=html, base_url=str(APP_DIR)).write_pdf()
