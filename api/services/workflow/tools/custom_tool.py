"""Custom tool execution for user-defined HTTP API tools."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from api.db import db_client
from api.utils.credential_auth import build_auth_header
from api.utils.template_renderer import render_template

# Map tool parameter types to JSON schema types
TYPE_MAP = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}

RUNTIME_IDENTITY_HEADER = "X-Dograh-Runtime-Identity"
RUNTIME_TOOL_CALL_ID_PREFIX = "tcid:v1:"
LATE_TERMINAL_HEADER = "X-Jeeves-Late-Terminal"


@dataclass(frozen=True)
class LateTerminalCapability:
    """Strict opt-in configuration for the versioned Jeeves late-result protocol."""

    poll_url: str
    revoke_url: str
    ack_url: str
    max_wait_ms: int
    poll_wait_ms: int


def _late_terminal_capability(config: Dict[str, Any]) -> Optional[LateTerminalCapability]:
    value = config.get("late_terminal")
    if value is None:
        return None
    required = {
        "version",
        "poll_url",
        "revoke_url",
        "ack_url",
        "max_wait_ms",
        "poll_wait_ms",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("version") != 1:
        raise ValueError("Invalid late-terminal capability")
    urls = (value["poll_url"], value["revoke_url"], value["ack_url"])
    if not all(isinstance(url, str) and url.strip() for url in urls):
        raise ValueError("Invalid late-terminal capability")
    max_wait_ms = value["max_wait_ms"]
    poll_wait_ms = value["poll_wait_ms"]
    if (
        not isinstance(max_wait_ms, int)
        or isinstance(max_wait_ms, bool)
        or not isinstance(poll_wait_ms, int)
        or isinstance(poll_wait_ms, bool)
        or not 1 <= poll_wait_ms <= max_wait_ms <= 60_000
    ):
        raise ValueError("Invalid late-terminal capability")
    return LateTerminalCapability(
        poll_url=value["poll_url"],
        revoke_url=value["revoke_url"],
        ack_url=value["ack_url"],
        max_wait_ms=max_wait_ms,
        poll_wait_ms=poll_wait_ms,
    )


def late_terminal_registration_id(identity: "HttpToolRuntimeIdentity") -> str:
    """Match the gateway's versioned registration derivation exactly."""
    material = "\n".join(
        (
            "1",
            identity.agent_scope,
            identity.tool_call_id,
            identity.tool_uuid,
            identity.workflow_run_id,
        )
    )
    return f"ltr_v1_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


async def _late_terminal_headers(
    tool: Any,
    organization_id: Optional[int],
    identity: "HttpToolRuntimeIdentity",
) -> Dict[str, str]:
    headers = dict((tool.definition or {}).get("config", {}).get("headers", {}) or {})
    credential_uuid = (tool.definition or {}).get("config", {}).get("credential_uuid")
    if credential_uuid and organization_id:
        credential = await db_client.get_credential_by_uuid(credential_uuid, organization_id)
        if credential:
            headers.update(build_auth_header(credential))
    headers[RUNTIME_IDENTITY_HEADER] = identity.as_header_value()
    return headers


async def poll_late_terminal(
    tool: Any,
    capability: LateTerminalCapability,
    identity: "HttpToolRuntimeIdentity",
    registration_id: str,
    organization_id: Optional[int],
) -> Dict[str, Any]:
    """Read one exact registered outcome; malformed or unavailable replies fail closed."""
    try:
        headers = await _late_terminal_headers(tool, organization_id, identity)
        async with httpx.AsyncClient(timeout=capability.poll_wait_ms / 1000) as client:
            response = await client.request(
                method="POST",
                url=capability.poll_url,
                headers=headers,
                json={"version": 1, "registration_id": registration_id},
            )
        data = response.json()
    except Exception:
        return {"status": "error", "error": "Late-terminal polling failed"}
    if response.status_code == 202 and data == {"version": 1, "status": "pending"}:
        return {"status": "pending"}
    if (
        response.status_code == 200
        and isinstance(data, dict)
        and set(data) == {"version", "status", "terminal"}
        and data.get("version") == 1
        and data.get("status") == "terminal"
    ):
        return {"status": "terminal", "terminal": data["terminal"]}
    return {"status": "error", "error": "Invalid late-terminal poll response"}


async def _late_terminal_close(
    tool: Any,
    capability: LateTerminalCapability,
    identity: "HttpToolRuntimeIdentity",
    registration_id: str,
    organization_id: Optional[int],
    operation: str,
) -> None:
    url = capability.ack_url if operation == "ack" else capability.revoke_url
    try:
        headers = await _late_terminal_headers(tool, organization_id, identity)
        async with httpx.AsyncClient(timeout=capability.poll_wait_ms / 1000) as client:
            await client.request(
                method="POST",
                url=url,
                headers=headers,
                json={"version": 1, "registration_id": registration_id},
            )
    except Exception:
        logger.warning("Late-terminal {} failed", operation)


async def ack_late_terminal(*args: Any, **kwargs: Any) -> None:
    await _late_terminal_close(*args, **kwargs, operation="ack")


async def revoke_late_terminal(*args: Any, **kwargs: Any) -> None:
    await _late_terminal_close(*args, **kwargs, operation="revoke")


def canonicalize_http_tool_call_id(provider_tool_call_id: str) -> str:
    """Return a stable bounded identity for one provider-owned tool call."""
    if not isinstance(provider_tool_call_id, str) or not provider_tool_call_id.strip():
        raise ValueError("Stable tool call identity is required for this HTTP tool")
    digest = hashlib.sha256(provider_tool_call_id.encode("utf-8")).hexdigest()
    return f"{RUNTIME_TOOL_CALL_ID_PREFIX}{digest}"


@dataclass(frozen=True)
class HttpToolRuntimeIdentity:
    """Dograh-owned identity forwarded to explicitly opted-in HTTP tools."""

    tool_call_id: str
    workflow_run_id: str
    tool_uuid: str
    agent_scope: str

    def as_header_value(self) -> str:
        """Serialize the reserved identity envelope deterministically."""
        return json.dumps(
            {
                "agent_scope": self.agent_scope,
                "tool_call_id": self.tool_call_id,
                "tool_uuid": self.tool_uuid,
                "workflow_run_id": self.workflow_run_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def tool_to_function_schema(tool: Any) -> Dict[str, Any]:
    """Convert a ToolModel to an LLM function schema.

    Args:
        tool: ToolModel instance with name, description, and definition

    Returns:
        Function schema dict compatible with OpenAI/Anthropic function calling
    """
    definition = tool.definition or {}
    config = definition.get("config", {})
    parameters = config.get("parameters", []) or []

    # Build properties and required list from parameters
    properties = {}
    required = []

    for param in parameters:
        param_name = param.get("name", "")
        param_type = param.get("type", "string")
        param_desc = param.get("description", "")
        param_required = param.get("required", True)

        if not param_name:
            continue

        schema_type = TYPE_MAP.get(param_type, "string")
        if schema_type == "object":
            properties[param_name] = {
                "type": "object",
                "additionalProperties": True,
                "description": param_desc,
            }
        elif schema_type == "array":
            properties[param_name] = {
                "type": "array",
                "items": {},
                "description": param_desc,
            }
        else:
            properties[param_name] = {
                "type": schema_type,
                "description": param_desc,
            }

        if param_required:
            required.append(param_name)

    # If this is an end_call tool with endCallReason enabled, add a required 'reason' parameter
    if definition.get("type") == "end_call" and config.get("endCallReason", False):
        default_description = (
            "The reason for ending the call (e.g., 'voicemail_detected', "
            "'issue_resolved', 'customer_requested')"
        )
        properties["reason"] = {
            "type": "string",
            "description": config.get("endCallReasonDescription")
            or default_description,
        }
        required.append("reason")

    # Sanitize tool name for function name (lowercase, underscores only)
    function_name = re.sub(r"[^a-z0-9_]", "_", tool.name.lower())
    # Remove consecutive underscores and trim
    function_name = re.sub(r"_+", "_", function_name).strip("_")

    return {
        "type": "function",
        "function": {
            "name": function_name,
            "description": tool.description or f"Execute {tool.name} tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        "_tool_uuid": tool.tool_uuid,
    }


def _coerce_parameter_value(value: Any, param_type: str) -> Any:
    """Coerce a rendered preset parameter into the configured JSON type."""

    if value is None:
        return None

    if param_type == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    if param_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value

        rendered = str(value).strip()
        if rendered == "":
            return None

        if re.fullmatch(r"[-+]?\d+", rendered):
            return int(rendered)

        return float(rendered)

    if param_type == "boolean":
        if isinstance(value, bool):
            return value

        if isinstance(value, (int, float)):
            return bool(value)

        rendered = str(value).strip().lower()
        if rendered in {"true", "1", "yes", "y", "on"}:
            return True
        if rendered in {"false", "0", "no", "n", "off"}:
            return False

        raise ValueError(f"Cannot convert '{value}' to boolean")

    if param_type == "object":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Cannot convert '{value}' to object") from exc
        if isinstance(value, dict):
            return value
        raise ValueError(f"Cannot convert '{value}' to object")

    if param_type == "array":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Cannot convert '{value}' to array") from exc
        if isinstance(value, list):
            return value
        raise ValueError(f"Cannot convert '{value}' to array")

    return value


def _resolve_preset_parameters(
    config: Dict[str, Any],
    call_context_vars: Optional[Dict[str, Any]],
    gathered_context_vars: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve fixed/template-backed parameters before executing the HTTP request."""

    preset_parameters = config.get("preset_parameters", []) or []
    if not preset_parameters:
        return {}

    initial_context = dict(call_context_vars or {})
    render_context: Dict[str, Any] = {
        **initial_context,
        "initial_context": initial_context,
        "gathered_context": dict(gathered_context_vars or {}),
    }

    resolved: Dict[str, Any] = {}
    for param in preset_parameters:
        param_name = (param.get("name") or "").strip()
        if not param_name:
            continue

        rendered = render_template(param.get("value_template", ""), render_context)
        if rendered in (None, ""):
            if param.get("required", True):
                raise ValueError(
                    f"Preset parameter '{param_name}' resolved to an empty value"
                )
            continue

        resolved[param_name] = _coerce_parameter_value(
            rendered, param.get("type", "string")
        )

    return resolved


async def execute_http_tool(
    tool: Any,
    arguments: Dict[str, Any],
    call_context_vars: Optional[Dict[str, Any]] = None,
    gathered_context_vars: Optional[Dict[str, Any]] = None,
    organization_id: Optional[int] = None,
    runtime_identity: Optional[HttpToolRuntimeIdentity] = None,
) -> Dict[str, Any]:
    """Execute an HTTP API tool.

    Args:
        tool: ToolModel instance
        arguments: Arguments passed by the LLM (parameter name -> value)
        call_context_vars: Initial context variables available at runtime
        gathered_context_vars: Variables extracted during the conversation
        organization_id: Organization ID for credential lookup
        runtime_identity: Dograh-owned identity for opted-in tools

    Returns:
        Result dict with response data or error
    """
    definition = tool.definition or {}
    config = definition.get("config", {})
    try:
        late_terminal = _late_terminal_capability(config)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    # Get HTTP method and URL
    method = config.get("method", "POST").upper()
    url = config.get("url", "")

    # Get headers from config
    headers = dict(config.get("headers", {}) or {})

    # Add auth header if credential is configured
    credential_uuid = config.get("credential_uuid")
    if credential_uuid and organization_id:
        try:
            credential = await db_client.get_credential_by_uuid(
                credential_uuid, organization_id
            )
            if credential:
                auth_header = build_auth_header(credential)
                headers.update(auth_header)
                logger.debug(f"Applied credential '{credential.name}' to tool request")
            else:
                logger.warning(
                    f"Credential {credential_uuid} not found for tool '{tool.name}'"
                )
        except Exception as e:
            logger.error(f"Failed to fetch credential for tool '{tool.name}': {e}")

    # Get timeout
    timeout_ms = config.get("timeout_ms", 5000)
    timeout_seconds = timeout_ms / 1000

    try:
        preset_arguments = _resolve_preset_parameters(
            config, call_context_vars, gathered_context_vars
        )
    except ValueError as e:
        logger.error(f"Custom tool '{tool.name}' preset parameter error: {e}")
        return {"status": "error", "error": str(e)}

    resolved_arguments = {**(arguments or {}), **preset_arguments}

    if config.get("forward_runtime_identity") is True:
        if runtime_identity is None:
            return {
                "status": "error",
                "error": "Runtime identity is required for this HTTP tool",
            }
        headers[RUNTIME_IDENTITY_HEADER] = runtime_identity.as_header_value()

    registration_id = None
    if late_terminal is not None:
        if runtime_identity is None:
            return {
                "status": "error",
                "error": "Runtime identity is required for this HTTP tool",
            }
        registration_id = late_terminal_registration_id(runtime_identity)
        headers[LATE_TERMINAL_HEADER] = json.dumps(
            {"version": 1, "registration_id": registration_id},
            sort_keys=True,
            separators=(",", ":"),
        )

    # Build request: JSON body for POST/PUT/PATCH, query params for GET/DELETE
    body = None
    params = None
    if method in ("POST", "PUT", "PATCH"):
        body = resolved_arguments
    elif method in ("GET", "DELETE") and resolved_arguments:
        params = resolved_arguments

    logger.info(
        f"Executing custom tool '{tool.name}' ({tool.tool_uuid}): {method} {url}"
    )
    if preset_arguments:
        logger.debug(
            f"Resolved preset parameters for '{tool.name}': {list(preset_arguments.keys())}"
        )
    logger.debug(f"Request body: {body}, params: {params}")

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body,
                params=params,
            )

            # Try to parse JSON response
            try:
                response_data = response.json()
            except Exception:
                response_data = {"raw_response": response.text}

            if response.status_code == 202 and late_terminal is not None:
                if response_data != {
                    "version": 1,
                    "status": "pending",
                    "registration_id": registration_id,
                }:
                    return {
                        "status": "error",
                        "error": "Invalid late-terminal pending response",
                    }
                return {
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                }

            result = {
                "status": "success",
                "status_code": response.status_code,
                "data": response_data,
            }

            logger.debug(
                f"Custom tool '{tool.name}' completed with status {response.status_code}"
            )
            return result

    except httpx.TimeoutException:
        logger.error(f"Custom tool '{tool.name}' timed out after {timeout_seconds}s")
        return {
            "status": "error",
            "error": f"Request timed out after {timeout_seconds} seconds",
        }
    except httpx.RequestError as e:
        logger.error(f"Custom tool '{tool.name}' request failed: {e}")
        return {
            "status": "error",
            "error": f"Request failed: {str(e)}",
        }
    except Exception as e:
        logger.error(f"Custom tool '{tool.name}' execution failed: {e}")
        return {
            "status": "error",
            "error": f"Tool execution failed: {str(e)}",
        }
