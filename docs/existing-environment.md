# Using your own Foundry project, toolbox and agents

No sample deployment is needed. Point the framework at what you already have and author tests for your tools.

## 1. What you need

- Foundry project endpoint, and the toolbox name (consumer endpoint: `<project>/toolboxes/<toolbox>/mcp?api-version=v1`).
- The agent name (a Foundry agent whose tools include that toolbox) for evals.
- Signed-in identity (`az login`) with **Azure AI User** on the project (call toolbox and agent). For ASSERT, also
  **Cognitive Services OpenAI User** on the resource hosting the judge model.
- Python 3.11+ and the install steps from [getting-started.md](getting-started.md#2-install).

## 2. Configure

`.env`:

```
FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
FOUNDRY_AGENT_NAME=<your-agent>
FOUNDRY_TOOLBOX_NAME=<your-toolbox>
AZURE_API_BASE=https://<resource>.openai.azure.com
ASSERT_AZURE_USE_AAD=1
```

The runners build the toolbox endpoint from `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_TOOLBOX_NAME`, so no config file is
needed. For a toolbox outside that project, pass `--endpoint <url>`, or copy `config\target.example.json` to
`config\<name>.local.json` (toolbox `mcp_endpoint` and `token_scope`, default `https://ai.azure.com/.default`) and pass
`--config`.

The agent is invoked by name through the Responses API (`agent_reference`); `evals\foundry_agent\target.py` reads
`FOUNDRY_AGENT_NAME`. Tool names in a toolbox are `{server_label}___{tool}`; list them with a `list`-kind tool test.

## 3. Author tests for your tools

Copy `data\tool-tests\sample-toolbox.jsonl` and edit; each line is one case validated by `schemas\tool-test.schema.json`.
Kinds: `call`, `list`, `auth`. Assertions: contains, regex, json_path_equals, json_path_exists, schema; plus latency limits
and `repetitions`. Cover happy path, not-found/invalid input (`expect_error: true` for an `isError` result,
`"protocol"` for a JSON-RPC error), schema conformance, auth failures.

Agent evals (`data\agent-evals\*.jsonl`, `schemas\agent-eval.schema.json`): each turn gives the prompt, expected tool calls
(name and argument checks), `must_not_call` (always enforced) and answer assertions; turns with the same
`conversation_id` run as one conversation.

```powershell
.\.venv\Scripts\python scripts\validate_eval_jsonl.py data\tool-tests\mine.jsonl   # kind comes from the folder or first record; --kind overrides
.\.venv\Scripts\python -m tooltests.run data\tool-tests\mine.jsonl
.\.venv\Scripts\python -m evals.run_agent_evals data\agent-evals\mine.jsonl
```

## 4. ASSERT behaviors for your agent

Copy `evals\configs\ungrounded_figures.yaml` and change:

- `suite` / `run` names.
- `behavior.description`: one atomic behavior, with a clear violation definition and a permissible boundary.
- `context`: your agent, its tools and what they return. The judge relies on this, so be specific.
- Judge models (`azure/<deployment>`), which must exist on `AZURE_API_BASE`'s resource.
- Keep the callable target (`evals.foundry_agent.target`) unless your agent is invoked differently.

Run: `.\.venv\Scripts\python scripts\run_assert.py evals\configs\<yours>.yaml`. The wrapper puts artifacts in
`results\assert`, so leave `artifacts_root` out of the config.

## 5. Telemetry

See [telemetry.md](telemetry.md); most projects already have an App Insights connection.

## 6. Toolboxes that pass the user's identity

If a toolbox connection uses OAuth2 identity passthrough, the MCP server sees the user who runs the tests. See
[entra.md](entra.md) for how this works; in short:

- The runners call as your `az login` user. Write cases for that user's permissions, and check identity with
  `{{caller.oid}}`, `{{caller.tid}}`, `{{caller.name}}` and `{{caller.username}}` instead of real names, so the files hold
  no personal data.
- Each user consents once per connection. Until then, the tool runner exits 2 and agent turns fail with
  `[AGENT NOT RUN: ...]`; both print the consent link on the console. Open it signed in as the test user, approve and rerun.
- For each authorization rule, test an allowed and a denied request, and check that denials leak no data.
- `upstream_error` (tool tests) and `tool_listing_failed` (agent evals) mean the toolbox couldn't authenticate to the server.
- Users need a project role that can invoke agents (Learn's minimum is **Foundry Agent Consumer**).
- `UserEntraToken` connections to custom MCP servers failed in our tests; OAuth2 works.

## Caveats

- A tool returning `isError` during an agent run causes HTTP 400 for the whole request; tool-error handling can only be
  tested at the toolbox level, and agent evals report it as a platform abort.
- One identity per run: to test another user, sign in as them (and consent once) before running their cases.
