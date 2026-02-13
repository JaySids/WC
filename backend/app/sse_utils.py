import json


def sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload)}\n\n"
