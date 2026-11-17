class GatewayError(Exception):
    """An error with a stable machine-readable code, surfaced to API and MCP clients."""

    def __init__(self, code: str, detail: str = "", status: int = 400):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.status = status

    def to_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail}


class BackendError(Exception):
    """A hypervisor command or API call failed."""
