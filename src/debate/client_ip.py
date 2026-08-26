"""Resolve the human on the far side of trycloudflare."""


def client_ip(headers: dict[str, str], fallback: str = "") -> str:
    cf = (headers.get("cf-connecting-ip") or headers.get("CF-Connecting-IP") or "").strip()
    if cf:
        return cf
    xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or ""
    first = xff.split(",")[0].strip()
    if first:
        return first
    return fallback
