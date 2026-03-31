---
name: test-linked-service
description: Test linked service connections — list services, check their types and Integration Runtime configuration, and verify connectivity status
---
# Test Linked Service Connections

Test one or more Azure Data Factory linked service connections using MCP tools.

## Available MCP Tools

- `mcp_adf_list_linked_services` — list all linked services with name, type, and IR info
- `mcp_adf_list_pipelines` — list all pipelines
- `mcp_adf_get_pipeline(pipeline_name)` — get full pipeline definition

**Note**: Direct connection testing (`adf_linked_service_test`) is not available through MCP. This skill focuses on listing, inspecting, and validating linked service configurations.

## Workflow

### 1. Determine Scope

| User Request | Action |
|---|---|
| Inspect a specific linked service by name | Go to **Step 2** |
| Inspect all linked services | `mcp_adf_list_linked_services` → go to **Step 2** for each |
| Inspect by type (e.g. "all Snowflake") | `mcp_adf_list_linked_services` → filter by matching type(s) → go to **Step 2** |

### 2. List and Inspect Linked Services

Call `mcp_adf_list_linked_services` to get all linked services with their types and Integration Runtime references.

For each service of interest, note:
- **Name** and **Type** (e.g. SnowflakeV2, AzureBlobStorage)
- **Integration Runtime** reference (`connect_via` field)
- **Annotations** for any metadata tags

### 3. Report Configuration Summary

Present a summary table:

```
| Linked Service | Type | Integration Runtime | Annotations |
|---|---|---|---|
| my-snowflake | SnowflakeV2 | ir-managed-01 | production |
| my-blob | AzureBlobStorage | (AutoResolve) | - |
| my-sql | SqlServer | ir-selfhosted | staging |
```

### 4. Provide Recommendations

Based on the configuration:
- **SelfHosted IR**: Remind user to verify the IR node is running and accessible
- **Managed IR**: Note that interactive authoring may need to be enabled for testing
- **Missing IR reference**: Service uses AutoResolve default
- **Authentication concerns**: Suggest verifying Key Vault references or credentials

## Important Notes

- Always call `mcp_adf_list_linked_services` first to understand what's available
- Group services by Integration Runtime for efficient analysis
- If the user needs actual connection testing, advise using Azure Portal or the ADF SDK directly
