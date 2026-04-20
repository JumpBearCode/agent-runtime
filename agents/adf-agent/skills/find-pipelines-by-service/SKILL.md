---
name: find-pipelines-by-service
description: Find all pipelines that use a specific type of linked service (e.g. Snowflake, AzureBlobStorage). Cross-references pipelines, data flows, and linked services.
---
# Find Pipelines by Linked Service Type

Find all pipelines in an ADF instance that use a specific type of linked service, through both direct activity references and indirect dataset/data flow references.

## Available MCP Tools

- `mcp_adf_list_linked_services` — list all linked services with name and type
- `mcp_adf_list_pipelines` — list all pipelines
- `mcp_adf_get_pipeline(pipeline_name)` — get full pipeline definition with activities
- `mcp_adf_list_data_flows` — list all data flows
- `mcp_adf_get_data_flow(data_flow_name)` — get full data flow definition with sources/sinks

## Workflow

### Step 1: List All Resources (parallel)

Call all three in parallel for efficiency:

- `mcp_adf_list_linked_services` — collect names + types
- `mcp_adf_list_pipelines` — collect all pipeline names
- `mcp_adf_list_data_flows` — collect all data flow names

### Step 2: Identify Target Linked Services

From the linked service list, identify which ones match the user's request (e.g. type contains "Snowflake" or "AzureBlobStorage").

Collect the **names** of all matching linked services into a target set.

### Step 3: Inspect Pipelines

For each pipeline, call `mcp_adf_get_pipeline(pipeline_name)` to get the full definition.

Walk all activities and check:

1. **Direct references**: Activity-level linked service references (e.g. Web Activity, Azure Function)
2. **Indirect references**: Activity references a dataset which points to a target linked service
3. **Data flow references**: Activity invokes a data flow whose sources/sinks reference a target linked service

### Step 4: Inspect Data Flows

For data flows referenced by pipelines (or all data flows), call `mcp_adf_get_data_flow(name)` to check if sources or sinks reference the target linked services.

### Step 5: Present Results

Present a summary table of pipelines that reference the target linked service type:

```
| Pipeline | Linked Services Used | Reference Type |
|---|---|---|
| copy-snowflake-prod | snowflake-prod-ls | Direct (Copy Activity) |
| etl-daily | snowflake-staging-ls | Via Data Flow (df-transform) |
| lookup-config | snowflake-prod-ls | Direct (Lookup Activity) |
```

Also note:
- Total pipelines scanned vs matched
- Any data flows that reference the target service type
- Linked services of the target type that are NOT referenced by any pipeline (orphaned)

## Important Notes

- Always call the list tools in parallel (Step 1) for efficiency
- Check both direct and indirect references for complete results
- If the user asks about a specific version (e.g. "Snowflake v2 only"), inspect the linked service type field to distinguish
- For large factories, process pipelines in batches to avoid overwhelming output
