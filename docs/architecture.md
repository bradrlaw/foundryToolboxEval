# Architecture

This document covers how the repo evaluates a Microsoft Foundry toolbox: its components, how a run flows through them, the
data they exchange, and the reasons behind the design.

For setup, see [getting-started.md](getting-started.md) (the sample deployment) or
[existing-environment.md](existing-environment.md) (your own project). Spans and queries are in [telemetry.md](telemetry.md).

## 1. Evaluation paths

The repo tests a toolbox at two levels. *Tests* call the toolbox directly; *evals* go through an agent connected to it.

| Path | Entry point | Calls | Checks | Output |
| --- | --- | --- | --- | --- |
| Tool tests | `python -m tooltests.run` | Toolbox MCP endpoint (Streamable HTTP, JSON-RPC) | Tool listing and definitions, results (assertions), error outcomes, auth status codes, latency | `results/tool-tests/run-*.json` |
| Agent evals, deterministic | `python -m evals.run_agent_evals` | Prompt agent through the Responses API | Expected tool calls and arguments (optionally ordered), forbidden calls, response regexes, latency | `results/agent-evals/run-*.json` |
| Agent evals, ASSERT | `python scripts/run_assert.py <config>` | `assert-ai`, which drives the agent through `evals/foundry_agent/target.py` and judges it with an LLM | Violation rate for one behavior per config (for example ungrounded figures, raw error replies) | `results/assert/results/<suite>/<run>/` |

The two agent-eval paths complement each other:

- The deterministic runner checks exact calls and parameters, which ASSERT does not.
- ASSERT judges open-ended behavior, which regexes cannot.
- Both call the agent through the same target code, so a platform quirk shows up the same way in each.

Both paths also work with toolboxes whose connections pass the signed-in user's identity to the MCP server; see
[entra.md](entra.md).

## 2. System context

```mermaid
flowchart TB
    dev(["Developer or CI"])

    subgraph repo["This repo, local Python venv"]
        tt["Tool-test runner<br/>tooltests/run.py"]
        ae["Agent-eval runner<br/>evals/run_agent_evals.py"]
        aa["ASSERT (assert-ai)<br/>via scripts/run_assert.py"]
        tg["Agent target<br/>evals/foundry_agent/target.py"]
        res[("results/<br/>read by scripts/report.py")]
    end

    subgraph foundry["Microsoft Foundry"]
        agent["Prompt agent<br/>Responses API"]
        tb["Toolbox<br/>MCP endpoint"]
        models["Model deployments<br/>agent model, ASSERT models"]
    end

    mcp["MCP server<br/>sample: Azure Container Apps,<br/>no auth or Entra-protected"]
    entra["Microsoft Entra ID"]
    appi[("Application Insights<br/>optional")]

    dev --> tt & ae & aa
    ae -->|"run_agent"| tg
    aa -->|"chat"| tg
    tt -->|"MCP: tools/list, tools/call"| tb
    tg -->|"POST /openai/v1/responses"| agent
    agent -->|"model calls"| models
    agent -->|"MCP"| tb
    aa -->|"LLM calls"| models
    tb -->|"MCP, via a project connection:<br/>no auth, or the user's own token (OAuth2)"| mcp
    tt & ae & aa --> res
    repo -.->|"tokens from az login"| entra
    tb -.->|"OAuth2 connection:<br/>per-user consent and refresh"| entra
    repo -.->|"spans"| appi
    foundry -.->|"server-side spans"| appi
```

Everything runs locally against live Azure resources; no eval code runs inside Azure. Resource identifiers come from the
git-ignored `.env`, and tracked files contain none.

## 3. Components

```mermaid
flowchart TB
    subgraph entry["Entry points"]
        val["scripts/validate_eval_jsonl.py"]
        run["tooltests/run.py"]
        rae["evals/run_agent_evals.py"]
        rass["scripts/run_assert.py"]
        rep["scripts/report.py"]
        dep["scripts/deploy_toolbox.py<br/>scripts/deploy_agent.py<br/>scripts/setup_entra.py"]
    end
    subgraph shared["Shared modules in evals/"]
        cases["cases.py<br/>load and validate JSONL,<br/>fill caller placeholders"]
        target["foundry_agent/target.py<br/>Responses API client"]
        common["auth.py: Entra tokens, caller claims<br/>telemetry.py: spans"]
        terr["toolbox_errors.py<br/>consent and tool-source failures"]
        reasons["reasons.py<br/>failure reason codes"]
    end
    mcpsdk["mcp SDK client"]
    assertai["assert-ai"]
    az["scripts/_az.py<br/>az CLI and az rest with retries"]
    configs[("evals/configs/*.yaml")]
    files[("schemas/*.json<br/>data/*/*.jsonl")]
    results[("results/")]

    val --> cases
    run --> mcpsdk
    run --> cases
    run --> common
    run --> terr
    rae --> cases
    rae --> target
    rae --> common
    target --> common
    target --> terr
    rass --> configs
    rass --> assertai
    assertai -->|"imports target:chat"| target
    cases --> files
    rep --> results
    run & rae & rep --> reasons
    dep --> az
```

| Module | Responsibility |
| --- | --- |
| `tooltests/run.py` | MCP client for the toolbox: one session per run, outcome classification, assertions, latency percentiles, reconnects |
| `evals/run_agent_evals.py` | Replays multi-turn conversations against the agent and scores every turn |
| `evals/foundry_agent/target.py` | Calls the agent through the Responses API with retries, reads `mcp_call` and `message` output items, detects platform aborts and turns the agent never ran (`abort_reason`), and emits agent and tool spans. Its `chat()` is ASSERT's callable target |
| `evals/cases.py` | Finds, parses and validates JSONL cases (JSON Schema plus cross-field and cross-file rules), and fills `{{caller.*}}` placeholders |
| `evals/auth.py` | Entra tokens from `az login`, cached per scope and refreshed 5 minutes before expiry; the signed-in user's claims for placeholders |
| `evals/toolbox_errors.py` | Recognizes toolbox errors caused by a tool source (consent needed, or the source failed) and prints consent links to the console, the only place they appear |
| `evals/reasons.py` | Failure reason codes and what each means, shared by both runners and `report.py` |
| `evals/telemetry.py` | `span()` helper and the optional App Insights exporter; does nothing without a connection string |
| `scripts/run_assert.py` | Runs `assert-ai run` from the repo root with artifacts under `results/assert` |
| `scripts/report.py` | Markdown reports and baseline diffs for all three result types: pass rates, failures grouped by reason, ASSERT violations with the judge's justifications and the errors of cases it couldn't score |
| `scripts/validate_eval_jsonl.py` | Validates case files without running them |
| `scripts/deploy_toolbox.py`, `deploy_agent.py`, `_az.py` | Create the connection (no auth, OAuth2 or UserEntraToken), toolbox version and agent version through `az rest`; for OAuth2, also the client secret and redirect URI on the client app |
| `scripts/setup_entra.py` | Registers the Entra apps for the Entra sample through Microsoft Graph: the API app (scope, app roles, your role assignments) and the OAuth client app |
| `sample-mcp-server/` | Synthetic MCP server, packaged as a container image: three tools, plus Entra token validation (`entra_auth.py`), region filtering and `whoami` with `AUTH_MODE=entra` |
| `tests/` | Offline unit tests: no Azure access or `.env` needed |
| `.github/workflows/ci.yml` | CI on pushes to `main` and pull requests: dependency check, byte-compile, case validation and unit tests on Ubuntu (Python 3.11 and 3.14) and Windows (3.14). Offline only; live runs would need Azure credentials, for example OIDC |

## 4. Cases and results

### Case formats

Cases are JSONL, one object per line, validated against `schemas/tool-test.schema.json` and
`schemas/agent-eval.schema.json`.

```mermaid
classDiagram
    direction LR
    class ToolTestCase {
        +string case_id
        +string category
        +string kind
        +string tool
        +object arguments
        +any expect_error
        +list~Assertion~ assertions
        +int repetitions
        +list~string~ expect_tools
        +bool require_schemas
        +string token
        +int expect_status
        +int max_latency_ms
        +string notes
    }
    class Assertion {
        +string type
        +any value
        +string pattern
        +string path
        +object schema
    }
    class AgentEvalTurn {
        +string conversation_id
        +int turn_number
        +string user_message
        +list~ExpectedCall~ expected_tool_calls
        +bool ordered
        +list~string~ must_not_call
        +string expected_response_pattern
        +string response_must_not_match
        +string pass_criteria
        +string category
        +int max_latency_ms
        +string notes
    }
    class ExpectedCall {
        +string tool
        +object params
    }
    ToolTestCase "1" *-- "0..*" Assertion : assertions
    AgentEvalTurn "1" *-- "0..*" ExpectedCall : expected_tool_calls
```

Each tool-test `kind` uses its own fields. The validator flags fields the kind ignores.

| `kind` | Required | Optional |
| --- | --- | --- |
| `call` (default) | `tool`, `arguments` | `expect_error` (`false`, `true` or `"tool"`, `"protocol"`), `assertions`, `repetitions`, `max_latency_ms` |
| `list` | `expect_tools` | `require_schemas`, `max_latency_ms` |
| `auth` | `token` (`invalid` or `none`), `expect_status` | none |

Assertion types:

- `contains` (`value`, case-insensitive)
- `regex` (`pattern`)
- `json_path_equals` (`path`, `value`)
- `json_path_exists` (`path`)
- `schema` (`schema`)

For agent turns, `expected_tool_calls` is required but may be empty, for example when only the reply matters.

Any string in either case type may contain `{{caller.oid}}`, `{{caller.tid}}`, `{{caller.name}}` or `{{caller.username}}`.
The runners fill them from the signed-in user's Foundry token before the run, regex-escaping values in regex fields, so
identity checks need no real user in the files ([entra.md](entra.md#caller-placeholders)).

### Validation

Both runners validate every case before running any, and `scripts/validate_eval_jsonl.py` performs the same checks
without running them.

```mermaid
flowchart LR
    paths["paths and globs"] --> expand["expand_paths<br/>supports **"]
    expand --> read["parse each line<br/>as JSON"]
    read --> kind["pick the schema<br/>runner: fixed kind<br/>validator: --kind, folder or first record"]
    kind --> schema["JSON Schema<br/>Draft 2020-12"]
    schema --> rules["cross-field rules<br/>ignored fields, bad regex or embedded schema,<br/>must_not_call overlapping expected calls,<br/>unknown caller placeholders"]
    rules --> files["cross-file rules<br/>duplicate ids, turn gaps"]
    files --> ok{"any problems?"}
    ok -->|"no"| cases(["cases"])
    ok -->|"yes"| stop(["runner exits 2<br/>validator exits 1"])
```

The runners then fill caller placeholders. They read the token's claims only if a case uses a placeholder, and a
placeholder whose claim the token lacks exits 2.

### Result files

```mermaid
classDiagram
    direction LR
    class ToolRun {
        +string endpoint
        +string timestamp
        +string protocol_version
        +object server
        +list~string~ tools
        +list~ToolResult~ results
    }
    class ToolResult {
        +string case_id
        +string kind
        +string tool
        +string category
        +bool passed
        +list~string~ failures
        +list~string~ reasons
        +int runs
        +object outcomes
        +float latency_p50_ms
        +float latency_p95_ms
        +string sample_output
    }
    class AgentRun {
        +string agent
        +string timestamp
        +list~AgentResult~ results
    }
    class AgentResult {
        +string conversation_id
        +int turn_number
        +string category
        +bool passed
        +list~string~ failures
        +list~string~ reasons
        +bool platform_abort
        +string abort_reason
        +bool history_gap
        +float latency_ms
        +int attempts
        +list~object~ tool_calls
        +string response
    }
    ToolRun "1" *-- "1..*" ToolResult : results
    AgentRun "1" *-- "1..*" AgentResult : results
```

- `outcomes` counts outcomes across repetitions.
- `reasons` runs parallel to `failures`: `reasons[i]` is the code for `failures[i]`, described in `evals/reasons.py`.
  Reports group failures by it, and spans carry the distinct codes as `eval.reasons`.
- `latency_ms` and `attempts` are null when a turn raised.
- `abort_reason` is `tool_error`, `consent_required` or `tool_listing_failed` when `platform_abort` is true.
- `report.py` matches results to a baseline by `case_id`, or by conversation and turn.

## 5. Tool tests

### Run sequence

```mermaid
sequenceDiagram
    autonumber
    actor U as Developer or CI
    participant R as tooltests.run
    participant A as evals.auth
    participant TB as Toolbox MCP endpoint
    participant S as MCP server
    U->>R: python -m tooltests.run cases.jsonl
    R->>R: resolve the endpoint, load and validate cases
    R->>A: get_token(scope) as a preflight
    A-->>R: token from az login, cached
    R->>TB: initialize with a bearer token
    TB-->>R: server info and protocol version
    R->>TB: tools/list, every page
    TB-->>R: tools with input and output schemas
    loop each case, on one MCP session
        alt kind call, once per repetition
            R->>TB: tools/call sample___get_customer
            TB->>S: tools/call get_customer
            S-->>TB: result, or a result with isError
            TB-->>R: result
            R->>R: classify the outcome, compare with expect_error, run assertions
        else kind list
            R->>TB: tools/list
            TB-->>R: tools
            R->>R: check expect_tools, duplicates and tool definitions
        else kind auth
            R->>TB: initialize over plain HTTP with a bad or missing token
            TB-->>R: HTTP status, compared with expect_status
        end
    end
    R->>R: write results/tool-tests/run-stamp.json
    R-->>U: PASS or FAIL per case, exit code 0, 1 or 2
```

- Before each timed request the runner calls `auth.get_token`, which serves the cached token, so a token refresh never
  counts toward a call's latency.
- `auth` cases use a separate HTTP client, because the MCP session cannot send a bad token.

### Outcomes

Every request ends in exactly one outcome:

```mermaid
flowchart TD
    req["MCP request"] --> raised{"raised an error?"}
    raised -->|"no"| iserr{"result has isError?"}
    iserr -->|"no"| ok(["ok"])
    iserr -->|"yes"| toolerr(["tool_error"])
    raised -->|"MCPError plus a non JSON-RPC HTTP error,<br/>connection closed, or request timeout"| transport(["transport_error"])
    raised -->|"other MCPError<br/>a JSON-RPC error response"| source{"caused by a tool source?<br/>toolbox_errors.classify"}
    source -->|"needs the user's consent"| consent(["consent_required<br/>link to the console only"])
    source -->|"failed, for example HTTP_401"| upstream(["upstream_error"])
    source -->|"no"| protocol(["protocol_error"])
    raised -->|"RuntimeError or ValidationError<br/>result breaks outputSchema or MCP shape"| invalid(["invalid_result"])
    raised -->|"bug in the runner itself"| runner(["runner_error"])
```

A run passes when both of these hold:

1. Its outcome is the expected one. `expect_error` maps to the expected outcome:
   - `false` → `ok`
   - `true` or `"tool"` → `tool_error`
   - `"protocol"` → `protocol_error`
2. Every assertion holds.

Assertions run only on the expected outcome, so they can check error text as well.

- `consent_required`, `upstream_error`, `invalid_result`, `transport_error` and `runner_error` always fail.
- A case passes when every repetition passes and none exceeds `max_latency_ms`.
- The results record p50 and p95 latency across repetitions.
- Each failure records a reason: the unexpected outcome, `unexpected_success` (an expected error didn't happen),
  `assertion` or `latency`. Listing and auth cases add `missing_tool`, `duplicate_tool`, `tool_definition` and
  `auth_status`, and cases left unrun after a lost session get `not_run`.

### Session lifecycle

```mermaid
stateDiagram-v2
    [*] --> Connect
    Connect --> Run: initialize and tools/list succeed
    Connect --> SetupError: fails before any session existed,<br/>for example a tool source needs consent
    Connect --> Retry: fails after an earlier session
    Run --> Run: next case
    Run --> Retry: session lost, the current case fails as transport_error
    Retry --> Connect: reconnect 1 to 3, after 2, 4 or 8 s
    Retry --> Done: a 4th loss, remaining cases fail as not run
    Run --> Done: all cases ran
    SetupError --> [*]: exit 2, consent links to the console
    Done --> [*]: write results, exit 0 or 1
```

The HTTP client has no read timeout. A read timeout inside an MCP POST tears down the whole session, so the MCP-level
request timeout (`--timeout`, default 60 s) bounds each request instead and reports `transport_error`. Connection
attempts are retried by the HTTP transport (3 retries).

## 6. Agent evals

### One agent turn

```mermaid
sequenceDiagram
    autonumber
    participant R as run_agent_evals or ASSERT
    participant T as target.py
    participant F as Foundry agent service
    participant M as Agent model
    participant TB as Toolbox
    participant S as MCP server
    R->>T: run_agent(messages) or chat(message, history)
    T->>F: POST /openai/v1/responses<br/>agent_reference, bearer token, traceparent
    F->>TB: tools/list as the signed-in user
    break a tool source needs the user's consent
        TB-->>F: CONSENT_REQUIRED with a consent link
        F-->>T: 200 with an oauth_consent_request item and no reply
        T-->>R: abort_reason consent_required, the link to the console only
    end
    break a tool source fails to list its tools
        TB-->>F: tool source error, for example HTTP_401
        F-->>T: HTTP 400 McpProtocolException encountered while enumerating tools
        T-->>R: abort_reason tool_listing_failed
    end
    TB-->>F: tools
    F->>M: instructions, conversation and toolbox tools
    loop until the model answers
        M-->>F: tool call, for example sample___get_customer
        F->>TB: tools/call
        TB->>S: tools/call get_customer
        S-->>TB: result, or a result with isError
        break the tool returned isError
            TB-->>F: error result
            F-->>T: HTTP 400 An error occurred invoking 'tool'
            T-->>R: abort_reason tool_error, tool name only, abort note as text
        end
        TB-->>F: result plus a structuredResponse block
        F->>M: tool output
    end
    M-->>F: final answer
    F-->>T: 200, status completed, mcp_call and message output items
    T->>T: strip structuredResponse, emit a span per tool call
    T-->>R: text, tool_calls, latency_ms, attempts
```

The agent service does not hand tool errors back to the model. An `isError` result fails the whole Responses call, so the
agent never gets a chance to recover. The target reports this as `platform_abort`, with the tool name parsed from the error and no
arguments. The `tool_error_handling` ASSERT config catches the raw failure text that users would see.

When the agent can't run at all, because a tool source needs the user's consent or can't list its tools, the target sets
`abort_reason` to `consent_required` or `tool_listing_failed` and notes `[AGENT NOT RUN: ...]` instead of a reply. Such
turns fail before any check runs, so a check like `must_not_call` can't pass on a turn that never happened.

### Retries

```mermaid
flowchart TD
    a["attempt n of 8"] --> tok["auth.get_token, cached"] --> post["POST /openai/v1/responses<br/>180 s timeout"]
    post --> r{"result"}
    r -->|"200"| ok(["return the response,<br/>this attempt's latency and n"])
    r -->|"HTTP 408, 429, 500, 502, 503 or 504"| hint["wait for retry-after-ms or Retry-After, up to 60 s,<br/>else 5, 10, 20, then 30 s"]
    r -->|"URLError or ConnectionError"| back["wait 5, 10, 20, then 30 s"]
    r -->|"other HTTP status"| err(["raise ResponsesAPIError<br/>HTTP 400 tool error or tool-listing<br/>failure becomes platform_abort"])
    r -->|"read timeout"| tmo(["raise, not retried:<br/>tools may already be running"])
    hint --> more{"n less than 8?"}
    back --> more
    more -->|"yes"| a
    more -->|"no"| last(["raise the last error"])
```

`latency_ms` covers the final attempt only, so token fetches and retry waits are excluded. `attempts` records how many
tries the turn needed.

### Scoring a turn

| Check | Applies when | Rule | Failure reason |
| --- | --- | --- | --- |
| `expected_tool_calls` | `pass_criteria` is `tool_called_correctly` or `both` | See the matching rules below | `missing_call`, `wrong_arguments` or `wrong_order` |
| `must_not_call` | always | None of these tools was called | `forbidden_call` |
| `expected_response_pattern` | `pass_criteria` is `response_matches` or `both` | Case-insensitive regex search over the whole reply | `response_mismatch` |
| `response_must_not_match` | always | The reply does not match | `forbidden_response` |
| `max_latency_ms` | always, if set | Latency of the final HTTP attempt | `latency` |

A turn the agent never ran (`abort_reason` `consent_required` or `tool_listing_failed`) fails with its note and the abort
reason as its failure reason, and no other check runs. A turn whose request raised fails with `runner_error`.

How `expected_tool_calls` are matched:

- Each expected call must match a call the agent made, with the same name and every listed param equal. Strings compare
  case-insensitively, and a `re:` prefix makes a param a regex search.
- Extra params and extra calls are allowed.
- With `ordered`, the matches must occur in order.
- A platform-aborted call matches on name only, because its arguments are unknown.
- An unmatched expected call fails as `wrong_order` when a matching call was made out of order, as `wrong_arguments` when
  the tool was called but with other arguments (the message lists them), and as `missing_call` otherwise.

### Multi-turn conversations

```mermaid
flowchart TD
    group["group turns by conversation_id,<br/>sort by turn_number"] --> next["next turn"]
    next --> user["append the user message to the history"]
    user --> invoke["run_agent with the full history,<br/>inside an eval.turn span"]
    invoke --> score["score_turn<br/>an exception fails the turn"]
    score --> answered{"agent answered?"}
    answered -->|"yes"| asst["append the reply to the history"]
    answered -->|"no: runner error or platform abort"| gap["later turns report history_gap"]
    asst --> more{"more turns?"}
    gap --> more
    more -->|"yes"| next
    more -->|"no"| conv(["next conversation"])
```

Each request replays the conversation as text: every user message, plus the assistant replies received so far. Tool
results from earlier turns are not replayed, so the agent calls tools again when it needs the data. Using
`previous_response_id` or Foundry conversations would keep that state; this is listed under Next in [plan.md](plan.md).

## 7. ASSERT

[ASSERT](https://commandline.microsoft.com/assert-written-intent-executable-evals/) (`assert-ai`) turns a written behavior
into generated test cases, runs them against the agent, and has an LLM judge each transcript. Each config in
`evals/configs/` covers one behavior.

```mermaid
flowchart LR
    cfg["evals/configs/*.yaml<br/>behavior, context, pipeline"] --> wrap["scripts/run_assert.py"]
    wrap -->|"assert-ai run<br/>artifacts_root = results/assert"| sys
    subgraph suitestages["Suite stages, cached per suite"]
        sys["systematize<br/>behavior categories"] --> ts["test_set<br/>stratified prompts and scenarios"]
    end
    subgraph runstages["Run stages, once per run"]
        inf["inference<br/>prompts, or tester-driven<br/>scenarios up to max_turns"] --> judge["judge<br/>config dimensions plus<br/>safety-extended preset"]
    end
    ts --> inf
    inf <-->|"chat(message, history)<br/>reply and spans"| tgt["target.py"]
    tgt --> agent["Foundry agent<br/>and toolbox"]
    judge --> out[("scores.jsonl<br/>metrics.json")]
    out --> rep["scripts/report.py assert"]
```

ASSERT's own model calls go through LiteLLM to `azure/<deployment>`:

- They include category generation, test generation, the tester that plays the user, and the judge.
- They use `AZURE_API_BASE`, with Entra auth when `ASSERT_AZURE_USE_AAD=1`.
- The judge needs a large `max_tokens` (24000) for a reasoning model.

Completed stages are cached, so a new run reuses the suite's categories and test set.

If the agent never ran (missing consent, or its tools couldn't be listed), `chat()` raises `AgentNotRun`. ASSERT records
that as a target error and leaves it unscored, so a setup problem isn't judged as a behavior violation. A `tool_error`
abort still returns its note, for the judge to see.

```text
results/assert/results/<suite>/
  taxonomy.json, test_set.jsonl, ...   suite stages, reused by later runs
  <run>/                               for example baseline
    inference_set.jsonl                transcripts with per-turn trace data
    scores.jsonl, metrics.json         judge verdicts and metrics
    manifest.json, config.yaml         stage status and the resolved config
```

`report.py assert` reads `scores.jsonl` for the violation rate per dimension and the judge's justification for each
violation, and `inference_set.jsonl` for the target error behind each case the judge couldn't score.

### How the judge sees tool calls

```mermaid
sequenceDiagram
    autonumber
    participant I as ASSERT inference
    participant T as target.py
    participant P as OTel tracer provider
    participant F as Foundry agent
    I->>T: import evals.foundry_agent.target
    T->>P: telemetry.init creates the provider<br/>and adds App Insights export if configured
    I->>P: attach an in-process span collector
    loop each turn
        I->>T: chat(message, history)
        T->>F: Responses API call
        F-->>T: output items
        T->>P: foundry_agent span with a tool span per call
        T-->>I: reply text
        I->>P: collect this turn's spans
    end
    I->>I: save transcript and spans to inference_set.jsonl
    Note over I: the judge reads transcripts with their tool spans,<br/>so it can check each stated figure against tool output
```

## 8. Authentication and configuration

### Tokens

```mermaid
flowchart LR
    caller["runner or target"] --> gt["auth.get_token(scope)"]
    gt --> hit{"cached token valid<br/>for 5 more minutes?"}
    hit -->|"yes"| use(["use the cached token"])
    hit -->|"no"| cli["AzureCliCredential<br/>az account get-access-token"]
    cli -->|"success"| cache["cache per scope"] --> use
    cli -->|"not signed in, or az missing"| fail(["raise: preflight exits 2"])
    cli -->|"other failure"| retry["retry after 2 s, then 4 s<br/>3 attempts in all"] --> cli
```

| Caller | Credential | Audience |
| --- | --- | --- |
| Tool runner, agent target | `evals.auth` (AzureCliCredential, cached, thread-safe) | `https://ai.azure.com/.default`; the tool runner can take `token_scope` from `--config` |
| Caller placeholders | `auth.caller_claims` reads the claims of that same cached token, without a signature check | None: no extra token |
| ASSERT model calls | assert-ai's Entra auth (`ASSERT_AZURE_USE_AAD=1`) | Azure OpenAI at `AZURE_API_BASE` |
| App Insights export | AzureCliCredential if `APPLICATIONINSIGHTS_USE_AAD=1`, else the connection string's key | App Insights ingestion |
| Deploy scripts | `az rest` | Azure Resource Manager (connection), `https://ai.azure.com` (toolbox, agent), Microsoft Graph (`setup_entra.py`, and the OAuth client's secret and redirect URI) |
| Toolbox to the sample MCP server | The connection's `authType: None` | Unauthenticated sample server |
| Toolbox to the Entra sample server | The connection's `authType: OAuth2`: the consenting user's token, refreshed with `offline_access` | `api://<api-app-id>/user_impersonation`, validated by the server |

### Toolbox endpoint

```mermaid
flowchart TD
    e{"--endpoint given?"} -->|"yes"| use1(["use it"])
    e -->|"no"| c{"--config given?"}
    c -->|"yes"| use2(["toolbox.mcp_endpoint and<br/>optional token_scope from the file"])
    c -->|"no"| p{"FOUNDRY_PROJECT_ENDPOINT set?"}
    p -->|"yes"| use3(["project endpoint + /toolboxes/name/mcp?api-version=v1<br/>name: --toolbox, FOUNDRY_TOOLBOX_NAME or sample-customer-toolbox"])
    p -->|"no"| err(["setup error, exit 2"])
```

### Settings

Settings live in the git-ignored `.env`; the template is `.env.example`. Every entry point loads `.env` from the repo
root, and `run_assert.py` runs ASSERT from the repo root so it loads the same file.

| Variable | Used by | Purpose |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | Target, deploy scripts, tool runner (default endpoint) | `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| `FOUNDRY_TOOLBOX_NAME` | Tool runner, deploy scripts | Toolbox name, default `sample-customer-toolbox` |
| `FOUNDRY_AGENT_NAME` | Target, `deploy_agent.py` | Agent name, default `customer-assistant` |
| `AZURE_API_BASE`, `ASSERT_AZURE_USE_AAD` | assert-ai | Endpoint and Entra auth for ASSERT's models |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | `evals/telemetry.py` | Turns on span export |
| `APPLICATIONINSIGHTS_USE_AAD` | `evals/telemetry.py` | Entra auth for ingestion |
| `OTEL_SERVICE_NAME` | `evals/telemetry.py` | Overrides the cloud role name |
| `ENTRA_TENANT_ID`, `ENTRA_API_APP_ID` | `setup_entra.py` (writes), `deploy_toolbox.py`; the Entra sample server as Container App settings | The Entra sample's tenant and API app |
| `ENTRA_OAUTH_CLIENT_APP_ID` | `setup_entra.py` (writes), `deploy_toolbox.py --auth-type OAuth2` | The OAuth client app for identity passthrough |

## 9. Telemetry

`telemetry.init` always installs an OpenTelemetry tracer provider, so spans exist in-process; ASSERT's collector reads the
target's spans from it. Spans are exported only when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set.

```mermaid
flowchart LR
    subgraph local["Eval process"]
        tc["toolbox.test_case"]
        turn["eval.turn"] --> fa["foundry_agent<br/>AGENT span"]
        fa --> tool["tool:name<br/>TOOL span, zero-duration marker"]
    end
    subgraph server["Foundry, cloud role responsesapi"]
        inv["invoke_agent"] --> chat["chat model"]
        inv --> exec["execute_tool"]
    end
    fa -.->|"W3C traceparent header"| inv
    local -->|"BatchSpanProcessor and<br/>AzureMonitorTraceExporter"| appi[("Application Insights<br/>dependencies table")]
    server -->|"if the project is connected<br/>to the same resource"| appi
```

The first entry point to install the tracer provider sets the cloud role:

| Run | Cloud role | Spans |
| --- | --- | --- |
| Tool tests | `foundry-toolbox-tests` | `toolbox.test_case` |
| Deterministic agent evals | `foundry-agent-evals` | `eval.turn`, with the target's `foundry_agent` and `tool:*` spans as children |
| ASSERT, or the target run on its own | `foundry-agent-target` | `foundry_agent`, `tool:*` |

`OTEL_SERVICE_NAME` overrides all three. Because the target injects `traceparent`, Foundry's server spans share the eval
turn's `operation_Id`, so one trace runs from the eval turn through the model and tool calls. Use the server-side
`execute_tool` spans for real tool latency. The `foundry_agent` span carries `eval.platform_abort` and `eval.abort_reason`;
consent links never reach a span.

## 10. Reliability

| Layer | Failure | Handling |
| --- | --- | --- |
| `evals/auth.py` | `az` token call fails | 3 attempts (2 s, 4 s); fails fast when not signed in or `az` is missing |
| Tool runner, HTTP | Connection fails | HTTP transport retries (3) |
| Tool runner, session | Session lost mid-run | The current case fails as `transport_error`; reconnects up to 3 times (2, 4, 8 s); after that, the remaining cases are reported as not run |
| Tool runner, first connect | Any failure, including a tool source that needs consent | Setup error, exit 2; consent links are printed to the console |
| Agent target | HTTP 408, 429, 5xx, connection errors | Up to 8 attempts, honoring `Retry-After` (up to 60 s), else 5, 10, 20, 30 s backoff (about 2.5 minutes in total) |
| Agent target | Consent needed, or the agent's tools can't be listed | Not retried; the turn fails as not run, and ASSERT records a target error |
| Agent target | Read timeout (180 s) | Not retried; the turn fails |
| Agent-eval runner | Any exception in a turn | The turn fails with a runner error; the run continues |
| Deploy scripts (`_az.rest`) | DNS, connection, 429, 502, 503, 504 | 6 attempts, waiting 10, 20, 30, 40, 50 s |
| ASSERT | Interrupted run | Completed stages are cached and reused |

All runners exit 0 when every case passes, 1 when any fails, and 2 on a setup problem (invalid cases, config, sign-in,
consent, connection).

## 11. Sample deployment

```mermaid
flowchart LR
    subgraph repo["This repo"]
        src["sample-mcp-server/<br/>server.py, data.py, Dockerfile"]
        dtb["scripts/deploy_toolbox.py"]
        dag["scripts/deploy_agent.py"]
    end
    subgraph azure["Azure"]
        acr["Container Registry<br/>image sample-mcp:tag"]
        aca["Container App<br/>/mcp on port 8000, no auth"]
        subgraph proj["Foundry project"]
            conn["Connection sample-mcp-conn<br/>RemoteTool, authType None"]
            tb["Toolbox sample-customer-toolbox<br/>server_label sample"]
            ag["Prompt agent customer-assistant<br/>model gpt-4-1-mini"]
        end
    end
    src -->|"az acr build"| acr
    acr -->|"az containerapp create or update"| aca
    dtb -->|"PUT connection via ARM"| conn
    dtb -->|"POST version, PATCH default_version"| tb
    dag -->|"POST agent version"| ag
    conn -.->|"target URL"| aca
    tb -->|"tools/call"| aca
    ag -->|"MCP tool: toolbox endpoint"| tb
```

The server uses the `mcp` SDK's `MCPServer` and serves Streamable HTTP at `/mcp`. Its data is synthetic (`data.py`). A
`ToolError` reaches the caller as an `isError` result that keeps its message.

The Entra stack deploys beside this one from the same image, with `AUTH_MODE=entra`, an OAuth2 connection, its own
toolbox and agent; its diagram is in [entra.md](entra.md#3-what-the-sample-deploys). In that mode `entra_auth.py`
accepts only Entra user tokens issued to the server's API app, and the tools authorize from the token's app roles:
`get_customer`, and `get_stock_quote` by customer ID, return a normal `access_denied` result for customers outside the
caller's regions. Denials aren't tool errors, because a tool error would abort the whole agent request.

| Tool, as named in the toolbox | Input | Returns | Error cases |
| --- | --- | --- | --- |
| `sample___get_customer` | `customer_id` (for example `C1001`) or `name` | Profile: industry, tier, region, account manager, contract value, ticker | Unknown customer or name, no input |
| `sample___get_market_conditions` | `industry`: manufacturing, healthcare, energy or retail | Outlook, YoY growth, sentiment, drivers, risks | Unsupported industry, missing argument |
| `sample___get_stock_quote` | `customer_id` or `ticker` | Price, change, market cap, P/E | Privately held customer (`C1005`), unknown ticker, no input |
| `sample___whoami`, only with `AUTH_MODE=entra` | none | The caller as the server sees them: name, username, `oid`, `tid`, roles, scopes, client app, token version, visible regions | none |

Deploy steps and current findings: [getting-started.md](getting-started.md) and [deploy/README.md](../deploy/README.md).

## 12. Platform behaviors the design handles

| Behavior | How the repo handles it |
| --- | --- |
| The toolbox names tools `<server_label>___<tool>` | Cases call `sample___get_customer` and so on |
| An unknown unprefixed tool returns JSON-RPC error -32602 "No tool config matches…" | `expect_error: "protocol"` |
| An unknown prefixed tool returns an `isError` result "Unknown tool: x" | `expect_error: "tool"` |
| The toolbox appends a `structuredResponse` block to tool text | Stripped by the tool runner (`split_output`) and the target (`_tool_output_text`) |
| A tool error inside an agent run fails the whole Responses call (HTTP 400) | `platform_abort`, `history_gap`, and the `tool_error_handling` ASSERT behavior |
| Local DNS and connection failures lasting minutes have been observed | Retries in the target and in `_az.rest` |
| The toolbox reports a failing tool source (consent needed, or the server rejected the token) as a JSON-RPC error, and the agent as an `oauth_consent_request` item or HTTP 400 | `toolbox_errors.classify`: the outcomes `consent_required` and `upstream_error`, and the abort reasons `consent_required` and `tool_listing_failed` |
| A consent link binds whoever completes it to the user's connection | Printed to the console only; never in results, failure text, spans or ASSERT transcripts |
| `UserEntraToken` can't get a token for a custom MCP server's audience | The Entra sample uses OAuth2 identity passthrough |
| A denial returned as a tool error would abort the agent request | The Entra sample returns denials as normal `access_denied` results |

## 13. Design decisions

- **Tests vs. evals.**
  - Tool tests isolate the toolbox, so a failure points at the toolbox or the MCP server rather than at the model.
  - Agent evals measure what users get.
- **REST instead of the Foundry SDK.** `azure-ai-projects` requires `openai>=3`, which conflicts with assert-ai's
  `openai<3` pin, so the target calls the Responses API with `urllib`.
- **The official `mcp` client.**
  - The tool runner uses the same protocol stack as real MCP clients, with one session per run.
  - It has no HTTP read timeout; each request is bounded by an MCP request timeout instead.
- **Two agent scorers.** Deterministic checks give exact, cheap, repeatable regression signals. ASSERT covers behaviors
  that need judgment.
- **Validate first.** Bad cases stop a run before any Azure call. Exit codes are the same across runners, so CI can tell
  setup problems from test failures.
- **Results are local JSON.** Reports and baseline diffs read those files; telemetry is optional and is not the record.
- **Text replay for multi-turn.** Every request carries the conversation so far, which keeps runs stateless and makes any
  turn safe to retry. The cost is that earlier tool results are not replayed.
- **No environment values in git.**
  - Tenant, subscription and resource identifiers live only in `.env`, `config/*.local.json` and
    `deploy/resources.local.md`, all git-ignored.
  - Case data is synthetic.
- **Per-user identity as the signed-in user.**
  - The runners act as the `az login` user, so a test sees what that user's agent would.
  - Cases name no real user: `{{caller.*}}` placeholders fill in the identity at run time.
  - OAuth2 identity passthrough is the supported path, because `UserEntraToken` doesn't work for custom servers.
- **Consent links stay on the console.** Whoever completes a link binds their account to the connection, so the runners
  treat links like secrets.

## 14. Extending

| Goal | Where |
| --- | --- |
| Test another toolbox or agent | `.env`, or `--endpoint`, `--config`, `--toolbox`; see [existing-environment.md](existing-environment.md) |
| Add tool tests | A JSONL file under `data/tool-tests/`; check it with `scripts/validate_eval_jsonl.py` |
| Add agent scenarios | A JSONL file under `data/agent-evals/` |
| Add an ASSERT behavior | A new config in `evals/configs/` (one behavior per config), run with `scripts/run_assert.py` |
| Add an assertion type | `$defs.assertion` in `schemas/tool-test.schema.json`, `ASSERTION_FIELDS` in `evals/cases.py`, `check()` in `tooltests/run.py`, plus unit tests |
| Add a failure reason | `Failure("code", message)` in the runner, and the code's meaning in `TOOL_REASONS` or `AGENT_REASONS` in `evals/reasons.py`; `tests/test_reasons.py` fails on undescribed codes |
| Add a span attribute | `telemetry.span(...)` calls in the runners; document it in [telemetry.md](telemetry.md) |
| Test a toolbox that passes the user's identity | [entra.md](entra.md): consent once, then allowed and denied cases that use `{{caller.*}}` placeholders |
| Add a caller placeholder | `CALLER_FIELDS` in `evals/cases.py` and `auth.caller_claims`, plus unit tests |
