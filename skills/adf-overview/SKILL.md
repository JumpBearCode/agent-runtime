---
name: adf-overview
description: Get a comprehensive overview of the Azure Data Factory — pipelines, data flows, linked services counts and summaries
---
# ADF Overview

Get a comprehensive summary of an Azure Data Factory instance using MCP tools.

## Available MCP Tools

- `mcp_adf_list_pipelines` — list all pipelines with name and description
- `mcp_adf_list_data_flows` — list all data flows with name and type
- `mcp_adf_list_linked_services` — list all linked services with name, type, and IR

## Workflow

### Step 1: Gather All Resources (parallel)

Call all three tools in parallel:

1. `mcp_adf_list_pipelines` — count and list all pipeline names
2. `mcp_adf_list_data_flows` — count and list all data flow names
3. `mcp_adf_list_linked_services` — count and list all linked services, group by type

### Step 2: Analyze and Summarize

From the results, build:

1. **Resource Counts**:
   - Total pipelines
   - Total data flows
   - Total linked services

2. **Linked Services by Type** — group and count:
   ```
   | Type | Count | Services |
   |---|---|---|
   | SnowflakeV2 | 3 | snow-prod, snow-staging, snow-dev |
   | AzureBlobStorage | 2 | blob-raw, blob-processed |
   | AzureSqlDatabase | 1 | sql-metadata |
   ```

3. **Integration Runtimes Used** — identify unique IRs and which services use them

4. **Pipeline Overview** — list pipelines with descriptions (if available)

5. **Data Flow Overview** — list data flows with types

### Step 3: Present Summary

Combine into a clear, organized overview with sections:

```
## ADF Factory Summary

### Resources
- Pipelines: 12
- Data Flows: 5
- Linked Services: 8

### Linked Services by Type
(table from Step 2)

### Integration Runtimes
(list of IRs and their associated services)

### Pipelines
(list with descriptions)

### Data Flows
(list with types)
```

## Important Notes

- Always call all three list tools in parallel for efficiency
- This is a read-only overview — no modifications are made
- If any tool call fails (e.g. missing credentials), report what was available and note the failure
