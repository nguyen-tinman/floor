"""Machine-readable refusals. Adapters fold these into the CMO envelope."""


class FloorError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def envelope_ok(payload: dict) -> dict:
    out = dict(payload)
    out.setdefault("ok", True)
    return out


def envelope_err(exc: FloorError) -> dict:
    return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
