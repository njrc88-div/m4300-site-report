from __future__ import annotations

from pydantic import BaseModel, Field


class SwitchCredential(BaseModel):
    name: str = Field(..., description="Friendly label for this switch, e.g. 'IDF2 - GC752XP'")
    host: str = Field(..., description="IP address or hostname of the switch")
    port: int = 8443
    username: str
    password: str
    verify_tls: bool = False


class TestConnectionRequest(BaseModel):
    switch: SwitchCredential


class TestConnectionResponse(BaseModel):
    success: bool
    message: str
    device_name: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    auth_mode: str | None = None  # "avui" | "configagent" - which API generation this switch spoke


class ExploreRequest(BaseModel):
    switch: SwitchCredential
    module: str


class ReportRequest(BaseModel):
    site_name: str
    client_name: str = ""
    prepared_by: str = ""
    notes: str = ""
    switches: list[SwitchCredential]
    modules: list[str]
