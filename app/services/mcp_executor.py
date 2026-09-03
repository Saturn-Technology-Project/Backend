"""In-process MCP stand-in used only when IN_PROCESS_RUNTIME=true (tests)."""

from typing import Any


class MCPExecutionError(RuntimeError):
    pass


def _customer(arguments: dict[str, Any]) -> dict[str, Any]:
    name = arguments.get("name") or arguments.get("customer") or "Acme"
    return {
        "customer_id": arguments.get("customer_id", "cus_001"),
        "name": name,
        "status": "active",
    }


def _payment(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "payment_id": "pay_dup_001",
        "customer_id": arguments.get("customer_id", "cus_001"),
        "amount": arguments.get("amount", 840),
        "status": "duplicate_charge",
    }


def _refund(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_id": "re_001",
        "customer_id": arguments.get("customer_id", "cus_001"),
        "amount": arguments.get("amount"),
        "status": "success",
    }


def _email(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": "msg_001",
        "to": arguments.get("to"),
        "status": "sent",
    }


_HANDLERS = {
    "crm.get_customer": _customer,
    "billing.get_payment": _payment,
    "billing.refund": _refund,
    "email.send": _email,
}


class MCPExecutor:
    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = _HANDLERS.get(tool_name)
        if handler is None:
            raise MCPExecutionError(f"Unknown MCP tool: {tool_name}")
        return handler(arguments)


mcp_executor = MCPExecutor()
