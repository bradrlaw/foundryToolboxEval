# OpenTelemetry to Application Insights

All three runners emit spans to Application Insights when a connection string is set. Without it nothing is exported.

## 1. Get an App Insights resource

A Foundry project usually has one connected (Foundry portal → project → **Tracing**, or Management center → Connected
resources). Otherwise create one and connect it:

```powershell
az monitor app-insights component create -a <name> -g <rg> -l <region> --kind web   # needs: az extension add -n application-insights
az resource show --ids <appinsights-resource-id> --query properties.ConnectionString -o tsv
```

If the portal already connects one to the project, Foundry's own service spans (`responsesapi`: `invoke_agent`, `chat <model>`,
`execute_tool`) land in it too.

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
| `foundry-toolbox-tests` | `toolbox.test_case` (`eval.case_id`, `eval.kind`, `eval.category`, `gen_ai.tool.name`, `eval.passed`, `eval.outcome`, `eval.latency_p50_ms`, `eval.failures`, `eval.reasons`) |
| `foundry-agent-evals` | `eval.turn` (`eval.conversation_id`, `eval.turn_number`, `eval.category`, `eval.passed`, `eval.failures`, `eval.reasons`), with the target's `foundry_agent` and `tool:*` spans as children |
| `foundry-agent-target` | `foundry_agent` (`gen_ai.agent.name`, `eval.platform_abort`, `eval.abort_reason`) and `tool:*` (`gen_ai.tool.name`), when ASSERT runs the target or it runs on its own |
| `responsesapi` (Foundry) | `invoke_agent`, `chat <model>`, `execute_tool` with real tool durations |

A process has one cloud role, set by the first module to install the tracer provider. Under `run_agent_evals` that is the
runner, so the target's spans carry `foundry-agent-evals`.

`eval.outcome` lists the outcomes seen across a case's runs (`ok`, `tool_error`, `protocol_error`, `consent_required`,
`upstream_error`, `invalid_result`, `transport_error`, `runner_error`), comma-separated. `eval.reasons` lists the distinct
reason codes of a failed case or turn ([evals/reasons.py](../evals/reasons.py)), comma-separated, and is empty when it
passed. `eval.abort_reason` is `tool_error`, `consent_required` or `tool_listing_failed` when `eval.platform_abort` is
true ([entra.md](entra.md#failures-specific-to-identity)).

Consent links never appear in spans. Identity cases can put the test user's object ID, name or username into span
attributes: agent replies in `output.value`, and failure messages with filled-in `{{caller.*}}` values in `eval.failures`.

The target injects a W3C `traceparent` into the Responses call, so server spans share the `operation_Id` of the eval turn
and you can see one trace from eval turn to model and tool calls. Tool spans created by the target are zero-duration
markers; use the server-side `execute_tool` spans for tool latency.

## 4. Query

Portal → App Insights → Logs (spans are in `dependencies`):

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

// agent requests that aborted or never ran, by reason
dependencies | where name == "foundry_agent" and tostring(customDimensions["eval.platform_abort"]) =~ "true"
| summarize n=count() by reason=tostring(customDimensions["eval.abort_reason"]), bin(timestamp, 1d)

// full trace for one eval turn
dependencies | where operation_Id == "<id>" | project timestamp, cloud_RoleName, name, duration | order by timestamp asc
```

From the CLI:

```powershell
'{"query":"dependencies | take 5"}' | Set-Content q.json
az rest --method post --resource https://api.applicationinsights.io `
  --url https://api.applicationinsights.io/v1/apps/<appId>/query --headers "Content-Type=application/json" --body "@q.json"
```
