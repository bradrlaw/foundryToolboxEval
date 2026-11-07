# OpenTelemetry to Application Insights

All three runners emit spans to Application Insights when a connection string is set. Without it nothing is exported.

## 1. Get an App Insights resource

A Foundry project usually has one connected (Foundry portal → project → **Tracing**, or Management center → Connected
resources). Otherwise create a workspace-based one:

```powershell
az monitor log-analytics workspace create -n <workspace> -g <rg> -l <region>
az monitor app-insights component create -a <name> -g <rg> -l <region> --kind web --workspace <workspace-resource-id>   # needs: az extension add -n application-insights
az resource show --ids <appinsights-resource-id> --query properties.ConnectionString -o tsv
```

Then connect it to the project (portal: project → **Tracing**), so Foundry's own spans land in it too: the agent service's
(`responsesapi`: `invoke_agent`, `chat <model>`, `execute_tool`) and the toolbox's (`agentsv2`). Without the portal, put this
project connection body in `conn.json`:

```json
{"properties": {"category": "AppInsights", "authType": "ApiKey", "target": "<appinsights-resource-id>", "isSharedToAll": false,
  "credentials": {"key": "<connection string>"}, "metadata": {"ApiType": "Azure", "ResourceId": "<appinsights-resource-id>"}}}
```

```powershell
az rest --method put --body "@conn.json" --url "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<resource>/projects/<project>/connections/<name>?api-version=2025-06-01"
Remove-Item conn.json   # it holds the connection string
```

## 2. Configure

In `.env` (treat the connection string as a secret; never commit it):

```
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...;IngestionEndpoint=...
# APPLICATIONINSIGHTS_USE_AAD=1     # if the resource disables local (key) auth; needs Monitoring Metrics Publisher
# OTEL_SERVICE_NAME=...             # overrides the cloud role name
```

Run the runners with the venv python (it has `azure-monitor-opentelemetry-exporter`). Ingestion lags 2–3 minutes.

## 3. What is emitted

| Cloud role | Spans |
| --- | --- |
| `foundry-toolbox-tests` | `toolbox.test_case` (`eval.case_id`, `eval.kind`, `eval.category`, `gen_ai.tool.name`, `eval.passed`, `eval.outcome`, `eval.latency_p50_ms`, `eval.failures`, `eval.reasons`), with the MCP SDK's `MCP send <method>` spans as children and the toolbox's requests under those |
| `foundry-agent-evals` | `eval.turn` (`eval.kind`, `eval.conversation_id`, `eval.turn_number`, `eval.repetition`, `eval.category`, `eval.passed`, `eval.failures`, `eval.reasons`, `eval.extra_calls`), with the target's `foundry_agent` and `tool:*` spans as children |
| `foundry-agent-target` | `foundry_agent` (`gen_ai.agent.name`, `gen_ai.agent.version`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `eval.platform_abort`, `eval.abort_reason`) and `tool:*` (`gen_ai.tool.name`), when ASSERT runs the target or it runs on its own. The version and tokens are those the agent's response reports, absent when the request failed |
| `responsesapi` (Foundry) | `invoke_agent`, `chat <model>`, `execute_tool` with real tool durations |
| `agentsv2` (Foundry toolbox) | Requests, in `requests`: `tools/call <tool>` and `tools/list <toolbox>:<version>` (`gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `microsoft.foundry.toolbox.name`, `microsoft.foundry.toolbox.version`, `error.type`, `rpc.response.status_code`). `resultCode` is `0`, or `500` when the call fails (`error.type` `tool_error` or `json_rpc_error`) |

A process has one cloud role, set by the first module to install the tracer provider. Under `run_agent_evals` that is the
runner, so the target's spans carry `foundry-agent-evals`.

`eval.outcome` lists the outcomes seen across a case's runs (`ok`, `tool_error`, `protocol_error`, `consent_required`,
`upstream_error`, `invalid_result`, `transport_error`, `runner_error`), comma-separated. `eval.reasons` lists the distinct
reason codes of a failed case or turn ([evals/reasons.py](../evals/reasons.py)), comma-separated, and is empty when it
passed. `eval.abort_reason` is `tool_error`, `consent_required` or `tool_listing_failed` when `eval.platform_abort` is
true ([entra.md](entra.md#failures-specific-to-identity)). `eval.repetition` is the run's number under `--repetitions`
(1 without it). `eval.extra_calls` counts the turn's tool calls beyond those it expects, and is absent when the turn
doesn't check its calls or none are known.

Consent links never appear in spans. Identity cases can put the test user's object ID, name or username into span
attributes: agent replies in `output.value`, and failure messages with filled-in `{{caller.*}}` values in `eval.failures`.
The toolbox's requests record every tool call's arguments (`gen_ai.tool.call.arguments`) and the tenant, subscription and
project IDs.

The target injects a W3C `traceparent` into the Responses call, so server spans share the `operation_Id` of the eval turn
and you can see one trace from eval turn to model and tool calls, including the toolbox's requests. Tool spans created by
the target are zero-duration markers; use the server-side spans for tool latency: `execute_tool` is the agent's view (it
includes Foundry's overhead), the toolbox's `tools/call` request the tool's.

The trace holds when the toolbox uses OAuth identity passthrough too: in the Entra sample's agent evals, every turn's
trace reached `invoke_agent`, `execute_tool` and the toolbox's `tools/call` requests. Foundry records one `chat <model>`
span per turn even when the agent calls tools, matching the one model request per turn that Azure Monitor counts for the
deployment. The toolbox's `tools/list` request appears in only some traces (once per run in our runs), because the
service reuses a toolbox's tool list across requests.

The tool runner sends the same header on every request to the toolbox, which ignores the copy of the trace context that
the MCP SDK puts in `params._meta`. Each toolbox request is a child of the runner's `MCP send` span, in the case's trace.
`MCP send` is the round trip the runner sees, and the toolbox's request the time spent inside the toolbox; the difference
is network and gateway overhead.

## 4. Query

Portal → App Insights → Logs (spans are in `dependencies`, the toolbox's requests in `requests`):

```kusto
// eval pass rate by day
dependencies | where name == "eval.turn"
| summarize turns=count(), passed=countif(tostring(customDimensions["eval.passed"]) =~ "true") by bin(timestamp, 1d)

// tool-test outcomes by tool
dependencies | where name == "toolbox.test_case"
| summarize cases=count() by tool=tostring(customDimensions["gen_ai.tool.name"]), outcome=tostring(customDimensions["eval.outcome"])

// failed tool-test cases and agent turns by reason
dependencies | where name in ("toolbox.test_case", "eval.turn") and tostring(customDimensions["eval.passed"]) =~ "false"
| mv-expand reason = split(tostring(customDimensions["eval.reasons"]), ",") to typeof(string)
| summarize failed=count() by name, reason, bin(timestamp, 1d)

// tool latency from Foundry's server-side spans
dependencies | where cloud_RoleName == "responsesapi" and name startswith "execute_tool"
| summarize p50=percentile(duration,50), p95=percentile(duration,95), n=count() by name

// toolbox latency and failures by tool, from all callers
requests | where cloud_RoleName == "agentsv2" and name startswith "tools/call"
| summarize p50=percentile(duration,50), p95=percentile(duration,95), n=count(), failed=countif(success == false)
    by tool=tostring(customDimensions["gen_ai.tool.name"])

// tool tests: the runner's round trip against the time inside the toolbox
dependencies | where cloud_RoleName == "foundry-toolbox-tests" and name startswith "MCP send tools/call"
| join kind=inner (requests | where cloud_RoleName == "agentsv2" | project operation_ParentId, toolbox=duration)
    on $left.id == $right.operation_ParentId
| summarize round_trip_p50=percentile(duration,50), toolbox_p50=percentile(toolbox,50), n=count() by name

// agent requests that aborted or never ran, by reason
dependencies | where name == "foundry_agent" and tostring(customDimensions["eval.platform_abort"]) =~ "true"
| summarize n=count() by reason=tostring(customDimensions["eval.abort_reason"]), bin(timestamp, 1d)

// agent tokens by agent version and day
dependencies | where name == "foundry_agent" and isnotempty(customDimensions["gen_ai.usage.input_tokens"])
| summarize turns=count(), input=sum(toint(customDimensions["gen_ai.usage.input_tokens"])),
    output=sum(toint(customDimensions["gen_ai.usage.output_tokens"]))
    by agent=tostring(customDimensions["gen_ai.agent.name"]), version=tostring(customDimensions["gen_ai.agent.version"]), bin(timestamp, 1d)

// full trace for one eval turn or tool-test case
union dependencies, requests | where operation_Id == "<id>" | project timestamp, cloud_RoleName, name, duration | order by timestamp asc
```

From the CLI:

```powershell
'{"query":"dependencies | take 5"}' | Set-Content q.json
az rest --method post --resource https://api.applicationinsights.io `
  --url https://api.applicationinsights.io/v1/apps/<appId>/query --headers "Content-Type=application/json" --body "@q.json"
```
