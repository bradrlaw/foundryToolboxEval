# Sample toolbox deployment

Real values for this deployment are kept in the git-ignored `deploy/resources.local.md`.

| Item | Value |
| --- | --- |
| Foundry resource / project | `<foundry-resource>` (rg `<foundry-rg>`) / `<project>` |
| MCP host | Container App `sample-mcp` in `<sample-rg>` (`<region>`), ACR `<acr-name>` |
| Project connection | `sample-mcp-conn` (category `RemoteTool`, auth `None`) |
| Toolbox | `sample-customer-toolbox` (v1, default) |
| Consumer endpoint | `{project_endpoint}/toolboxes/sample-customer-toolbox/mcp?api-version=v1` |

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

- **A tool error aborts the whole agent request.** When an MCP tool returns `isError: true` while an agent is calling it through the toolbox,
  the Responses API returns HTTP 400 (`An error occurred invoking 'sample___...'`) instead of giving the error to the model.
  The agent cannot say "that customer was not found"; callers must handle the 400. Direct toolbox calls are unaffected (tool tests see `isError`).
  The ASSERT target reports this as an `[AGENT FAILURE ...]` reply so it is scored.
- Responses API calls can hit transient DNS errors; the target retries connection errors, 408, 429 and 5xx (honoring `Retry-After`).

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
  `tools/list` took about 0.3 s on both. Both apps kept one warm replica.

## Agent

Prompt agent `customer-assistant` (model `gpt-4-1-mini`) with the toolbox endpoint as an MCP tool (`require_approval: never`).
Create a new version with `.\.venv\Scripts\python scripts\deploy_agent.py [--model <deployment>] [--dry-run]`.
Invoke: `POST {project}/openai/v1/responses` with `agent_reference`.

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

If `az acr build` crashes printing logs (Windows codepage), set `$env:PYTHONIOENCODING="utf-8"`; the build still completes.
