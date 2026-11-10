# Sample toolbox deployment

Real values for this deployment are kept in the git-ignored `deploy/resources.local.md`.

| Item | Value |
| --- | --- |
| Foundry resource / project | `<foundry-resource>` (rg `<foundry-rg>`) / `<project>` |
| MCP host | Container App `sample-mcp` in `<sample-rg>` (`<region>`), ACR `<acr-name>`, `--min-replicas 1` |
| Project connection | `sample-mcp-conn` (category `RemoteTool`, auth `None`) |
| Toolbox | `sample-customer-toolbox` (v1, default) |
| Consumer endpoint | `{project_endpoint}/toolboxes/sample-customer-toolbox/mcp?api-version=v1` |
| CI identity | User-assigned managed identity `id-toolbox-eval-ci` in `<sample-rg>`: Foundry User on the Foundry resource, which Foundry's evaluators need to call the judge as the identity, and on the project; a federated credential for the repository's `live` environment ([docs/live-ci.md](../docs/live-ci.md)) |

The host is unauthenticated (synthetic data only). Calls to the toolbox need a bearer token for `https://ai.azure.com/.default`
(`az account get-access-token --resource https://ai.azure.com`).

The Entra stack ([docs/entra.md](../docs/entra.md)) runs beside it:

| Item | Value |
| --- | --- |
| Entra MCP host | Container App `sample-mcp-entra` in `<sample-rg>`: the same image with `AUTH_MODE=entra`, `--min-replicas 1` |
| Entra apps | API app `api://<api-app-id>` (scope `user_impersonation`, app roles `Customers.Read.*`) and the OAuth client app |
| Project connection | `sample-mcp-oauth-conn` (category `RemoteTool`, auth `OAuth2`) |
| Toolbox | `sample-customer-toolbox-oauth` |
| Agent | `customer-assistant-oauth` |

## Findings

- Toolbox tool names use `{server_label}___{tool}` (three underscores), e.g. `sample___get_customer`, not the dotted form shown in the docs.
- An unknown name without a server prefix returns a JSON-RPC error (-32602 `No tool config matches tool name ...`), not a tool
  result. An unknown name with a valid prefix (`sample___nope`) is passed to the MCP server and comes back as `isError`.
- Tool errors from the MCP server surface as `isError: true` with the original message, and so do missing or wrong-type
  arguments (with the validation message).
- **Typed outputs pass through.** The toolbox lists each tool's `outputSchema` unchanged, including an `anyOf` of two
  object shapes, and direct calls return the server's `structuredContent`. A redeployed server's new schemas were listed
  at once, without redeploying the toolbox. Inside an agent run, the `mcp_call` output is the tool's text followed by a
  `structuredResponse` block that holds the `structuredContent`, so the model reads the data twice.

- **Tool errors reach the model as text.** When an MCP tool returns `isError: true` while an agent is calling it through
  the toolbox, the model gets the error message (`Error executing tool get_stock_quote: Customer C1005 ... is privately
  held; ...`) as the tool output and can explain it to the user. The `mcp_call` output item still reports
  `status: completed` and `error: null`, so only the text shows that the call failed. Direct toolbox calls are unaffected
  (tool tests see `isError`).
- **Through October 2026 a tool error aborted the whole agent request.** The Responses API returned HTTP 400
  (`An error occurred invoking 'sample___...'`) instead of giving the error to the model, so the user got no reply. The
  runners still handle it: the agent runner fails the turn as `platform_abort`, and the ASSERT target returns an
  `[AGENT FAILURE ...]` reply for the judge to score.
- Responses API calls can hit transient DNS errors; the target retries connection errors, 408, 429 and 5xx (honoring `Retry-After`).
- **Don't let the MCP server scale to zero.** Tried in November 2026 and reverted. A Container App starting from zero
  took about 20 s, and agent requests in that window failed with HTTP 400 `McpProtocolException encountered while
  enumerating tools` (or `while invoking tool <name>`): "The server does not support the requested protocol version
  '2026-07-28' ... The server appears to require the initialize handshake." That message comes from the MCP C# SDK: a
  client pinned to protocol version 2026-07-28 whose `server/discover` probe gets no answer in time fails instead of
  falling back to the initialize handshake. The toolbox gave up after about 10 s. In three cold runs, the first two agent
  turns failed and later turns passed, on the open and the OAuth toolbox alike. The runners report such turns as
  `platform_abort` with `abort_reason` `tool_listing_failed` or `tool_call_failed`. Direct calls waited instead: a cold
  `tools/list` took about 19 s and succeeded, and those tool tests passed.
- **A replica can get stuck starting.** On one cold start the OAuth server's new replica pulled its image but never
  started its container, for over 15 minutes. Each OAuth `tools/call` spent about 150 s in the toolbox before failing,
  so every call case in the tool tests timed out (`transport_error` after the runner's 60 s timeout); `tools/list` was
  unaffected. A new revision replaced the replica.

## Entra findings

- The toolbox endpoint accepts only tokens for `https://ai.azure.com`; tokens for other Azure audiences get 401. The MCP
  server never sees this token.
- **`UserEntraToken` fails for a custom MCP server.** Foundry's on-behalf-of token service answers HTTP 400 "Audience for
  incoming token is incorrect" for `api://<api-app-id>` and for the bare app ID. Toolbox `tools/list` returns -32007
  (`HTTP_400`), `tools/call` returns -32602 "Connection resolution failed...", and the agent gets HTTP 400
  `McpProtocolException`. The same user token works through a `UserEntraToken` connection to `https://mcp.ai.azure.com`.
- **`OAuth2` identity passthrough works**, with the Entra v2 authorize and token URLs, the refresh URL set to the token URL,
  scopes `api://<api-app-id>/user_impersonation offline_access`, and a client ID and secret.
- Each OAuth2 connection gets its own redirect URI, `https://global.consent.azure-apim.net/redirect/<connector>`, which must
  be registered on the client app. Rerunning `deploy_toolbox.py` (which rotates the secret) kept the URI and users' consent,
  and calls more than two hours later still worked, so the connection refreshes users' tokens with the new secret.
- Consent is per user, per connection. Toolbox errors are HTTP 200 with a JSON-RPC error:

| Request | Failure | Code | Message |
| --- | --- | --- | --- |
| `tools/list` | consent needed | -32006 | `tools/list failed for 1 tool source(s) ... {"errors":[{"name":"sample","type":"mcp","error":{"code":"CONSENT_REQUIRED","message":"<link>"}}]}` |
| `tools/list` | server rejected the token | -32006 | The same format with code `HTTP_401` |
| `tools/call` | consent needed | -32006 | The consent link itself; `data.grpcStatus` is `FailedPrecondition` |
| `tools/call` | server rejected the token | -32002 | `Authentication failed when connecting to the MCP server: ...`; `data.grpcStatus` is `Unauthenticated` |

- Through the agent, missing consent completes the response with an `oauth_consent_request` output item (`server_label`,
  `consent_link`) and no message. A tool source that fails to list returns HTTP 400 `McpProtocolException encountered while
  enumerating tools` with the same error list inside.
- Tool calls through the OAuth2 connection took about 1.1-1.5 s, against about 0.3 s through the unauthenticated one;
  `tools/list` took about 0.3 s on both. Both apps kept one warm replica. Toolbox spans in Application Insights put the
  difference inside Foundry: an OAuth `tools/call` spent 490-980 ms in the toolbox, against 90-115 ms without auth, and
  the rest of the round trip (network and client) was about 150-190 ms in both.

## ASSERT findings

Both behaviors ran in November 2026 against `customer-assistant` (`gpt-4-1-mini`), judged by `gpt-5.6-sol`, and every
case was scored. The v2 runs are the baselines for later ones (`report.py assert <run> --baseline <run>`). Versions 3 and
4 add grounding rules to the agent's instructions (`scripts\deploy_agent.py`), each written against the violations of the
version before. Cases violated, prompts and scenarios together:

| Behavior | Cases | Dimension | v2 (baseline) | v3 | v4 |
| --- | ---: | --- | ---: | ---: | ---: |
| `ungrounded_figures` | 36 | `figure_grounding` | 15 | 11 | 9 |
| `ungrounded_figures` | 36 | `overrefusal` | 12 | 9 | 6 |
| `tool_error_handling` | 18 | `graceful_error` | 2 | 1 | 1 |
| `tool_error_handling` | 18 | `policy_violation` | 3 | 3 | 1 |

In the baseline:

- **Under pressure, the agent filled gaps with its own figures.** In 13 of the 15 ungrounded-figures violations the user
  insisted. In 10, a tool had returned no data (a privately held company, an unknown ticker or customer, an unsupported
  industry): the agent said so, then supplied a share price, P/E, valuation, growth rate or contract value anyway, twice
  with advice to put it in a valuation memo or a lender appendix (`harm_actionability`). The other five: a currency
  conversion at an invented rate, a contract value that contradicted the tool's, a user's remembered figure restated as
  confirmed, manufacturing data stretched to aerospace, and a claim to have checked records the tools don't return.
- The judge's `overrefusal` flags fell on those same replies. None was a refusal: each missed a permissible behavior
  (relay the data faithfully, say it is unavailable, or ask which customer) through the same ungrounded content.
- **Tool errors were mostly handled well.** Two replies to an unsupported industry didn't name the supported ones, though
  the tool's error lists them, and one turned an unknown ticker into a claim that no listing exists anywhere. One
  `pii_leakage` flag was for naming the customer's (synthetic) account manager.

With the grounding rules:

- **v4 cleared 6 of the 15 `figure_grounding` violations and added none** (exact McNemar test on the paired cases,
  p = 0.03). In `tool_error_handling`, replies to an unsupported industry now name the supported ones (6 of 6, against 4
  of 6). The deterministic agent evals still pass: 30 of 30 runs on the main suite and 18 of 18 on the Entra suite, with
  `--repetitions 3`. Each version ran once, with the judge at temperature 1.0, on the test cases the rules were written
  against, so a fresh test set is the fairer check.
- **The user's word still beats the instructions.** The 9 remaining `figure_grounding` violations failed in all three
  versions, and each follows a direct user instruction that the rules forbid. Six adopt a figure the user supplied (a
  VP's contract value instead of the tool's, a share price "from an earlier chat" for a private company, a market cap
  calculated from the user's price and share count), two fill an unsupported industry with general-knowledge or
  relabeled manufacturing figures, and one converts a contract value at the user's planning rate. The one
  `tool_error_handling` violation claims a cross-exchange ticker search that no tool call made. Neither round of rules
  moved these cases; the next levers are a stronger model, or a check of each figure in a reply against the tool results
  before the user sees it ([both tried on new cases](#on-a-fresh-test-set)).
- From v3 the agent often words an access denial as a missing app role rather than "not authorized", so the Entra
  suite's denial turns accept any mention of a permission or role.
- **Chaining the turns changed nothing measurable here.** Rerun with each later turn continuing the stored response
  (run `agent-v4-chain` on the same test set; all 24 later scenario turns continued one), v4 violated
  `figure_grounding` in 8 of 36 cases, against 9 when replayed. The case that cleared is a single-turn prompt, which
  chaining can't affect, so it is run-to-run variation. These violations follow the user's word, not a lost tool
  result; chaining matters when an answer depends on an earlier tool result, as in the agent evals' `c-recall`, where
  the replayed agent made up a contract value in 2 of 4 runs and the chained one quoted the record in 4 of 4.

### On a fresh test set

v3's and v4's rules were written against the cases above, so both behaviors ran again in November 2026 on new cases:
holdout suites that keep the development suites' behavior categories (their `artifacts\systematize` folder copied in)
with a newly generated test set, none of whose cases repeat a development case
(`run_assert.py <config> --override suite=<suite>-holdout`). They ran against v2 and v4, pinned with
`FOUNDRY_AGENT_VERSION`, and against `customer-assistant-gpt-6-astra`, which has v4's instructions and toolbox on the
stronger `gpt-6-astra` ([Agent](#agent)). Each run was judged once. Cases violated:

| Behavior | Cases | Dimension | v2 | v4 | v4's instructions on `gpt-6-astra` |
| --- | ---: | --- | ---: | ---: | ---: |
| `ungrounded_figures` | 36 | `figure_grounding` | 14 | 9 | 0 |
| `ungrounded_figures` | 36 | `overrefusal` | 11 | 9 | 1 |
| `ungrounded_figures` | 36 | `policy_violation` | 14 | 10 | 1 |
| `tool_error_handling` | 18 | `graceful_error` | 6 | 2 | 0 |
| `tool_error_handling` | 18 | `policy_violation` | 7 | 4 | 0 |

The judge timed out on one `ungrounded_figures` case of the `gpt-6-astra` run, which has 35 scored cases; v2 and v4
passed that case.

- **The rules carried over, more weakly.** On the new cases v4 cleared 5 of v2's 14 `figure_grounding` violations and
  added none (p = 0.06, against 0.03 on the development cases), and 4 of v2's 6 `graceful_error` violations
  (p = 0.125). Its 9 `figure_grounding` violations again follow the user: it keeps a figure the user supplied (a VP's
  draft contract value, Finance's share price for a private company), estimates a share price the quote tool doesn't
  have, or gives aerospace a growth rate from general knowledge or from manufacturing's data. All its tool-error
  violations are in multi-turn scenarios: internal names leaked in follow-ups, a claim that the lookup covered inactive
  accounts, and a manufacturing proxy given for an unsupported industry without saying so. The new cases were harder
  for v2 too: 6 `graceful_error` violations of 18, against 2 on the development cases.
- **The stronger model removed nearly all of them.** With the same instructions, `gpt-6-astra` violated
  `figure_grounding` in none of its 35 scored cases, against 9 for v4 (p = 0.004), and had no tool-error violation
  (v4: 4 cases with `policy_violation`, p = 0.125). It rejected each figure the user pressed on it, by name: "not the
  unverified $1.8 million in your notes". Its one violating case went the other way: it declined a clearly labeled
  hypothetical worked example (`overrefusal` and `policy_violation`). It used 17 to 19% more tokens than v4, and like
  v4 it passed the deterministic agent evals, figure check included, in 36 of 36 runs with `--repetitions 3`.
- **The figure check** (`figures_from_tools`, [architecture](../docs/architecture.md#the-figure-check)), its rules
  frozen from the development runs, flagged 11 of v2's 14 and 8 of v4's 9 `figure_grounding` violations. It also
  flagged 18 to 26% of the transcripts the judge passed, most often for a user's figure repeated to reject it, which
  the stronger model does often. So it suits turns that should only relay tool data, as in the agent evals; telling
  adopting a user's figure from rejecting it still takes the judge.

## Foundry evaluator findings

Judged by `gpt-4-1-mini` in November 2026 on the sample suite against v4 (`python -m evals.run_cloud_eval`): first
with Foundry's built-in `task_adherence`, `intent_resolution` and `tool_call_accuracy`, the defaults then, and then
with this repo's `tool_use` and `grounded_adherence` and the built-in `intent_resolution`, the defaults now. The
deterministic checks passed every turn below:

| Evaluators | Run | Turns | Judge passed | Judge tokens | Time |
| --- | --- | ---: | ---: | ---: | ---: |
| Built-in | A chained agent run's stored responses | 12 | 9 | 92,699 | 161 s |
| Built-in | `--target`: Foundry calls the agent with each first turn | 10 | 8 | 68,422 | 129 s |
| This repo's | The same chained run's stored responses | 12 | 12 | 81,022 | 129 s |
| This repo's | `--target` | 10 | 10 | 65,817 | 129 s |

- **Every disagreement with the built-in evaluators was a correct turn the judge failed.** The three evaluators cost
  6,800 to 7,700 judge tokens a turn.
- **`tool_call_accuracy` fails a correct call that returns an error.** Asked for a privately held customer's stock price,
  or for an unknown customer, the agent made the right call and relayed the error. The evaluator called the call
  relevant and correct, then scored it 2 of 5 because "the tool returned an error and no successful retry was made".
- **`task_adherence` can't tell a recalled figure from a made-up one.** In `c-recall` turn 2 the chained agent quoted the
  contract value its first lookup returned; the evaluator sees earlier turns as text, without their tool results, and
  failed the reply for stating a figure no tool call had provided. A replayed run's made-up value on the same turn
  failed for the same reason.
- **This repo's evaluators pass those turns and still catch the made-up value.** They get the earlier turns with their
  tool calls and results. On a replayed `c-recall` run of 6 repetitions, `grounded_adherence` failed the 3 replies
  that made up a contract value (score 2: "not grounded in any tool result from this conversation") and passed the 3
  that said it wasn't available, which the checks fail for lacking the value; `tool_use` passed all 6. With
  `intent_resolution`, they cost 6,400 to 6,800 judge tokens a turn.
- **The judge prompts needed calibrating.** On 11 synthetic turns with an expected verdict each
  ([architecture](../docs/architecture.md#8-foundry-evaluators)), early versions let a wrong reply lower `tool_use`'s
  score for correct calls, and, with the score before the reason in the judge's answer, the judge sometimes gave a
  score its own reason contradicted: 2, with the reason "the score should be 5". The prompts in the repo give every
  expected verdict: 66 of 66 over 3 runs of `python -m evals.calibrate_evaluators --repetitions 3`.
- **Text history needed `output_text`.** A replayed run's later turns couldn't be judged at all: every evaluator
  rejected "content type 'input_text' for message with role 'assistant'", how Foundry stores a plain-string reply.
  The agent target (`evals\foundry_agent\target.py`) now sends earlier replies as `output_text`, so a replayed run's
  later turns are judged like chained ones.
- `intent_resolution` passed every turn.

## Agent

Prompt agent `customer-assistant` (model `gpt-4-1-mini`) with the toolbox endpoint as an MCP tool (`require_approval: never`).
Its instructions (`INSTRUCTIONS` in `scripts\deploy_agent.py`) allow only figures a tool returned, treat figures the user
gives as unverified, and say how to report a tool error; each rule answers an [ASSERT finding](#assert-findings).
Create a new version with `.\.venv\Scripts\python scripts\deploy_agent.py [--model <deployment>] [--dry-run]`.
Invoke: `POST {project}/openai/v1/responses` with `agent_reference`. The reference names a version only when
`FOUNDRY_AGENT_VERSION` (or the agent runner's `--agent-version`) sets one; otherwise every run gets the latest. A
cloud-eval `--target` run gives Foundry's agent target the same version.

For the model comparison in the [ASSERT findings](#assert-findings), `customer-assistant-gpt-6-astra` has v4's
instructions and toolbox on the stronger `gpt-6-astra`:
`.\.venv\Scripts\python scripts\deploy_agent.py --agent customer-assistant-gpt-6-astra --model gpt-6-astra`. Point a run
at it with `--agent` or `FOUNDRY_AGENT_NAME`.

## Redeploy

Recreate the connection and toolbox (new toolbox version, promoted to default; `--dry-run` prints the requests):

```powershell
.\.venv\Scripts\python scripts\deploy_toolbox.py --mcp-url https://<app-fqdn>/mcp --resource-group <foundry-rg>
```

Rebuild the MCP server (mcp 2.x `MCPServer`) under a new tag; keep the previous tag for rollback:

```powershell
az acr build -r <acr-name> -t sample-mcp:<new-tag> sample-mcp-server
az containerapp update -n sample-mcp -g <sample-rg> --image <acr-name>.azurecr.io/sample-mcp:<new-tag>
```

The Entra stack (the toolbox rerun rotates the client secret; users keep their consent):

```powershell
az containerapp update -n sample-mcp-entra -g <sample-rg> --image <acr-name>.azurecr.io/sample-mcp:<new-tag>
.\.venv\Scripts\python scripts\deploy_toolbox.py --mcp-url https://<entra-app-fqdn>/mcp --resource-group <foundry-rg> `
  --auth-type OAuth2 --connection sample-mcp-oauth-conn --toolbox sample-customer-toolbox-oauth
.\.venv\Scripts\python scripts\deploy_agent.py --agent customer-assistant-oauth --toolbox sample-customer-toolbox-oauth
```

If `az acr build` crashes printing logs (a `UnicodeEncodeError` from the Windows console codepage, even with
`$env:PYTHONIOENCODING="utf-8"`), the build still runs to completion in the registry. Before updating the apps, check that
`az acr task list-runs -r <acr-name> --top 1 -o table` shows it `Succeeded`.
