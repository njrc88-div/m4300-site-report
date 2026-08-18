"""Human-readable decodes for the numeric enum fields the M4300 API returns.

The REST API reports most states as integers (see the ConfigAgent OpenAPI
spec). A site report should read like a network engineer wrote it, not like
a raw API dump, so every enum field gets translated here before it reaches
a template.
"""

PORT_LINK_STATUS = {0: "Up", 1: "Down"}

PORT_SPEED = {
    1: "Auto-Negotiate",
    2: "100 Mbps Half",
    3: "100 Mbps Full",
    4: "10 Mbps Half",
    5: "10 Mbps Full",
    6: "100 Mbps Full (FX)",
    7: "1 Gbps Full",
    8: "10 Gbps Full",
    9: "20 Gbps Full",
    10: "40 Gbps Full",
    11: "25 Gbps Full",
    12: "50 Gbps Full",
    13: "100 Gbps Full",
    14: "155 Mbps (AAL5)",
    15: "5 Gbps Full",
    128: "2.5 Gbps Full",
    129: "LAG",
    130: "Unknown",
}

PORT_DUPLEX = {0: "Half", 1: "Full", 65535: "Auto"}

PORT_MODE = {
    0: "None",
    1: "General",
    2: "Access",
    3: "Trunk",
    4: "Private Host",
    5: "Private Promiscuous",
}

STP_PORT_STATE = {
    1: "Discarding",
    2: "Learning",
    3: "Forwarding",
    4: "Disabled",
    5: "Manual Forwarding",
    6: "Not Participating",
}

PORT_AUTH_STATE = {1: "Authorised", 2: "Unauthorised", 3: "N/A"}

POE_STATUS = {
    -1: "Invalid",
    0: "Disabled",
    1: "Searching",
    2: "Delivering Power",
    3: "Test",
    4: "Fault",
    5: "Other Fault",
    6: "Requesting Power",
    7: "Overload",
}

POE_POWER_LIMIT_MODE = {0: "Invalid", 1: "802.3af", 2: "User Defined", 3: "None", 4: "Count"}

POE_CLASS = {0: "Invalid", 1: "Class 0", 2: "Class 1", 3: "Class 2", 4: "Class 3"}

STP_MODE = {0: "STP", 1: "Unused", 2: "RSTP", 3: "MST"}

STP_GUARD_MODE = {0: "Loop", 1: "Root", 2: "None"}

LAG_TYPE = {0: "Dynamic (LACP)", 1: "Static"}

CHASSIS_ID_SUBTYPE = {
    1: "Chassis Component",
    2: "Interface Alias",
    3: "Port Component",
    4: "MAC Address",
    5: "Network Address",
    6: "Interface Name",
    7: "Local",
}


def decode(mapping: dict, value, default: str | None = None) -> str:
    if value is None:
        return default if default is not None else "-"
    try:
        return mapping.get(int(value), default if default is not None else str(value))
    except (TypeError, ValueError):
        return mapping.get(value, default if default is not None else str(value))


def fmt_bool(value, true_text="Yes", false_text="No") -> str:
    if value is None:
        return "-"
    return true_text if value else false_text
