"""Tests for custom tool integration with PipecatEngine.

This module tests:
1. tool_to_function_schema - converting tool models to LLM function schemas
2. execute_http_tool - executing HTTP API tools
3. CustomToolManager - tool registration and handler execution
4. End-to-end LLM generation with custom tool calls
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    FunctionCallCancelFrame,
    FunctionCallFromLLM,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsFromLLMInfoFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    UserTurnInferenceCompletedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

from api.services.workflow.pipecat_engine_custom_tools import get_function_schema
from api.services.workflow.tools import custom_tool as custom_tool_module
from api.services.workflow.tools.custom_tool import (
    _coerce_parameter_value,
    execute_http_tool,
    tool_to_function_schema,
)
from pipecat.tests import MockLLMService, run_test


@dataclass
class MockToolModel:
    """Mock tool model for testing."""

    tool_uuid: str
    name: str
    description: str
    category: str
    definition: Dict[str, Any]


class TestToolToFunctionSchema:
    """Tests for tool_to_function_schema function."""

    def test_simple_tool_with_string_parameter(self):
        """Test converting a simple tool with one string parameter."""
        tool = MockToolModel(
            tool_uuid="test-uuid-1",
            name="Get Weather",
            description="Get current weather for a location",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "GET",
                    "url": "https://api.weather.com/current",
                    "parameters": [
                        {
                            "name": "location",
                            "type": "string",
                            "description": "City name",
                            "required": True,
                        }
                    ],
                },
            },
        )

        schema = tool_to_function_schema(tool)

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "get_weather"
        assert schema["function"]["description"] == "Get current weather for a location"
        assert schema["function"]["parameters"]["type"] == "object"
        assert "location" in schema["function"]["parameters"]["properties"]
        assert (
            schema["function"]["parameters"]["properties"]["location"]["type"]
            == "string"
        )
        assert (
            schema["function"]["parameters"]["properties"]["location"]["description"]
            == "City name"
        )
        assert "location" in schema["function"]["parameters"]["required"]
        assert schema["_tool_uuid"] == "test-uuid-1"

    def test_tool_with_multiple_parameter_types(self):
        """Test converting a tool with string, number, and boolean parameters."""
        tool = MockToolModel(
            tool_uuid="test-uuid-2",
            name="Book Appointment",
            description="Book an appointment with the service",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/appointments",
                    "parameters": [
                        {
                            "name": "customer_name",
                            "type": "string",
                            "description": "Customer's full name",
                            "required": True,
                        },
                        {
                            "name": "duration_minutes",
                            "type": "number",
                            "description": "Appointment duration in minutes",
                            "required": True,
                        },
                        {
                            "name": "is_priority",
                            "type": "boolean",
                            "description": "Whether this is a priority appointment",
                            "required": False,
                        },
                    ],
                },
            },
        )

        schema = tool_to_function_schema(tool)

        props = schema["function"]["parameters"]["properties"]
        assert props["customer_name"]["type"] == "string"
        assert props["duration_minutes"]["type"] == "number"
        assert props["is_priority"]["type"] == "boolean"

        required = schema["function"]["parameters"]["required"]
        assert "customer_name" in required
        assert "duration_minutes" in required
        assert "is_priority" not in required

    def test_tool_with_object_and_array_parameters(self):
        """Test converting a tool with object and array parameters."""
        tool = MockToolModel(
            tool_uuid="test-uuid-nested",
            name="Create Booking",
            description="Create a booking with nested details",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/bookings",
                    "parameters": [
                        {
                            "name": "booking",
                            "type": "object",
                            "description": "Nested booking payload",
                            "required": True,
                        },
                        {
                            "name": "attendees",
                            "type": "array",
                            "description": "Booking attendees",
                            "required": False,
                        },
                    ],
                },
            },
        )

        schema = tool_to_function_schema(tool)

        props = schema["function"]["parameters"]["properties"]
        assert props["booking"] == {
            "type": "object",
            "additionalProperties": True,
            "description": "Nested booking payload",
        }
        assert props["attendees"] == {
            "type": "array",
            "items": {},
            "description": "Booking attendees",
        }

    def test_preset_parameters_are_not_exposed_to_llm_schema(self):
        """Test that preset parameters are injected at runtime, not shown to the LLM."""
        tool = MockToolModel(
            tool_uuid="test-uuid-preset",
            name="Lookup Customer",
            description="Lookup a customer using contextual identifiers",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/customers/lookup",
                    "parameters": [
                        {
                            "name": "customer_name",
                            "type": "string",
                            "description": "Customer name spoken by the caller",
                            "required": True,
                        }
                    ],
                    "preset_parameters": [
                        {
                            "name": "phone_number",
                            "type": "string",
                            "value_template": "{{initial_context.phone_number}}",
                            "required": True,
                        }
                    ],
                },
            },
        )

        schema = tool_to_function_schema(tool)
        props = schema["function"]["parameters"]["properties"]

        assert "customer_name" in props
        assert "phone_number" not in props

    def test_tool_name_sanitization(self):
        """Test that tool names with special characters are sanitized."""
        tool = MockToolModel(
            tool_uuid="test-uuid-3",
            name="Get User's Account Info!!!",
            description="Get account information",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "GET",
                    "url": "https://api.example.com/account",
                    "parameters": [],
                },
            },
        )

        schema = tool_to_function_schema(tool)

        # Name should be lowercase with underscores only
        assert schema["function"]["name"] == "get_user_s_account_info"

    def test_tool_with_no_parameters(self):
        """Test converting a tool with no parameters."""
        tool = MockToolModel(
            tool_uuid="test-uuid-4",
            name="Ping Server",
            description="Check if server is alive",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "GET",
                    "url": "https://api.example.com/ping",
                },
            },
        )

        schema = tool_to_function_schema(tool)

        assert schema["function"]["parameters"]["properties"] == {}
        assert schema["function"]["parameters"]["required"] == []

    def test_tool_without_description_uses_fallback(self):
        """Test that tools without description use fallback."""
        tool = MockToolModel(
            tool_uuid="test-uuid-5",
            name="My Tool",
            description=None,
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/tool",
                },
            },
        )

        schema = tool_to_function_schema(tool)

        assert schema["function"]["description"] == "Execute My Tool tool"


class TestExecuteHttpTool:
    """Tests for execute_http_tool function."""

    @staticmethod
    def _runtime_identity():
        return custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id="call_custom_123",
            workflow_run_id="42",
            tool_uuid="tool-uuid",
            agent_scope="jeeves_windows",
        )

    @staticmethod
    def _identity_tool(method: str, **config_overrides: Any):
        return MockToolModel(
            tool_uuid="tool-uuid",
            name="Windows Tool",
            description="Call a Windows-owned tool",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": method,
                    "url": "https://windows.example.com/tool",
                    "forward_runtime_identity": True,
                    **config_overrides,
                },
            },
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
    async def test_post_family_forwards_reserved_runtime_identity_header(self, method):
        """Opted-in body methods carry runtime identity without changing arguments."""
        tool = self._identity_tool(method)
        arguments = {"app_id": "app:v1:spotify"}

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(status_code=200)
            mock_response.json.return_value = {"status": "ok"}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await execute_http_tool(
                tool,
                arguments,
                runtime_identity=self._runtime_identity(),
            )

            request = mock_client.request.call_args.kwargs
            assert request["json"] == arguments
            assert request["params"] is None
            assert request["headers"]["X-Dograh-Runtime-Identity"] == (
                '{"agent_scope":"jeeves_windows","tool_call_id":"call_custom_123",'
                '"tool_uuid":"tool-uuid","workflow_run_id":"42"}'
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    async def test_query_methods_forward_identity_in_method_safe_header(self, method):
        """Opted-in query methods carry identity without adding a request body."""
        tool = self._identity_tool(method)
        arguments = {"query": "spotify"}

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(status_code=200)
            mock_response.json.return_value = {"status": "ok"}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await execute_http_tool(
                tool,
                arguments,
                runtime_identity=self._runtime_identity(),
            )

            request = mock_client.request.call_args.kwargs
            assert request["json"] is None
            assert request["params"] == arguments
            assert "X-Dograh-Runtime-Identity" in request["headers"]

    @pytest.mark.asyncio
    async def test_runtime_identity_overrides_reserved_names_from_all_configurable_inputs(
        self,
    ):
        """Model, preset, and configured header values cannot forge runtime identity."""
        tool = self._identity_tool(
            "POST",
            headers={"X-Dograh-Runtime-Identity": "forged-header"},
            preset_parameters=[
                {
                    "name": "tool_call_id",
                    "type": "string",
                    "value_template": "forged-preset",
                    "required": True,
                }
            ],
        )
        arguments = {
            "tool_call_id": "forged-model",
            "workflow_run_id": "forged-model-run",
        }

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(status_code=200)
            mock_response.json.return_value = {"status": "ok"}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await execute_http_tool(
                tool,
                arguments,
                runtime_identity=self._runtime_identity(),
            )

            request = mock_client.request.call_args.kwargs
            assert request["json"]["tool_call_id"] == "forged-preset"
            assert request["json"]["workflow_run_id"] == "forged-model-run"
            assert request["headers"]["X-Dograh-Runtime-Identity"] == (
                '{"agent_scope":"jeeves_windows","tool_call_id":"call_custom_123",'
                '"tool_uuid":"tool-uuid","workflow_run_id":"42"}'
            )

    @pytest.mark.asyncio
    async def test_opted_in_tool_without_runtime_identity_fails_before_io(self):
        """An opted-in tool fails closed when stable runtime identity is absent."""
        tool = self._identity_tool("POST")

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            result = await execute_http_tool(tool, {"app_id": "app:v1:spotify"})

        assert result == {
            "status": "error",
            "error": "Runtime identity is required for this HTTP tool",
        }
        mock_client_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_late_terminal_202_is_internal_pending_not_a_tool_result(self):
        """A valid v1 pending reply is retained for polling, never surfaced as success."""
        capability = {
            "version": 1,
            "poll_url": "https://windows.example.com/late-terminal/v1/poll",
            "revoke_url": "https://windows.example.com/late-terminal/v1/revoke",
            "ack_url": "https://windows.example.com/late-terminal/v1/ack",
            "max_wait_ms": 60000,
            "poll_wait_ms": 5000,
        }
        tool = self._identity_tool("POST", late_terminal=capability)
        identity = self._runtime_identity()
        registration_id = (
            "ltr_v1_"
            + hashlib.sha256(
                f"1\n{identity.agent_scope}\n{identity.tool_call_id}\n{identity.tool_uuid}\n{identity.workflow_run_id}".encode()
            ).hexdigest()
        )

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(status_code=202)
            mock_response.json.return_value = {
                "version": 1,
                "status": "pending",
                "registration_id": registration_id,
            }
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await execute_http_tool(
                tool,
                {"app_id": "app:v1:spotify"},
                runtime_identity=identity,
            )

        assert result["status"] == "late_terminal_pending"
        assert result["registration_id"] == registration_id
        assert mock_client.request.call_args.kwargs["headers"][
            "X-Jeeves-Late-Terminal"
        ] == json.dumps(
            {"version": 1, "registration_id": registration_id},
            sort_keys=True,
            separators=(",", ":"),
        )

    @pytest.mark.asyncio
    async def test_late_terminal_poll_rejects_malformed_terminal_truth(self):
        """Only a gateway terminal object is eligible for the original callback."""
        capability = custom_tool_module.LateTerminalCapability(
            poll_url="https://windows.example.com/late-terminal/v1/poll",
            revoke_url="https://windows.example.com/late-terminal/v1/revoke",
            ack_url="https://windows.example.com/late-terminal/v1/ack",
            max_wait_ms=60_000,
            poll_wait_ms=5_000,
        )
        identity = self._runtime_identity()
        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(status_code=200)
            mock_response.json.return_value = {
                "version": 1,
                "status": "terminal",
                "terminal": ["not", "a", "terminal", "object"],
            }
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await custom_tool_module.poll_late_terminal(
                self._identity_tool("POST"),
                capability,
                identity,
                custom_tool_module.late_terminal_registration_id(identity),
                None,
            )

        assert result == {
            "status": "error",
            "error": "Invalid late-terminal poll response",
        }

    @pytest.mark.asyncio
    async def test_legacy_tool_retains_exact_request_shape_with_reserved_arguments(
        self,
    ):
        """A non-opted-in tool retains the exact pre-change mocked request shape."""
        tool = MockToolModel(
            tool_uuid="legacy-tool-uuid",
            name="Legacy Tool",
            description="Legacy request",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://legacy.example.com/tool",
                    "headers": {"X-Legacy": "preserved"},
                    "timeout_ms": 5000,
                },
            },
        )
        arguments = {
            "tool_call_id": "model-value",
            "workflow_run_id": "model-run",
        }

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(status_code=200)
            mock_response.json.return_value = {"status": "ok"}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await execute_http_tool(
                tool,
                arguments,
                runtime_identity=self._runtime_identity(),
            )

            mock_client.request.assert_awaited_once_with(
                method="POST",
                url="https://legacy.example.com/tool",
                headers={"X-Legacy": "preserved"},
                json=arguments,
                params=None,
            )

    @pytest.mark.asyncio
    async def test_post_request_sends_json_body(self):
        """Test that POST requests send arguments as JSON body."""
        tool = MockToolModel(
            tool_uuid="test-uuid",
            name="Create User",
            description="Create a new user",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/users",
                    "timeout_ms": 5000,
                },
            },
        )

        arguments = {"name": "John", "email": "john@example.com"}

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 201
            mock_response.json.return_value = {"id": 123, "name": "John"}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await execute_http_tool(tool, arguments)

            # Verify request was made with JSON body
            mock_client.request.assert_called_once()
            call_kwargs = mock_client.request.call_args.kwargs
            assert call_kwargs["method"] == "POST"
            assert call_kwargs["url"] == "https://api.example.com/users"
            assert call_kwargs["json"] == arguments
            assert call_kwargs["params"] is None

            assert result["status"] == "success"
            assert result["status_code"] == 201
            assert result["data"]["id"] == 123

    @pytest.mark.asyncio
    async def test_post_request_sends_nested_json_body(self):
        """Test that POST requests preserve nested arguments in the JSON body."""
        tool = MockToolModel(
            tool_uuid="test-uuid-nested",
            name="Create Booking",
            description="Create a nested booking",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/bookings",
                    "timeout_ms": 5000,
                },
            },
        )

        arguments = {
            "booking": {
                "start": "2026-05-28T10:00:00Z",
                "attendee": {"name": "Jane", "email": "jane@example.com"},
                "metadata": {"source": "voice"},
            }
        }

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"bookingId": "booking-123"}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await execute_http_tool(tool, arguments)

            call_kwargs = mock_client.request.call_args.kwargs
            assert call_kwargs["json"] == arguments
            assert isinstance(call_kwargs["json"]["booking"], dict)
            assert isinstance(call_kwargs["json"]["booking"]["attendee"], dict)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_post_request_injects_preset_parameters(self):
        """Test that preset parameters are resolved from runtime context."""
        tool = MockToolModel(
            tool_uuid="test-uuid-preset",
            name="Create Lead",
            description="Create a lead with caller context",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/leads",
                    "timeout_ms": 5000,
                    "preset_parameters": [
                        {
                            "name": "phone_number",
                            "type": "string",
                            "value_template": "{{initial_context.phone_number}}",
                            "required": True,
                        },
                        {
                            "name": "customer_id",
                            "type": "number",
                            "value_template": "{{gathered_context.customer_id}}",
                            "required": True,
                        },
                        {
                            "name": "is_vip",
                            "type": "boolean",
                            "value_template": "{{initial_context.is_vip}}",
                            "required": False,
                        },
                    ],
                },
            },
        )

        arguments = {"name": "John"}

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 201
            mock_response.json.return_value = {"id": 123}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await execute_http_tool(
                tool,
                arguments,
                call_context_vars={
                    "phone_number": "+14155550123",
                    "is_vip": "true",
                },
                gathered_context_vars={"customer_id": "42"},
            )

            call_kwargs = mock_client.request.call_args.kwargs
            assert call_kwargs["json"] == {
                "name": "John",
                "phone_number": "+14155550123",
                "customer_id": 42,
                "is_vip": True,
            }
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_missing_required_preset_parameter_returns_error(self):
        """Test that required preset parameters fail before the HTTP request."""
        tool = MockToolModel(
            tool_uuid="test-uuid-preset-error",
            name="Create Lead",
            description="Create a lead with caller context",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/leads",
                    "timeout_ms": 5000,
                    "preset_parameters": [
                        {
                            "name": "phone_number",
                            "type": "string",
                            "value_template": "{{initial_context.phone_number}}",
                            "required": True,
                        }
                    ],
                },
            },
        )

        result = await execute_http_tool(tool, {"name": "John"}, call_context_vars={})

        assert result["status"] == "error"
        assert "phone_number" in result["error"]

    @pytest.mark.asyncio
    async def test_get_request_sends_query_params(self):
        """Test that GET requests send arguments as query parameters."""
        tool = MockToolModel(
            tool_uuid="test-uuid",
            name="Search Users",
            description="Search for users",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "GET",
                    "url": "https://api.example.com/users/search",
                    "timeout_ms": 5000,
                },
            },
        )

        arguments = {"query": "john", "limit": 10}

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"users": []}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await execute_http_tool(tool, arguments)

            # Verify request was made with query params
            call_kwargs = mock_client.request.call_args.kwargs
            assert call_kwargs["method"] == "GET"
            assert call_kwargs["json"] is None
            assert call_kwargs["params"] == arguments

            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_delete_request_sends_query_params(self):
        """Test that DELETE requests send arguments as query parameters."""
        tool = MockToolModel(
            tool_uuid="test-uuid",
            name="Delete User",
            description="Delete a user",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "DELETE",
                    "url": "https://api.example.com/users",
                    "timeout_ms": 5000,
                },
            },
        )

        arguments = {"user_id": "123"}

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 204
            mock_response.json.return_value = {}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await execute_http_tool(tool, arguments)

            call_kwargs = mock_client.request.call_args.kwargs
            assert call_kwargs["method"] == "DELETE"
            assert call_kwargs["json"] is None
            assert call_kwargs["params"] == arguments

    @pytest.mark.asyncio
    async def test_timeout_error_handling(self):
        """Test that timeout errors are handled gracefully."""
        import httpx

        tool = MockToolModel(
            tool_uuid="test-uuid",
            name="Slow API",
            description="A slow API call",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/slow",
                    "timeout_ms": 1000,
                },
            },
        )

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request.side_effect = httpx.TimeoutException(
                "Request timed out"
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await execute_http_tool(tool, {})

            assert result["status"] == "error"
            assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_request_includes_custom_headers(self):
        """Test that custom headers are included in the request."""
        tool = MockToolModel(
            tool_uuid="test-uuid",
            name="API with Headers",
            description="API that requires headers",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/data",
                    "headers": {
                        "X-API-Key": "secret-key",
                        "X-Custom-Header": "custom-value",
                    },
                    "timeout_ms": 5000,
                },
            },
        )

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await execute_http_tool(tool, {"data": "test"})

            call_kwargs = mock_client.request.call_args.kwargs
            assert call_kwargs["headers"]["X-API-Key"] == "secret-key"
            assert call_kwargs["headers"]["X-Custom-Header"] == "custom-value"

    @pytest.mark.asyncio
    async def test_request_includes_auth_header_from_credential(self):
        """Test that auth headers from credentials are included in the request."""
        tool = MockToolModel(
            tool_uuid="test-uuid",
            name="Authenticated API",
            description="API that requires authentication",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/secure",
                    "credential_uuid": "cred-uuid-123",
                    "timeout_ms": 5000,
                },
            },
        )

        # Mock credential
        mock_credential = Mock()
        mock_credential.name = "API Token"
        mock_credential.credential_type = "bearer_token"
        mock_credential.credential_data = {"token": "my-secret-token"}

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("api.services.workflow.tools.custom_tool.db_client") as mock_db:
                mock_db.get_credential_by_uuid = AsyncMock(return_value=mock_credential)

                await execute_http_tool(tool, {"data": "test"}, organization_id=1)

                # Verify credential was fetched
                mock_db.get_credential_by_uuid.assert_called_once_with(
                    "cred-uuid-123", 1
                )

                # Verify auth header was added
                call_kwargs = mock_client.request.call_args.kwargs
                assert (
                    call_kwargs["headers"]["Authorization"] == "Bearer my-secret-token"
                )

    @pytest.mark.asyncio
    async def test_no_credential_lookup_without_organization_id(self):
        """Test that credential lookup is skipped without organization_id."""
        tool = MockToolModel(
            tool_uuid="test-uuid",
            name="API with Credential",
            description="API with credential configured",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/secure",
                    "credential_uuid": "cred-uuid-123",
                    "timeout_ms": 5000,
                },
            },
        )

        with patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("api.services.workflow.tools.custom_tool.db_client") as mock_db:
                # Call without organization_id
                await execute_http_tool(tool, {"data": "test"})

                # Verify credential lookup was NOT called
                mock_db.get_credential_by_uuid.assert_not_called()


class TestCoerceParameterValue:
    """Tests for _coerce_parameter_value function."""

    def test_object_value_returns_dict_unchanged(self):
        """Test that object parameters preserve dict values."""
        value = {"attendee": {"name": "Jane"}}

        assert _coerce_parameter_value(value, "object") is value

    def test_object_value_parses_json_string(self):
        """Test that object parameters parse JSON string values."""
        value = '{"attendee": {"name": "Jane"}}'

        assert _coerce_parameter_value(value, "object") == {
            "attendee": {"name": "Jane"}
        }

    def test_array_value_returns_list_unchanged(self):
        """Test that array parameters preserve list values."""
        value = [{"name": "Jane"}, {"name": "Sam"}]

        assert _coerce_parameter_value(value, "array") is value

    def test_array_value_parses_json_string(self):
        """Test that array parameters parse JSON string values."""
        value = '[{"name": "Jane"}, {"name": "Sam"}]'

        assert _coerce_parameter_value(value, "array") == [
            {"name": "Jane"},
            {"name": "Sam"},
        ]

    @pytest.mark.parametrize("value", ["not json", "[]", "null"])
    def test_object_value_rejects_invalid_or_wrong_shape(self, value):
        """Test that object parameters require a JSON object."""
        with pytest.raises(ValueError, match="Cannot convert"):
            _coerce_parameter_value(value, "object")

    @pytest.mark.parametrize("value", ["not json", "{}", "null"])
    def test_array_value_rejects_invalid_or_wrong_shape(self, value):
        """Test that array parameters require a JSON array."""
        with pytest.raises(ValueError, match="Cannot convert"):
            _coerce_parameter_value(value, "array")


class TestAuthHeaders:
    """Tests for auth header building utilities."""

    def test_bearer_token_auth(self):
        """Test building bearer token auth header."""
        from api.utils.credential_auth import build_auth_header

        mock_credential = Mock()
        mock_credential.credential_type = "bearer_token"
        mock_credential.credential_data = {"token": "abc123"}

        header = build_auth_header(mock_credential)

        assert header == {"Authorization": "Bearer abc123"}

    def test_api_key_auth(self):
        """Test building API key auth header."""
        from api.utils.credential_auth import build_auth_header

        mock_credential = Mock()
        mock_credential.credential_type = "api_key"
        mock_credential.credential_data = {
            "header_name": "X-API-Key",
            "api_key": "secret-key-123",
        }

        header = build_auth_header(mock_credential)

        assert header == {"X-API-Key": "secret-key-123"}

    def test_basic_auth(self):
        """Test building basic auth header."""
        import base64

        from api.utils.credential_auth import build_auth_header

        mock_credential = Mock()
        mock_credential.credential_type = "basic_auth"
        mock_credential.credential_data = {
            "username": "user",
            "password": "pass123",
        }

        header = build_auth_header(mock_credential)

        expected_encoded = base64.b64encode(b"user:pass123").decode()
        assert header == {"Authorization": f"Basic {expected_encoded}"}

    def test_custom_header_auth(self):
        """Test building custom header auth."""
        from api.utils.credential_auth import build_auth_header

        mock_credential = Mock()
        mock_credential.credential_type = "custom_header"
        mock_credential.credential_data = {
            "header_name": "X-Custom-Auth",
            "header_value": "custom-value-123",
        }

        header = build_auth_header(mock_credential)

        assert header == {"X-Custom-Auth": "custom-value-123"}

    def test_unknown_auth_type_returns_empty(self):
        """Test that unknown auth types return empty dict."""
        from api.utils.credential_auth import build_auth_header

        mock_credential = Mock()
        mock_credential.credential_type = "unknown_type"
        mock_credential.credential_data = {}

        header = build_auth_header(mock_credential)

        assert header == {}

    def test_none_credential_type_returns_empty(self):
        """Test that 'none' credential type returns empty dict."""
        from api.utils.credential_auth import build_auth_header

        mock_credential = Mock()
        mock_credential.credential_type = "none"
        mock_credential.credential_data = {}

        header = build_auth_header(mock_credential)

        assert header == {}

    def test_build_auth_header_from_data(self):
        """Test building auth header from raw data."""
        from api.utils.credential_auth import build_auth_header_from_data

        header = build_auth_header_from_data(
            credential_type="bearer_token",
            credential_data={"token": "my-token"},
        )

        assert header == {"Authorization": "Bearer my-token"}

    def test_api_key_default_header_name(self):
        """Test that API key uses default header name if not specified."""
        from api.utils.credential_auth import build_auth_header

        mock_credential = Mock()
        mock_credential.credential_type = "api_key"
        mock_credential.credential_data = {"api_key": "key123"}

        header = build_auth_header(mock_credential)

        assert header == {"X-API-Key": "key123"}


class TestCustomToolManagerIntegration:
    """Integration tests for CustomToolManager with MockLLMService."""

    @pytest.mark.asyncio
    async def test_llm_calls_custom_tool_handler(self):
        """Test that when LLM makes a function call, the custom tool handler is executed."""
        # Create function call chunks that simulate LLM calling a custom tool
        chunks = MockLLMService.create_function_call_chunks(
            function_name="book_appointment",
            arguments={"customer_name": "John Doe", "date": "2024-01-15"},
            tool_call_id="call_custom_123",
        )

        llm = MockLLMService(mock_chunks=chunks, chunk_delay=0.001)

        # Track if our handler was called
        handler_called = False
        received_arguments = None

        async def mock_book_appointment(params: FunctionCallParams):
            nonlocal handler_called, received_arguments
            handler_called = True
            received_arguments = params.arguments
            await params.result_callback({"status": "booked", "confirmation": "ABC123"})

        # Register the function handler
        llm.register_function("book_appointment", mock_book_appointment)

        # Create context and run
        messages = [
            {"role": "user", "content": "Book an appointment for John Doe on Jan 15"}
        ]
        context = LLMContext(messages)

        pipeline = Pipeline([llm])
        frames_to_send = [LLMContextFrame(context)]

        await run_test(
            pipeline,
            frames_to_send=frames_to_send,
            expected_down_frames=[
                LLMFullResponseStartFrame,
                FunctionCallsFromLLMInfoFrame,
                UserTurnInferenceCompletedFrame,
                FunctionCallsStartedFrame,
                LLMFullResponseEndFrame,
                FunctionCallInProgressFrame,
                FunctionCallResultFrame,
            ],
        )

        # Verify handler was called with correct arguments
        assert handler_called, "Custom tool handler should have been called"
        assert received_arguments == {"customer_name": "John Doe", "date": "2024-01-15"}

    @pytest.mark.asyncio
    async def test_multiple_custom_tools_can_be_registered(self):
        """Test that multiple custom tools can be registered and called."""
        # Create chunks for calling multiple tools
        functions = [
            {
                "name": "get_weather",
                "arguments": {"location": "NYC"},
                "tool_call_id": "call_weather",
            },
            {
                "name": "book_restaurant",
                "arguments": {"restaurant": "Tavern", "party_size": 4},
                "tool_call_id": "call_restaurant",
            },
        ]
        chunks = MockLLMService.create_multiple_function_call_chunks(functions)

        llm = MockLLMService(mock_chunks=chunks, chunk_delay=0.001)

        # Track calls
        calls_made = []

        async def mock_get_weather(params: FunctionCallParams):
            calls_made.append(("get_weather", params.arguments))
            await params.result_callback({"temp": 72, "condition": "sunny"})

        async def mock_book_restaurant(params: FunctionCallParams):
            calls_made.append(("book_restaurant", params.arguments))
            await params.result_callback({"confirmed": True})

        llm.register_function("get_weather", mock_get_weather)
        llm.register_function("book_restaurant", mock_book_restaurant)

        messages = [{"role": "user", "content": "Check weather and book restaurant"}]
        context = LLMContext(messages)

        pipeline = Pipeline([llm])
        await run_test(
            pipeline,
            frames_to_send=[LLMContextFrame(context)],
            expected_down_frames=None,
        )

        # Verify both handlers were called
        assert len(calls_made) == 2
        tool_names = [call[0] for call in calls_made]
        assert "get_weather" in tool_names
        assert "book_restaurant" in tool_names


class TestCustomToolManagerUnit:
    """Unit tests for CustomToolManager class."""

    @staticmethod
    def _identity_manager_and_tool():
        from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

        engine = Mock()
        engine._workflow_run_id = 42
        engine._call_context_vars = {"source": "voice"}
        engine._gathered_context = {"topic": "apps"}
        engine._fetch_recording_audio = None
        engine.is_call_disposed.return_value = False
        tool = MockToolModel(
            tool_uuid="windows-tool-uuid",
            name="Windows Open App",
            description="Open an app through Jeeves Windows",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://windows.example.com/tools/windows_open_app",
                    "forward_runtime_identity": True,
                    "agent_scope": "jeeves_windows",
                },
            },
        )
        manager = CustomToolManager(engine)
        manager.get_organization_id = AsyncMock(return_value=7)
        return manager, tool

    @staticmethod
    def _late_terminal_capability():
        return {
            "version": 1,
            "poll_url": "https://windows.example.com/late-terminal/v1/poll",
            "revoke_url": "https://windows.example.com/late-terminal/v1/revoke",
            "ack_url": "https://windows.example.com/late-terminal/v1/ack",
            "max_wait_ms": 60000,
            "poll_wait_ms": 1,
        }

    async def _run_http_handler_through_pipecat(
        self,
        executor,
        *,
        timeout_ms: int = 20,
    ):
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["timeout_ms"] = timeout_ms
        llm = MockLLMService()
        task_manager = TaskManager()
        task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
        llm._task_manager = task_manager
        llm._pipeline_worker = Mock(app_resources=None)
        llm._call_event_handler = AsyncMock()
        manager._engine.llm = llm

        handler, timeout_secs = manager._create_handler(tool, "windows_open_app")
        llm.register_function(
            "windows_open_app",
            handler,
            timeout_secs=timeout_secs,
        )

        frames = []
        terminal_frame = asyncio.Event()

        async def record_frame(frame_cls, **kwargs):
            frame = frame_cls(**kwargs)
            frames.append(frame)
            if isinstance(frame, FunctionCallResultFrame):
                terminal_frame.set()

        async def run_inline(runner_items):
            for runner_item in runner_items:
                await llm._run_function_call(runner_item)

        llm.broadcast_frame = record_frame
        llm._run_parallel_function_calls = run_inline
        llm._run_sequential_function_calls = run_inline

        call = FunctionCallFromLLM(
            function_name="windows_open_app",
            tool_call_id="call_terminal_123",
            arguments={"app_id": "app:v1:spotify"},
            context=LLMContext(),
        )

        async def run_with_executor():
            with patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                side_effect=executor,
            ):
                await llm.run_function_calls([call])

        task = asyncio.create_task(run_with_executor())
        return task, frames, terminal_frame, timeout_secs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result",
        [
            {"status": "success", "status_code": 200, "data": {"opened": True}},
            {"status": "error", "status_code": 504, "error": "gateway timeout"},
            {"status": "error", "error": "Request timed out after 0.02 seconds"},
        ],
    )
    async def test_registered_http_handler_emits_one_terminal_result(self, result):
        async def executor(**_kwargs):
            return result

        (
            task,
            frames,
            _terminal,
            _outer_timeout,
        ) = await self._run_http_handler_through_pipecat(executor)
        await task

        terminal = [f for f in frames if isinstance(f, FunctionCallResultFrame)]
        assert [f.result for f in terminal] == [result]

    @pytest.mark.asyncio
    async def test_registered_http_handler_preserves_caller_cancellation(self):
        started = asyncio.Event()

        async def executor(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        (
            task,
            frames,
            _terminal,
            _outer_timeout,
        ) = await self._run_http_handler_through_pipecat(executor)
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert not [f for f in frames if isinstance(f, FunctionCallResultFrame)]

    @pytest.mark.asyncio
    async def test_registered_http_handler_outer_deadline_emits_once(self):
        started = asyncio.Event()

        async def executor(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        (
            task,
            frames,
            _terminal,
            outer_timeout,
        ) = await self._run_http_handler_through_pipecat(executor)
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(task, timeout=1)

        results = [f.result for f in frames if isinstance(f, FunctionCallResultFrame)]
        assert results == [
            {"status": "error", "error": "Request timed out after 0.02 seconds"}
        ]
        assert outer_timeout > 0.02

    def test_opted_in_handler_outer_deadline_includes_one_registration_probe(self):
        """Pipecat cannot time out while the bounded post-timeout proof is still eligible."""
        manager, tool = self._identity_manager_and_tool()
        capability = self._late_terminal_capability()
        capability["poll_wait_ms"] = 5_000
        tool.definition["config"].update(
            timeout_ms=5_000,
            late_terminal=capability,
        )

        _handler, outer_timeout = manager._create_handler(tool, "windows_open_app")

        assert outer_timeout == pytest.approx(11.0)

    @pytest.mark.asyncio
    async def test_registered_http_handler_ignores_late_executor_completion(self):
        started = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()
        late_result = {"status": "success", "data": {"opened": True}}

        async def executor(**_kwargs):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            completed.set()
            return late_result

        (
            task,
            frames,
            terminal,
            _outer_timeout,
        ) = await self._run_http_handler_through_pipecat(executor)
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(terminal.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(completed.wait(), timeout=1)

        results = [f.result for f in frames if isinstance(f, FunctionCallResultFrame)]
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_registered_http_handler_deadline_race_is_single_terminal(self):
        for _ in range(20):

            async def executor(**_kwargs):
                await asyncio.sleep(0.01)
                return {"status": "success"}

            (
                task,
                frames,
                _terminal,
                _outer_timeout,
            ) = await self._run_http_handler_through_pipecat(
                executor,
                timeout_ms=10,
            )
            await asyncio.wait_for(task, timeout=1)
            results = [
                f.result for f in frames if isinstance(f, FunctionCallResultFrame)
            ]
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_http_handler_forwards_bounded_tool_call_and_workflow_run_identity(
        self,
    ):
        """The handler forwards a bounded identity instead of provider-owned data."""
        manager, tool = self._identity_manager_and_tool()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        result_callback = AsyncMock()
        params = Mock(
            tool_call_id="call_open_app_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=result_callback,
        )

        with patch(
            "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
            new_callable=AsyncMock,
            return_value={"status": "success", "data": {"opened": True}},
        ) as mock_execute:
            await handler(params)

        identity = mock_execute.await_args.kwargs["runtime_identity"]
        assert identity.tool_call_id == (
            "tcid:v1:" + hashlib.sha256(b"call_open_app_123").hexdigest()
        )
        assert "call_open_app_123" not in identity.as_header_value()
        assert identity.workflow_run_id == "42"
        assert identity.tool_uuid == "windows-tool-uuid"
        assert identity.agent_scope == "jeeves_windows"
        result_callback.assert_awaited_once_with(
            {"status": "success", "data": {"opened": True}}
        )

    @pytest.mark.asyncio
    async def test_late_terminal_observer_delivers_once_then_acks_after_context_acceptance(
        self,
    ):
        """Only a terminal poll reaches the original callback, and ack is context-gated."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        delivered = asyncio.Event()

        async def record_result(*_args, **_kwargs):
            delivered.set()

        result_callback = AsyncMock(side_effect=record_result)
        params = Mock(
            tool_call_id="call_late_terminal_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=result_callback,
        )
        terminal = {"status": "succeeded", "execution_id": "exec_123"}
        registration_id = custom_tool_module.late_terminal_registration_id(
            custom_tool_module.HttpToolRuntimeIdentity(
                tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                    params.tool_call_id
                ),
                workflow_run_id="42",
                tool_uuid="windows-tool-uuid",
                agent_scope="jeeves_windows",
            )
        )

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                create=True,
                new_callable=AsyncMock,
                return_value={"status": "terminal", "terminal": terminal},
            ),
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                create=True,
                new_callable=AsyncMock,
            ) as ack,
        ):
            await handler(params)
            await asyncio.wait_for(delivered.wait(), timeout=1)
            result_callback.assert_awaited_once()
            assert result_callback.await_args.args[0] == terminal
            properties = result_callback.await_args.kwargs["properties"]
            await properties.on_context_updated()
            ack.assert_awaited_once()

        await manager.cancel_late_terminal_observers()

    @pytest.mark.asyncio
    async def test_same_registration_replay_keeps_the_original_callback(self):
        """An active duplicate cannot replace the durable callback owner."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        first_delivered = asyncio.Event()

        async def record_first(*_args, **kwargs):
            await kwargs["properties"].on_context_updated()
            first_delivered.set()

        first_callback = AsyncMock(side_effect=record_first)
        replay_callback = AsyncMock()
        params = {
            "tool_call_id": "call_same_registration_123",
            "arguments": {"app_id": "app:v1:spotify"},
        }
        first_params = Mock(**params, result_callback=first_callback)
        replay_params = Mock(**params, result_callback=replay_callback)
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params["tool_call_id"]
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        terminal = {"status": "succeeded", "execution_id": "exec_replayed"}
        poll_started = asyncio.Event()
        release_terminal = asyncio.Event()

        async def delayed_terminal(*_args, **_kwargs):
            poll_started.set()
            await release_terminal.wait()
            return {"status": "terminal", "terminal": terminal}

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=delayed_terminal,
            ) as poll,
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                new_callable=AsyncMock,
            ) as ack,
        ):
            await handler(first_params)
            await asyncio.wait_for(poll_started.wait(), timeout=1)
            await handler(replay_params)
            release_terminal.set()
            await asyncio.wait_for(first_delivered.wait(), timeout=1)

        first_callback.assert_awaited_once()
        assert first_callback.await_args.args[0] == terminal
        replay_callback.assert_not_awaited()
        poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        ack.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert len(manager._late_terminal_completed) == 1
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_replay_does_not_supersede_an_entered_original_callback(self):
        """An in-flight duplicate lets the one original callback finish."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        tool_call_id = "call_inflight_replay_123"
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        terminal = {"status": "succeeded", "execution_id": "exec_inflight_replay"}
        original_entered = asyncio.Event()
        original_cancelled = asyncio.Event()
        release_original = asyncio.Event()
        original_completed = asyncio.Event()

        async def original_result(*_args, **kwargs):
            original_entered.set()
            try:
                await release_original.wait()
            except asyncio.CancelledError:
                original_cancelled.set()
                raise
            await kwargs["properties"].on_context_updated()
            original_completed.set()

        first_callback = AsyncMock(side_effect=original_result)
        replay_callback = AsyncMock()
        first_params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=first_callback,
        )
        replay_params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=replay_callback,
        )

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "status": "late_terminal_pending",
                        "registration_id": registration_id,
                    },
                    {
                        "status": "success",
                        "status_code": 200,
                        "data": terminal,
                    },
                ],
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                return_value={"status": "terminal", "terminal": terminal},
            ) as poll,
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                new_callable=AsyncMock,
            ) as ack,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ),
        ):
            try:
                await handler(first_params)
                await asyncio.wait_for(original_entered.wait(), timeout=1)
                await handler(replay_params)
                assert not original_cancelled.is_set()
                replay_callback.assert_not_awaited()
                release_original.set()
                await asyncio.wait_for(original_completed.wait(), timeout=1)
            finally:
                release_original.set()
                await manager.cancel_late_terminal_observers()

        first_callback.assert_awaited_once()
        assert first_callback.await_args.args[0] == terminal
        replay_callback.assert_not_awaited()
        assert not original_cancelled.is_set()
        poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        ack.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)

    @pytest.mark.asyncio
    async def test_replay_after_terminal_removal_uses_completed_tombstone(self):
        """An acknowledged registration cannot create another poll, callback, or ack."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        tool_call_id = "call_post_terminal_replay_123"
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        terminal = {"status": "succeeded", "execution_id": "exec_post_terminal"}
        release_terminal = asyncio.Event()
        first_completed = asyncio.Event()

        async def terminal_poll(*_args, **_kwargs):
            await release_terminal.wait()
            return {"status": "terminal", "terminal": terminal}

        async def first_result(*_args, **kwargs):
            await kwargs["properties"].on_context_updated()
            first_completed.set()

        first_callback = AsyncMock(side_effect=first_result)
        replay_callback = AsyncMock()
        params = {
            "tool_call_id": tool_call_id,
            "arguments": {"app_id": "app:v1:spotify"},
        }

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "status": "late_terminal_pending",
                        "registration_id": registration_id,
                    },
                    {
                        "status": "success",
                        "status_code": 410,
                        "data": {"status": "acknowledged"},
                    },
                ],
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=terminal_poll,
            ) as poll,
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                new_callable=AsyncMock,
            ) as ack,
        ):
            await handler(Mock(**params, result_callback=first_callback))
            first_observer = next(iter(manager._late_terminal_observers.values()))
            release_terminal.set()
            await asyncio.wait_for(first_completed.wait(), timeout=1)
            await asyncio.wait_for(first_observer.task, timeout=1)
            assert manager._late_terminal_observers == {}

            await handler(Mock(**params, result_callback=replay_callback))
            replay_tasks = [
                observer.task
                for observer in manager._late_terminal_observers.values()
                if observer.task is not None
            ]
            if replay_tasks:
                await asyncio.gather(*replay_tasks)

        first_callback.assert_awaited_once()
        replay_callback.assert_not_awaited()
        poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        ack.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert len(manager._late_terminal_completed) == 1

    @pytest.mark.asyncio
    async def test_concurrent_same_registration_reserves_one_observer_atomically(self):
        """Concurrent replays cannot create an orphan observer."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        first_callback = AsyncMock()
        replay_callback = AsyncMock()
        tool_call_id = "call_concurrent_registration_123"
        first_params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=first_callback,
        )
        replay_params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=replay_callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        organization_calls = 0
        reservation_waiters = 0
        release_reservation = asyncio.Event()

        async def synchronized_organization_id():
            nonlocal organization_calls, reservation_waiters
            organization_calls += 1
            if organization_calls > 2:
                reservation_waiters += 1
                if reservation_waiters == 2:
                    release_reservation.set()
                await release_reservation.wait()
            return 7

        manager.get_organization_id = AsyncMock(
            side_effect=synchronized_organization_id
        )
        release_poll = asyncio.Event()
        first_poll = asyncio.Event()
        terminal = {"status": "succeeded", "execution_id": "exec_concurrent"}

        async def delayed_terminal(*_args, **_kwargs):
            first_poll.set()
            await release_poll.wait()
            return {"status": "terminal", "terminal": terminal}

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=delayed_terminal,
            ) as poll,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ),
        ):
            try:
                await asyncio.gather(handler(first_params), handler(replay_params))
                await asyncio.wait_for(first_poll.wait(), timeout=1)
                for _ in range(20):
                    await asyncio.sleep(0)
                    if poll.await_count > 1:
                        break
                assert poll.await_count == 1
                assert len(manager._late_terminal_observers) == 1
            finally:
                release_poll.set()
                await asyncio.sleep(0.05)
                await manager.cancel_late_terminal_observers()

    @pytest.mark.asyncio
    async def test_duplicate_terminal_transport_stops_after_one_callback_and_one_accepted_context_ack(
        self,
    ):
        """A second terminal response cannot be consumed after the observer terminalizes."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        delivered = asyncio.Event()

        async def record_result(*_args, **_kwargs):
            delivered.set()

        callback = AsyncMock(side_effect=record_result)
        params = Mock(
            tool_call_id="call_duplicate_terminal_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        terminal = {"status": "succeeded", "execution_id": "exec_duplicate"}

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=[
                    {"status": "terminal", "terminal": terminal},
                    {"status": "terminal", "terminal": terminal},
                ],
            ) as poll,
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                new_callable=AsyncMock,
            ) as ack,
        ):
            await handler(params)
            await asyncio.wait_for(delivered.wait(), timeout=1)
            callback.assert_awaited_once()
            assert callback.await_args.args[0] == terminal
            properties = callback.await_args.kwargs["properties"]
            await properties.on_context_updated()
            await properties.on_context_updated()

        execute.assert_awaited_once()
        poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        ack.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_wrong_conversation_generation_cannot_install_an_observer(self):
        """A pending reply bound to another generation fails before polling."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        callback = AsyncMock()
        params = Mock(
            tool_call_id="call_wrong_generation_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        exact_identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        wrong_identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=exact_identity.tool_call_id,
            workflow_run_id="43",
            tool_uuid=exact_identity.tool_uuid,
            agent_scope=exact_identity.agent_scope,
        )
        wrong_registration = custom_tool_module.late_terminal_registration_id(
            wrong_identity
        )
        handler = manager._create_http_tool_handler(tool, "windows_open_app")

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": wrong_registration,
                },
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
            ) as poll,
        ):
            await handler(params)

        execute.assert_awaited_once()
        poll.assert_not_awaited()
        callback.assert_awaited_once_with(
            {"status": "error", "error": "Invalid late-terminal registration"}
        )
        assert wrong_registration != custom_tool_module.late_terminal_registration_id(
            exact_identity
        )
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("poll_error", "case"),
        [
            (
                "Invalid late-terminal poll response",
                "malformed_or_incompatible_response",
            ),
            ("Late-terminal registration missing", "missing_registration"),
        ],
    )
    async def test_observer_protocol_failure_revokes_without_success_or_second_action(
        self,
        poll_error,
        case,
    ):
        """Missing or malformed durable state fails closed on the original call."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        delivered = asyncio.Event()

        async def record_result(*_args, **_kwargs):
            delivered.set()

        callback = AsyncMock(side_effect=record_result)
        params = Mock(
            tool_call_id=f"call_{case}_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                return_value={"status": "error", "error": poll_error},
            ) as poll,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            await handler(params)
            await asyncio.wait_for(delivered.wait(), timeout=1)

        execute.assert_awaited_once()
        poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        callback.assert_awaited_once_with({"status": "error", "error": poll_error})
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_initial_timeout_probes_only_its_exact_durable_registration_and_fails_closed(
        self,
    ):
        """A timed-out opt-in request cannot install an observer before its registration is proven."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        tool.definition["config"]["timeout_ms"] = 1
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        result_callback = AsyncMock()
        params = Mock(
            tool_call_id="call_initial_timeout_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=result_callback,
        )
        expected_identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        expected_registration = custom_tool_module.late_terminal_registration_id(
            expected_identity
        )

        async def initial_request(**_kwargs):
            await asyncio.Event().wait()

        probe_started = asyncio.Event()
        release_probe = asyncio.Event()

        async def prove_registration(*_args, **_kwargs):
            probe_started.set()
            await release_probe.wait()
            return {
                "status": "error",
                "error": "Invalid late-terminal poll response",
            }

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=initial_request,
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=prove_registration,
            ) as poll,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            handler_task = asyncio.create_task(handler(params))
            await asyncio.wait_for(probe_started.wait(), timeout=1)
            handler_was_waiting_for_proof = not handler_task.done()
            release_probe.set()
            await handler_task
            await manager.cancel_late_terminal_observers()

        assert handler_was_waiting_for_proof

        execute.assert_awaited_once()
        poll.assert_awaited_once_with(
            tool,
            ANY,
            expected_identity,
            expected_registration,
            7,
        )
        result_callback.assert_awaited_once_with(
            {
                "status": "error",
                "error": "Late-terminal registration was not proven",
            }
        )
        revoke.assert_awaited_once_with(
            tool,
            ANY,
            expected_identity,
            expected_registration,
            7,
        )
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_non_opted_in_timeout_preserves_legacy_result_without_poll_or_fallback(
        self,
    ):
        """Missing v1 metadata keeps the original one-request timeout behavior."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["timeout_ms"] = 1
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        params = Mock(
            tool_call_id="call_legacy_timeout_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )

        async def initial_request(**_kwargs):
            await asyncio.Event().wait()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=initial_request,
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
            ) as poll,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            await handler(params)

        execute.assert_awaited_once()
        poll.assert_not_awaited()
        revoke.assert_not_awaited()
        callback.assert_awaited_once_with(
            {"status": "error", "error": "Request timed out after 0.001 seconds"}
        )
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_initial_timeout_terminal_proof_acks_only_after_context_acceptance(
        self,
    ):
        """A terminal registration probe uses the same context-gated delivery as an observer."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"].update(
            late_terminal=self._late_terminal_capability(),
            timeout_ms=1,
        )
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        delivered = asyncio.Event()

        async def record_result(*_args, **_kwargs):
            delivered.set()

        callback = AsyncMock(side_effect=record_result)
        params = Mock(
            tool_call_id="call_initial_timeout_terminal_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        terminal = {"status": "failed", "reason": "durable denial"}

        async def initial_request(**_kwargs):
            await asyncio.Event().wait()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=initial_request,
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                return_value={"status": "terminal", "terminal": terminal},
            ) as poll,
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                new_callable=AsyncMock,
            ) as ack,
        ):
            await handler(params)
            await asyncio.wait_for(delivered.wait(), timeout=1)
            callback.assert_awaited_once()
            assert callback.await_args.args[0] == terminal
            properties = callback.await_args.kwargs["properties"]
            ack.assert_not_awaited()
            await properties.on_context_updated()
            await properties.on_context_updated()

        execute.assert_awaited_once()
        poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        ack.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_opted_in_fast_terminal_uses_original_callback_without_observer_or_transport_effects(
        self,
    ):
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        callback = AsyncMock()
        params = Mock(
            tool_call_id="call_fast_123", arguments={}, result_callback=callback
        )
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        result = {"status": "succeeded", "execution_id": "exec_fast"}
        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value=result,
            ) as execute,
            patch.object(
                custom_tool_module, "poll_late_terminal", new_callable=AsyncMock
            ) as poll,
            patch.object(
                custom_tool_module, "ack_late_terminal", new_callable=AsyncMock
            ) as ack,
            patch.object(
                custom_tool_module, "revoke_late_terminal", new_callable=AsyncMock
            ) as revoke,
        ):
            await handler(params)
        callback.assert_awaited_once_with(result)
        execute.assert_awaited_once()
        poll.assert_not_awaited()
        ack.assert_not_awaited()
        revoke.assert_not_awaited()
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_initial_timeout_proves_pending_then_observes_only_the_same_registration(
        self,
    ):
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"].update(
            late_terminal=self._late_terminal_capability(), timeout_ms=1
        )
        delivered = asyncio.Event()

        async def record_result(*_args, **_kwargs):
            delivered.set()

        callback = AsyncMock(side_effect=record_result)
        params = Mock(
            tool_call_id="call_timeout_pending_123",
            arguments={},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)

        async def initial_request(**_kwargs):
            await asyncio.Event().wait()

        terminal = {"status": "succeeded", "execution_id": "exec_timeout"}
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=initial_request,
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=[
                    {"status": "pending"},
                    {"status": "terminal", "terminal": terminal},
                ],
            ) as poll,
        ):
            await handler(params)
            callback.assert_not_awaited()
            await asyncio.wait_for(delivered.wait(), timeout=1)
        callback.assert_awaited_once()
        assert callback.await_args.args[0] == terminal
        execute.assert_awaited_once()
        assert [call.args[3] for call in poll.await_args_list] == [
            registration_id,
            registration_id,
        ]
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_new_turn_callback_cannot_claim_the_original_late_terminal_result(
        self,
    ):
        """A newer tool call receives only its own result; the late result stays on the pinned callback."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        old_delivered = asyncio.Event()
        release_old = asyncio.Event()

        async def record_old(*_args, **_kwargs):
            old_delivered.set()

        old_callback = AsyncMock(side_effect=record_old)
        new_callback = AsyncMock()
        old_params = Mock(
            tool_call_id="call_superseded_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=old_callback,
        )
        new_params = Mock(
            tool_call_id="call_new_turn_456",
            arguments={"app_id": "app:v1:calculator"},
            result_callback=new_callback,
        )
        old_identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                old_params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        old_registration = custom_tool_module.late_terminal_registration_id(
            old_identity
        )
        old_terminal = {"status": "failed", "reason": "original call denied"}
        new_terminal = {"status": "succeeded", "execution_id": "exec_new_turn"}

        async def poll_original(*_args, **_kwargs):
            await release_old.wait()
            return {"status": "terminal", "terminal": old_terminal}

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "status": "late_terminal_pending",
                        "registration_id": old_registration,
                    },
                    new_terminal,
                ],
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=poll_original,
            ) as poll,
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                new_callable=AsyncMock,
            ) as ack,
        ):
            await handler(old_params)
            await handler(new_params)
            new_callback.assert_awaited_once_with(new_terminal)
            old_callback.assert_not_awaited()
            release_old.set()
            await asyncio.wait_for(old_delivered.wait(), timeout=1)

        execute.assert_awaited()
        assert execute.await_count == 2
        poll.assert_awaited_once_with(tool, ANY, old_identity, old_registration, 7)
        old_callback.assert_awaited_once()
        assert old_callback.await_args.args[0] == old_terminal
        assert old_callback.await_args.kwargs["properties"].on_context_updated
        new_callback.assert_awaited_once_with(new_terminal)
        ack.assert_not_awaited()
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_pipecat_supersession_drops_original_result_before_context_ack(
        self,
    ):
        """The pinned callback's frame is rejected after its exact call leaves the live map."""
        from pipecat.frames.frames import (
            FunctionCallCancelFrame,
            FunctionCallResultProperties,
        )
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMAssistantAggregator,
        )

        context = LLMContext()
        aggregator = LLMAssistantAggregator(context)
        accepted = AsyncMock()
        tool_call_id = "call_superseded_context_123"
        await aggregator._handle_function_call_in_progress(
            FunctionCallInProgressFrame(
                function_name="windows_open_app",
                tool_call_id=tool_call_id,
                arguments={"app_id": "app:v1:spotify"},
                cancel_on_interruption=True,
            )
        )
        await aggregator._handle_function_call_cancel(
            FunctionCallCancelFrame(
                function_name="windows_open_app",
                tool_call_id=tool_call_id,
            )
        )
        messages_after_supersession = list(context.messages)

        await aggregator._handle_function_call_result(
            FunctionCallResultFrame(
                function_name="windows_open_app",
                tool_call_id=tool_call_id,
                arguments={"app_id": "app:v1:spotify"},
                result={"status": "succeeded", "execution_id": "exec_stale"},
                properties=FunctionCallResultProperties(on_context_updated=accepted),
            )
        )

        assert context.messages == messages_after_supersession
        accepted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unrelated_tool_activity_keeps_late_result_and_ack_on_original_identity(
        self,
    ):
        """An unrelated callback cannot receive or acknowledge the original terminal."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        original_handler = manager._create_http_tool_handler(tool, "windows_open_app")
        unrelated_handler = manager._create_http_tool_handler(tool, "windows_find_app")
        delivered = asyncio.Event()

        async def record_original(*_args, **_kwargs):
            delivered.set()

        original_callback = AsyncMock(side_effect=record_original)
        unrelated_callback = AsyncMock()
        original_params = Mock(
            tool_call_id="call_original_activity_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=original_callback,
        )
        unrelated_params = Mock(
            tool_call_id="call_unrelated_activity_456",
            arguments={"query": "calculator"},
            result_callback=unrelated_callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                original_params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        original_terminal = {
            "status": "partial",
            "detail": "original action partially completed",
        }
        unrelated_terminal = {"status": "succeeded", "apps": ["calculator"]}

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "status": "late_terminal_pending",
                        "registration_id": registration_id,
                    },
                    unrelated_terminal,
                ],
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                return_value={
                    "status": "terminal",
                    "terminal": original_terminal,
                },
            ),
            patch.object(
                custom_tool_module,
                "ack_late_terminal",
                new_callable=AsyncMock,
            ) as ack,
        ):
            await original_handler(original_params)
            await unrelated_handler(unrelated_params)
            await asyncio.wait_for(delivered.wait(), timeout=1)
            properties = original_callback.await_args.kwargs["properties"]
            await properties.on_context_updated()

        original_callback.assert_awaited_once()
        assert original_callback.await_args.args[0] == original_terminal
        unrelated_callback.assert_awaited_once_with(unrelated_terminal)
        ack.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal",
        [
            pytest.param(
                {"status": "succeeded", "execution_id": "exec_fast"}, id="late_success"
            ),
            pytest.param({"status": "failed", "reason": "denied"}, id="late_failure"),
            pytest.param(
                {"status": "partial", "detail": "some work completed"},
                id="late_partial",
            ),
            pytest.param(
                {"status": "deadline_after_effect"}, id="deadline_after_effect"
            ),
            pytest.param(
                {"status": "unknown", "reason": "indeterminate"}, id="indeterminate"
            ),
        ],
    )
    async def test_late_terminal_delivery_preserves_exact_windows_truth_and_acks_once(
        self, terminal
    ):
        """Every terminal family reaches only its originating callback without translation."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        delivered = asyncio.Event()

        async def record_result(*_args, **_kwargs):
            delivered.set()

        callback = AsyncMock(side_effect=record_result)
        params = Mock(
            tool_call_id="call_terminal_matrix_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                return_value={"status": "terminal", "terminal": terminal},
            ) as poll,
            patch.object(
                custom_tool_module, "ack_late_terminal", new_callable=AsyncMock
            ) as ack,
        ):
            await handler(params)
            await asyncio.wait_for(delivered.wait(), timeout=1)
            callback.assert_awaited_once()
            assert callback.await_args.args[0] == terminal
            properties = callback.await_args.kwargs["properties"]
            await properties.on_context_updated()
            await properties.on_context_updated()
            ack.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
            execute.assert_awaited_once()
            poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        await manager.cancel_late_terminal_observers()

    @pytest.mark.asyncio
    async def test_function_call_cancel_frame_revokes_only_its_exact_observer(self):
        """Pipeline cancellation suppresses and revokes the exact detached callback."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        unrelated_callback = AsyncMock()
        tool_call_id = "call_exact_cancel_123"
        unrelated_tool_call_id = "call_unrelated_cancel_456"
        params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        unrelated_params = Mock(
            tool_call_id=unrelated_tool_call_id,
            arguments={"app_id": "app:v1:calculator"},
            result_callback=unrelated_callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        unrelated_identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                unrelated_tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        unrelated_registration_id = (
            custom_tool_module.late_terminal_registration_id(unrelated_identity)
        )
        polling = asyncio.Event()
        poll_count = 0

        async def pending_poll(*_args, **_kwargs):
            nonlocal poll_count
            poll_count += 1
            if poll_count == 2:
                polling.set()
            await asyncio.Event().wait()

        async def pending_execution(**kwargs):
            return {
                "status": "late_terminal_pending",
                "registration_id": custom_tool_module.late_terminal_registration_id(
                    kwargs["runtime_identity"]
                ),
            }

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=pending_execution,
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=pending_poll,
            ),
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            try:
                await handler(params)
                await handler(unrelated_params)
                await asyncio.wait_for(polling.wait(), timeout=1)
                assert manager._engine.task.add_observer.call_count == 1
                cancellation_observer = (
                    manager._engine.task.add_observer.call_args.args[0]
                )
                await cancellation_observer.on_push_frame(
                    Mock(
                        frame=FunctionCallCancelFrame(
                            function_name="windows_open_app",
                            tool_call_id=tool_call_id,
                        )
                    )
                )
                assert len(manager._late_terminal_observers) == 1
                remaining_observer = next(
                    iter(manager._late_terminal_observers.values())
                )
                assert (
                    remaining_observer.tool_call_id
                    == unrelated_identity.tool_call_id
                )
                revoke.assert_awaited_once_with(
                    tool, ANY, identity, registration_id, 7
                )
                await manager.cancel_late_terminal_observers()
            finally:
                await manager.cancel_late_terminal_observers()

        callback.assert_not_awaited()
        unrelated_callback.assert_not_awaited()
        assert [item.args for item in revoke.await_args_list] == [
            (tool, ANY, identity, registration_id, 7),
            (tool, ANY, unrelated_identity, unrelated_registration_id, 7),
        ]
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_user_interruption_revokes_and_replay_cannot_revive_the_observer(self):
        """An interrupted registration remains an ineligible duplicate tombstone."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        tool_call_id = "call_interrupted_after_detach_123"
        params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        replay_callback = AsyncMock()
        replay_params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=replay_callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        polling = asyncio.Event()

        async def pending_poll(*_args, **_kwargs):
            polling.set()
            await asyncio.Event().wait()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "status": "late_terminal_pending",
                        "registration_id": registration_id,
                    },
                    {
                        "status": "success",
                        "status_code": 410,
                        "data": {"status": "revoked"},
                    },
                ],
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=pending_poll,
            ) as poll,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            try:
                await handler(params)
                await asyncio.wait_for(polling.wait(), timeout=1)
                cancellation_observer = (
                    manager._engine.task.add_observer.call_args.args[0]
                )
                await cancellation_observer.on_push_frame(
                    Mock(frame=InterruptionFrame())
                )
                assert manager._late_terminal_observers == {}
                revoke.assert_awaited_once_with(
                    tool, ANY, identity, registration_id, 7
                )
                await handler(replay_params)
                await asyncio.sleep(0)
                assert manager._late_terminal_observers == {}
                assert len(manager._late_terminal_completed) == 1
                assert poll.await_count == 1
                revoke.assert_awaited_once_with(
                    tool, ANY, identity, registration_id, 7
                )
            finally:
                await manager.cancel_late_terminal_observers()

        callback.assert_not_awaited()
        replay_callback.assert_not_awaited()
        assert poll.await_count == 1
        revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_shutdown_racing_pending_registration_revokes_after_unblock(self):
        """A handler cannot publish an observer after final manager shutdown."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        tool_call_id = "call_shutdown_registration_race_123"
        params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        execute_started = asyncio.Event()
        release_execute = asyncio.Event()

        async def blocked_execute(*_args, **_kwargs):
            execute_started.set()
            await release_execute.wait()
            return {
                "status": "late_terminal_pending",
                "registration_id": registration_id,
            }

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=blocked_execute,
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
            ) as poll,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            handler_task = asyncio.create_task(handler(params))
            await asyncio.wait_for(execute_started.wait(), timeout=1)
            try:
                await manager.close_late_terminal_observers()
            finally:
                release_execute.set()
                await handler_task
                await manager.cancel_late_terminal_observers()

        callback.assert_not_awaited()
        poll.assert_not_awaited()
        revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_cleanup_cancels_suspended_registration_proof_before_callback(self):
        """Cleanup owns a suspended proof and prevents post-close terminal delivery."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"].update(
            late_terminal=self._late_terminal_capability(),
            timeout_ms=1,
        )
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        tool_call_id = "call_cleanup_suspended_proof_123"
        params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=AsyncMock(),
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        proof_started = asyncio.Event()
        release_proof = asyncio.Event()
        terminal = {"status": "succeeded", "execution_id": "exec_after_cleanup"}

        async def initial_request(**_kwargs):
            await asyncio.Event().wait()

        async def suspended_proof(*_args, **_kwargs):
            proof_started.set()
            await release_proof.wait()
            return {"status": "terminal", "terminal": terminal}

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=initial_request,
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=suspended_proof,
            ),
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            handler_task = asyncio.create_task(handler(params))
            await asyncio.wait_for(proof_started.wait(), timeout=1)
            try:
                await asyncio.wait_for(
                    manager.close_late_terminal_observers(),
                    timeout=0.2,
                )
                release_proof.set()
                await asyncio.wait_for(handler_task, timeout=1)
            finally:
                release_proof.set()
                if not handler_task.done():
                    handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)

        params.result_callback.assert_not_awaited()
        revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_cleanup_bounds_proof_failure_revoke_and_suppresses_callback(self):
        """A proof-phase revoke uses the shared budget and remains cleanup-owned."""
        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"].update(
            late_terminal=self._late_terminal_capability(),
            timeout_ms=1,
        )
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        tool_call_id = "call_cleanup_proof_revoke_123"
        params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=AsyncMock(),
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        proof_started = asyncio.Event()
        release_proof = asyncio.Event()
        revoke_started = asyncio.Event()
        release_revoke = asyncio.Event()

        async def initial_request(**_kwargs):
            await asyncio.Event().wait()

        async def failed_proof(*_args, **_kwargs):
            proof_started.set()
            await release_proof.wait()
            return {"status": "error", "error": "registration missing"}

        async def blocked_revoke(*_args, **_kwargs):
            revoke_started.set()
            await release_revoke.wait()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=initial_request,
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=failed_proof,
            ),
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
                side_effect=blocked_revoke,
            ) as revoke,
        ):
            handler_task = asyncio.create_task(handler(params))
            await asyncio.wait_for(proof_started.wait(), timeout=1)
            release_proof.set()
            await asyncio.wait_for(revoke_started.wait(), timeout=1)
            try:
                await asyncio.wait_for(
                    manager.close_late_terminal_observers(),
                    timeout=0.2,
                )
                await asyncio.wait_for(handler_task, timeout=0.2)
            finally:
                release_revoke.set()
                if not handler_task.done():
                    handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)

        params.result_callback.assert_not_awaited()
        revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_cleanup_bounds_inflight_revoke_and_never_retries_it(self):
        """Cleanup is independent of transport wait and cannot duplicate revoke."""
        manager, tool = self._identity_manager_and_tool()
        capability = self._late_terminal_capability()
        capability.update(max_wait_ms=60_000, poll_wait_ms=60_000)
        tool.definition["config"]["late_terminal"] = capability
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        tool_call_id = "call_cleanup_bound_123"
        params = Mock(
            tool_call_id=tool_call_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(tool_call_id),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        revoke_started = asyncio.Event()
        release_revoke = asyncio.Event()

        async def blocked_revoke(*_args, **_kwargs):
            revoke_started.set()
            await release_revoke.wait()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                return_value={"status": "error", "error": "protocol failure"},
            ),
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
                side_effect=blocked_revoke,
            ) as revoke,
        ):
            try:
                await handler(params)
                await asyncio.wait_for(revoke_started.wait(), timeout=1)
                started_at = asyncio.get_running_loop().time()
                await asyncio.wait_for(
                    manager.cancel_late_terminal_observers(),
                    timeout=0.2,
                )
                assert asyncio.get_running_loop().time() - started_at < 0.15
            finally:
                release_revoke.set()
                await manager.cancel_late_terminal_observers()

        callback.assert_not_awaited()
        revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_call_end_edge_revokes_observer_without_callback_or_revival(self):
        """PipecatEngine.end_call_with_reason reaches real observer cancellation."""
        from api.services.workflow.pipecat_engine import PipecatEngine

        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        params = Mock(
            tool_call_id="call_end_edge_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        polling = asyncio.Event()

        async def pending_poll(*_args, **_kwargs):
            polling.set()
            await asyncio.Event().wait()

        class EndCallObserved(Exception):
            pass

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=pending_poll,
            ),
            patch.object(
                custom_tool_module, "revoke_late_terminal", new_callable=AsyncMock
            ) as revoke,
        ):
            await handler(params)
            await asyncio.wait_for(polling.wait(), timeout=1)
            original_cancel = manager.close_late_terminal_observers

            async def cancel_then_stop():
                await original_cancel()
                raise EndCallObserved

            manager.close_late_terminal_observers = AsyncMock(
                side_effect=cancel_then_stop
            )
            manager._engine._call_disposed = False
            manager._engine._custom_tool_manager = manager
            with pytest.raises(EndCallObserved):
                await PipecatEngine.end_call_with_reason(
                    manager._engine, "caller_hangup"
                )

            callback.assert_not_awaited()
            revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
            assert manager._engine._call_disposed is True
            assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_provider_disconnect_cleanup_revokes_observer_without_callback(
        self,
    ):
        """PipecatEngine.cleanup reaches real observer cancellation on disconnect."""
        from api.services.workflow.pipecat_engine import PipecatEngine

        manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        params = Mock(
            tool_call_id="call_provider_disconnect_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        polling = asyncio.Event()

        async def pending_poll(*_args, **_kwargs):
            polling.set()
            await asyncio.Event().wait()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=pending_poll,
            ),
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            await handler(params)
            await asyncio.wait_for(polling.wait(), timeout=1)
            manager._engine._custom_tool_manager = manager
            manager._engine._user_response_timeout_task = None
            manager._engine._context_summarization_manager = None
            await PipecatEngine.cleanup(manager._engine)

            callback.assert_not_awaited()
            revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
            assert manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_dograh_restart_cannot_reconstruct_or_rebind_original_callback(
        self,
    ):
        """A fresh manager has no observer, callback, or registration claim from the old process."""
        old_manager, tool = self._identity_manager_and_tool()
        tool.definition["config"]["late_terminal"] = self._late_terminal_capability()
        handler = old_manager._create_http_tool_handler(tool, "windows_open_app")
        callback = AsyncMock()
        params = Mock(
            tool_call_id="call_restart_absence_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        polling = asyncio.Event()

        async def pending_poll(*_args, **_kwargs):
            polling.set()
            await asyncio.Event().wait()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                return_value={
                    "status": "late_terminal_pending",
                    "registration_id": registration_id,
                },
            ) as execute,
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                side_effect=pending_poll,
            ) as poll,
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
            ) as revoke,
        ):
            await handler(params)
            await asyncio.wait_for(polling.wait(), timeout=1)
            await old_manager.cancel_late_terminal_observers()
            new_manager, _new_tool = self._identity_manager_and_tool()

            execute.assert_awaited_once()
            poll.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
            revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
            callback.assert_not_awaited()
            assert old_manager._late_terminal_observers == {}
            assert new_manager._late_terminal_observers == {}

    @pytest.mark.asyncio
    async def test_late_terminal_observer_expiry_rejects_delivery_and_revokes(self):
        """An observer deadline is a failure boundary, not a fabricated terminal result."""
        manager, tool = self._identity_manager_and_tool()
        capability = self._late_terminal_capability()
        capability.update(max_wait_ms=1, poll_wait_ms=1)
        tool.definition["config"]["late_terminal"] = capability
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        delivered = asyncio.Event()

        async def record_result(*_args, **_kwargs):
            delivered.set()

        callback = AsyncMock(side_effect=record_result)
        replay_callback = AsyncMock()
        params = Mock(
            tool_call_id="call_expiry_123", arguments={}, result_callback=callback
        )
        replay_params = Mock(
            tool_call_id=params.tool_call_id,
            arguments={},
            result_callback=replay_callback,
        )
        identity = custom_tool_module.HttpToolRuntimeIdentity(
            tool_call_id=custom_tool_module.canonicalize_http_tool_call_id(
                params.tool_call_id
            ),
            workflow_run_id="42",
            tool_uuid="windows-tool-uuid",
            agent_scope="jeeves_windows",
        )
        registration_id = custom_tool_module.late_terminal_registration_id(identity)
        revoked = asyncio.Event()

        async def record_revoke(*_args, **_kwargs):
            revoked.set()

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "status": "late_terminal_pending",
                        "registration_id": registration_id,
                    },
                    {
                        "status": "success",
                        "status_code": 410,
                        "data": {"status": "expired"},
                    },
                ],
            ),
            patch.object(
                custom_tool_module,
                "poll_late_terminal",
                new_callable=AsyncMock,
                return_value={"status": "pending"},
            ),
            patch.object(
                custom_tool_module,
                "revoke_late_terminal",
                new_callable=AsyncMock,
                side_effect=record_revoke,
            ) as revoke,
        ):
            await handler(params)
            await asyncio.wait_for(revoked.wait(), timeout=1)
            await handler(replay_params)
            callback.assert_not_awaited()
            replay_callback.assert_not_awaited()
            revoke.assert_awaited_once_with(tool, ANY, identity, registration_id, 7)
        await manager.cancel_late_terminal_observers()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("length", [700, 1268])
    async def test_http_handler_canonicalizes_long_provider_tool_call_id(
        self,
        length,
    ):
        """Provider IDs observed in production become one fixed-size identity."""
        manager, tool = self._identity_manager_and_tool()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        raw_id = "x" * length
        params = Mock(
            tool_call_id=raw_id,
            arguments={"app_id": "app:v1:spotify"},
            result_callback=AsyncMock(),
        )

        with patch(
            "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
            new_callable=AsyncMock,
            return_value={"status": "success"},
        ) as mock_execute:
            await handler(params)

        identity = mock_execute.await_args.kwargs["runtime_identity"]
        assert identity.tool_call_id == (
            "tcid:v1:" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
        )
        assert len(identity.tool_call_id) == 72

    @pytest.mark.asyncio
    async def test_http_handler_reuses_identity_for_same_logical_tool_call(self):
        """Repeated delivery of one Pipecat call forwards identical identity."""
        manager, tool = self._identity_manager_and_tool()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        params = Mock(
            tool_call_id="call_retry_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=AsyncMock(),
        )

        with patch(
            "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
            new_callable=AsyncMock,
            return_value={"status": "success"},
        ) as mock_execute:
            await handler(params)
            await handler(params)

        identities = [
            awaited.kwargs["runtime_identity"]
            for awaited in mock_execute.await_args_list
        ]
        assert identities[0] == identities[1]

    @pytest.mark.asyncio
    async def test_http_handler_does_not_collide_distinct_tool_calls(self):
        """Distinct Pipecat call IDs remain distinct at the HTTP executor."""
        manager, tool = self._identity_manager_and_tool()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")

        with patch(
            "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
            new_callable=AsyncMock,
            return_value={"status": "success"},
        ) as mock_execute:
            for tool_call_id in ("call_first_123", "call_second_456"):
                await handler(
                    Mock(
                        tool_call_id=tool_call_id,
                        arguments={"app_id": "app:v1:spotify"},
                        result_callback=AsyncMock(),
                    )
                )

        identities = [
            awaited.kwargs["runtime_identity"]
            for awaited in mock_execute.await_args_list
        ]
        assert identities[0].tool_call_id != identities[1].tool_call_id

    @pytest.mark.asyncio
    async def test_http_handler_rejects_missing_pipecat_tool_call_id(self):
        """An opted-in handler fails closed before executor I/O without a stable ID."""
        manager, tool = self._identity_manager_and_tool()
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        result_callback = AsyncMock()
        params = Mock(
            tool_call_id="",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=result_callback,
        )

        with patch(
            "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
            new_callable=AsyncMock,
        ) as mock_execute:
            await handler(params)

        mock_execute.assert_not_awaited()
        result_callback.assert_awaited_once_with(
            {
                "status": "error",
                "error": "Stable tool call identity is required for this HTTP tool",
            }
        )

    @pytest.mark.asyncio
    async def test_http_handler_rejects_missing_agent_scope(self):
        """An opted-in handler requires explicit Dograh-owned agent scope."""
        manager, tool = self._identity_manager_and_tool()
        del tool.definition["config"]["agent_scope"]
        handler = manager._create_http_tool_handler(tool, "windows_open_app")
        result_callback = AsyncMock()
        params = Mock(
            tool_call_id="call_open_app_123",
            arguments={"app_id": "app:v1:spotify"},
            result_callback=result_callback,
        )

        with patch(
            "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
            new_callable=AsyncMock,
        ) as mock_execute:
            await handler(params)

        mock_execute.assert_not_awaited()
        result_callback.assert_awaited_once_with(
            {
                "status": "error",
                "error": "Agent scope is required for this HTTP tool",
            }
        )

    @pytest.mark.asyncio
    async def test_get_tool_schemas_returns_correct_format(self):
        """Test that get_tool_schemas returns FunctionSchema objects."""
        # Create a mock engine
        from pipecat.adapters.schemas.function_schema import FunctionSchema

        from api.services.workflow.pipecat_engine import PipecatEngine
        from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

        mock_engine = Mock()
        mock_engine._workflow_run_id = 1
        mock_engine._call_context_vars = {}
        mock_engine._organization_id = None
        mock_engine._get_organization_id = PipecatEngine._get_organization_id.__get__(
            mock_engine
        )

        manager = CustomToolManager(mock_engine)

        # Mock the database client
        mock_tool = MockToolModel(
            tool_uuid="uuid-1",
            name="Test Tool",
            description="A test tool",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/test",
                    "parameters": [
                        {
                            "name": "param1",
                            "type": "string",
                            "description": "Test param",
                            "required": True,
                        }
                    ],
                },
            },
        )

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.db_client"
            ) as mock_db,
            patch(
                "api.db:db_client.get_organization_id_by_workflow_run_id",
                new_callable=AsyncMock,
                return_value=1,
            ),
        ):
            mock_db.get_tools_by_uuids = AsyncMock(return_value=[mock_tool])

            schemas = await manager.get_tool_schemas(["uuid-1"])

            assert len(schemas) == 1
            schema = schemas[0]

            # Schema should be a FunctionSchema object
            assert isinstance(schema, FunctionSchema)

            # FunctionSchema should have correct attributes
            assert schema.name == "test_tool"
            assert "param1" in schema.properties
            assert schema.properties["param1"]["type"] == "string"
            assert "param1" in schema.required

    @pytest.mark.asyncio
    async def test_register_handlers_creates_working_handler(self):
        """Test that register_handlers creates handlers that can execute tools."""
        from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

        # Create a mock engine with a mock LLM
        mock_llm = Mock()
        registered_handlers = {}
        registered_kwargs = {}

        def capture_register(name, handler, **kwargs):
            registered_handlers[name] = handler
            registered_kwargs[name] = kwargs

        mock_llm.register_function = capture_register

        from api.services.workflow.pipecat_engine import PipecatEngine

        mock_engine = Mock()
        mock_engine._workflow_run_id = 1
        mock_engine._call_context_vars = {}
        mock_engine._organization_id = None
        mock_engine._get_organization_id = PipecatEngine._get_organization_id.__get__(
            mock_engine
        )
        mock_engine.llm = mock_llm

        manager = CustomToolManager(mock_engine)

        mock_tool = MockToolModel(
            tool_uuid="uuid-1",
            name="API Call",
            description="Make an API call",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "POST",
                    "url": "https://api.example.com/call",
                    "parameters": [],
                },
            },
        )

        with (
            patch(
                "api.services.workflow.pipecat_engine_custom_tools.db_client"
            ) as mock_db,
            patch(
                "api.db:db_client.get_organization_id_by_workflow_run_id",
                new_callable=AsyncMock,
                return_value=1,
            ),
        ):
            mock_db.get_tools_by_uuids = AsyncMock(return_value=[mock_tool])

            await manager.register_handlers(["uuid-1"])

            # Verify handler was registered
            assert "api_call" in registered_handlers
        assert registered_kwargs["api_call"]["timeout_secs"] == pytest.approx(6)

        # Now test that the handler works
        handler = registered_handlers["api_call"]

        result_received = None

        async def mock_result_callback(result, properties=None):
            nonlocal result_received
            result_received = result

        mock_params = Mock()
        mock_params.arguments = {"key": "value"}
        mock_params.result_callback = mock_result_callback

        with patch(
            "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool"
        ) as mock_execute:
            mock_execute.return_value = {
                "status": "success",
                "data": {"response": "ok"},
            }

            await handler(mock_params)

            # Verify execute was called
            mock_execute.assert_called_once()

            # Verify result was returned
            assert result_received["status"] == "success"


def _update_llm_context(context, system_message, functions):
    """Inline helper replicating the old update_llm_context for tests."""
    tools_schema = ToolsSchema(standard_tools=functions)
    previous_interactions = context.messages

    if previous_interactions and previous_interactions[0]["role"] == "system":
        messages = [system_message] + previous_interactions[1:]
    else:
        messages = [system_message] + previous_interactions

    context.set_messages(messages)

    if functions:
        context.set_tools(tools_schema)


class TestUpdateLLMContext:
    """Tests for _update_llm_context inline logic."""

    def test_replaces_system_message(self):
        """Test that _update_llm_context replaces existing system messages."""
        context = LLMContext()
        context.set_messages(
            [
                {"role": "system", "content": "Old system message"},
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ]
        )

        new_system = {"role": "system", "content": "New system message"}
        _update_llm_context(context, new_system, [])

        messages = context.messages
        # Should have new system message at the start
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "New system message"
        # Should preserve user and assistant messages
        assert len(messages) == 3
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"

    def test_preserves_conversation_history(self):
        """Test that user/assistant messages are preserved in order."""
        context = LLMContext()
        context.set_messages(
            [
                {"role": "system", "content": "Old prompt"},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
                {"role": "assistant", "content": "Second answer"},
            ]
        )

        new_system = {"role": "system", "content": "New prompt"}
        _update_llm_context(context, new_system, [])

        messages = context.messages
        assert len(messages) == 5
        assert messages[1]["content"] == "First question"
        assert messages[2]["content"] == "First answer"
        assert messages[3]["content"] == "Second question"
        assert messages[4]["content"] == "Second answer"

    def test_sets_tools_when_functions_provided(self):
        """Test that tools are set on context when functions are provided."""
        context = LLMContext()
        context.set_messages([{"role": "system", "content": "Old"}])

        # Create function schemas
        functions = [
            get_function_schema("book_appointment", "Book an appointment"),
            get_function_schema("cancel_appointment", "Cancel an appointment"),
        ]

        new_system = {"role": "system", "content": "New prompt with tools"}
        _update_llm_context(context, new_system, functions)

        # Verify tools were set
        tools = context.tools
        assert tools is not None
        assert len(tools.standard_tools) == 2

    def test_does_not_set_tools_when_functions_empty(self):
        """Test that tools are not set when functions list is empty."""
        context = LLMContext()
        context.set_messages([{"role": "system", "content": "Old"}])

        new_system = {"role": "system", "content": "New prompt without tools"}
        _update_llm_context(context, new_system, [])

        # Tools should not be set (or remain None)
        # Note: The function only calls set_tools if functions is truthy
        # So we verify the context state is as expected
        messages = context.messages
        assert len(messages) == 1
        assert messages[0]["content"] == "New prompt without tools"

    def test_works_with_empty_context(self):
        """Test that update works on a fresh context with no messages."""
        context = LLMContext()

        new_system = {"role": "system", "content": "Initial prompt"}
        functions = [get_function_schema("test_func", "A test function")]

        _update_llm_context(context, new_system, functions)

        messages = context.messages
        assert len(messages) == 1
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Initial prompt"

    def test_function_schema_structure(self):
        """Test that get_function_schema creates correct structure."""
        schema = get_function_schema(
            "search_products",
            "Search for products in the catalog",
            properties={
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results"},
            },
            required=["query"],
        )

        assert schema.name == "search_products"
        assert schema.description == "Search for products in the catalog"
        assert "query" in schema.properties
        assert "limit" in schema.properties
        assert "query" in schema.required
        assert "limit" not in schema.required

    def test_function_schema_with_no_parameters(self):
        """Test get_function_schema with no properties or required."""
        schema = get_function_schema("ping", "Check if service is alive")

        assert schema.name == "ping"
        assert schema.description == "Check if service is alive"
        assert schema.properties == {}
        assert schema.required == []
