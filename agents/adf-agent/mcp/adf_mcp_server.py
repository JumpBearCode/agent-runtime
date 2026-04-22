"""
Azure Data Factory MCP Server
Wraps the Azure Data Factory SDK and exposes ADF resources as MCP tools via FastMCP.

Auth model
----------
This subprocess does NOT own a credential. It holds one long-lived
DataFactoryManagementClient whose credential is a `ContextualCredential`
from the top-level `auth` package. The runtime middleware injects
`_auth_token` / `_auth_expires_at` kwargs on every tool call; the
`@with_auth` decorator stashes them in a thread-local that
ContextualCredential reads when the Azure SDK asks for a token. After the
call, the decorator clears the thread-local. Cross-user state cannot
survive a tool call.
"""

import os
from typing import Any

from azure.mgmt.datafactory import DataFactoryManagementClient
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from auth import ContextualCredential, with_auth

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration – read from environment variables or .env
# ---------------------------------------------------------------------------
SUBSCRIPTION_ID: str = os.environ["ADF_SUBSCRIPTION_ID"]
RESOURCE_GROUP: str = os.environ["ADF_RESOURCE_GROUP"]
FACTORY_NAME: str = os.environ["ADF_FACTORY_NAME"]

# ---------------------------------------------------------------------------
# ADF client — built once, auth comes from ContextualCredential thread-local
# ---------------------------------------------------------------------------
_credential = ContextualCredential()
_client: DataFactoryManagementClient = DataFactoryManagementClient(
    _credential, SUBSCRIPTION_ID,
)


def _get_client() -> DataFactoryManagementClient:
    return _client


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="azure-data-factory",
    instructions="MCP server that exposes Azure Data Factory resources as tools.",
)


# ── Pipelines ──────────────────────────────────────────────────────────────


@mcp.tool(
    name="list_pipelines",
    description=(
        "List all pipelines in the Azure Data Factory. "
        "Returns a list of pipeline names and their descriptions."
    ),
)
@with_auth
def list_pipelines() -> list[dict[str, Any]]:
    """Return summary info for every pipeline in the factory."""
    client = _get_client()
    results: list[dict[str, Any]] = []

    for pipeline in client.pipelines.list_by_factory(RESOURCE_GROUP, FACTORY_NAME):
        results.append(
            {
                "name": pipeline.name,
                "description": pipeline.description,
                "etag": pipeline.etag,
            }
        )

    return results


@mcp.tool(
    name="get_pipeline",
    description=(
        "Get the full definition of a specific pipeline in the Azure Data Factory, "
        "including all activities, parameters, and variables."
    ),
)
@with_auth
def get_pipeline(pipeline_name: str) -> dict[str, Any]:
    """
    Retrieve the complete definition of a single pipeline.

    Args:
        pipeline_name: The name of the pipeline to retrieve.
    """
    client = _get_client()
    pipeline = client.pipelines.get(RESOURCE_GROUP, FACTORY_NAME, pipeline_name)

    activities = []
    if pipeline.activities:
        for act in pipeline.activities:
            activities.append(
                {
                    "name": act.name,
                    "type": act.type,
                    "description": getattr(act, "description", None),
                    "depends_on": [
                        {"activity": d.activity, "dependency_conditions": d.dependency_conditions}
                        for d in (act.depends_on or [])
                    ],
                }
            )

    return {
        "name": pipeline.name,
        "description": pipeline.description,
        "etag": pipeline.etag,
        "activities": activities,
        "parameters": {
            k: {"type": v.type, "default_value": v.default_value}
            for k, v in (pipeline.parameters or {}).items()
        },
        "variables": {
            k: {"type": v.type, "default_value": v.default_value}
            for k, v in (pipeline.variables or {}).items()
        },
        "concurrency": pipeline.concurrency,
        "annotations": pipeline.annotations or [],
        "folder": pipeline.folder.name if pipeline.folder else None,
    }


# ── Data Flows ─────────────────────────────────────────────────────────────


@mcp.tool(
    name="list_data_flows",
    description=(
        "List all data flows in the Azure Data Factory. "
        "Returns the name, type, and description of each data flow."
    ),
)
@with_auth
def list_data_flows() -> list[dict[str, Any]]:
    """Return summary info for every data flow in the factory."""
    client = _get_client()
    results: list[dict[str, Any]] = []

    for df in client.data_flows.list_by_factory(RESOURCE_GROUP, FACTORY_NAME):
        results.append(
            {
                "name": df.name,
                "type": df.type,
                "etag": df.etag,
                "description": getattr(df.properties, "description", None),
            }
        )

    return results


@mcp.tool(
    name="get_data_flow",
    description=(
        "Get the full definition of a specific data flow in the Azure Data Factory, "
        "including sources, sinks, and transformation scripts."
    ),
)
@with_auth
def get_data_flow(data_flow_name: str) -> dict[str, Any]:
    """
    Retrieve the complete definition of a single data flow.

    Args:
        data_flow_name: The name of the data flow to retrieve.
    """
    client = _get_client()
    df = client.data_flows.get(RESOURCE_GROUP, FACTORY_NAME, data_flow_name)
    props = df.properties

    # Helper to serialise source/sink objects which differ by data-flow type
    def _to_dict(obj: Any) -> dict[str, Any] | None:
        if obj is None:
            return None
        if hasattr(obj, "__dict__"):
            return {k: v for k, v in vars(obj).items() if not k.startswith("_") and v is not None}
        return str(obj)

    result: dict[str, Any] = {
        "name": df.name,
        "type": df.type,
        "etag": df.etag,
        "description": getattr(props, "description", None),
        "annotations": getattr(props, "annotations", []) or [],
        "folder": props.folder.name if getattr(props, "folder", None) else None,
    }

    # MappingDataFlow
    if hasattr(props, "sources"):
        result["sources"] = [_to_dict(s) for s in (props.sources or [])]
    if hasattr(props, "sinks"):
        result["sinks"] = [_to_dict(s) for s in (props.sinks or [])]
    if hasattr(props, "transformations"):
        result["transformations"] = [_to_dict(t) for t in (props.transformations or [])]
    if hasattr(props, "script"):
        result["script"] = props.script
    if hasattr(props, "script_lines"):
        result["script_lines"] = props.script_lines

    return result


# ── Linked Services ────────────────────────────────────────────────────────


@mcp.tool(
    name="list_linked_services",
    description=(
        "List all linked services in the Azure Data Factory. "
        "Returns each linked service's name, type, and description."
    ),
)
@with_auth
def list_linked_services() -> list[dict[str, Any]]:
    """Return summary info for every linked service in the factory."""
    client = _get_client()
    results: list[dict[str, Any]] = []

    for ls in client.linked_services.list_by_factory(RESOURCE_GROUP, FACTORY_NAME):
        results.append(
            {
                "name": ls.name,
                "type": ls.type,
                "etag": ls.etag,
                "description": getattr(ls.properties, "description", None),
                "connect_via": (
                    ls.properties.connect_via.reference_name
                    if getattr(ls.properties, "connect_via", None)
                    else None
                ),
                "annotations": getattr(ls.properties, "annotations", []) or [],
            }
        )

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
