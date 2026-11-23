"""Slides 1-5: cover, why two levels, what it does, architecture, sample stack."""
from deck_lib import *  # noqa: F401,F403


def s01_cover(prs, page):
    s = new_slide(prs, "Foundry Toolbox Eval tests a Microsoft Foundry toolbox at two levels: the tools on their own, "
                       "through direct MCP calls, and an agent that uses them. Two LLM judges, ASSERT and Foundry's "
                       "evaluators, cover what deterministic checks can't, Entra ID carries the user's identity to the "
                       "MCP server, and every run can be followed as one trace in Application Insights.")
    topbar(s)
    text(s, 39.6, 96, 864, 15.8, "PROJECT OVERVIEW", font="Aptos", size=10, c=BLUE, bold=True, markup=False)
    text(s, 39.6, 118, 880, 62, "Foundry Toolbox Eval", font="Aptos Display", size=46, c=INK, bold=True, markup=False)
    box(s, 39.6, 190, 37.4, 5, fill=BLUE)
    text(s, 39.6, 204, 880, 30, "How well does a Foundry toolbox work, on its own and inside an agent?", font="Aptos",
         size=20, c=BODY, markup=False)
    text(s, 39.6, 238, 880, 20, "Tool tests  ·  agent evals  ·  LLM judges  ·  per-user identity  ·  one trace end to end",
         font="Aptos", size=13, c=MUTED, markup=False)
    tiles = [("Tool tests", "Direct MCP calls", "Is each tool right, fast and well formed?", BLUE),
             ("Agent evals", "Deterministic checks", "Right tools, right arguments, right reply?", TEAL),
             ("LLM judges", "ASSERT and Foundry", "Does each reply stay grounded, even under pressure?", PURPLE),
             ("Telemetry", "Application Insights", "One trace from eval turn to tool call.", ORANGE)]
    for i, t in enumerate(tiles):
        tile(s, 39.6 + i * 224.4, 290, 205.2, 104, *t)
    pill(s, 39.6, 420, 206, 24.5, "github.com/bradrlaw/foundryToolboxEval")
    footer(s, page)


def s02_why(prs, page):
    s = new_slide(prs, "An agent's answer passes through three layers. If we only test the agent, a wrong answer could "
                       "come from the model's choices, from the toolbox, or from the tool behind it. Testing the tools "
                       "directly gives a baseline: when tool tests pass and agent evals fail, the problem is the agent.")
    header(s, 1, "Why two levels", "When an agent gets it wrong, which layer failed?",
           "An agent's answer depends on three layers, and a wrong answer can start in any of them. Testing the tools "
           "on their own and the agent end to end shows which one to fix.")
    cards3(s, [
        ("The agent", ["Chooses which tools to call and with which arguments, then writes the reply. It can skip a "
                       "tool, guess an argument or state a figure no tool returned.",
                       "**Tested by:** agent evals, ASSERT and Foundry's evaluators."], PURPLE),
        ("The toolbox", ["Foundry's managed MCP endpoint in front of the tools. It names them, handles sign-in and "
                         "forwards each call. It can time out or turn a tool error into a failed request.",
                         "**Tested by:** tool tests, and agent evals through the agent."], BLUE),
        ("The tools", ["MCP servers behind the toolbox that return the data the agent relies on. They can be wrong, "
                       "slow, or return a result that breaks their own schema.",
                       "**Tested by:** tool tests, through the toolbox."], TEAL)])
    panel(s, 39.6, 347.8, 879.8, 139)
    stack(s, 56.2, 365, 115.2, [("AGENT", PURPLE), ("TOOLBOX", BLUE), ("MCP SERVER", TEAL)])
    ptitle(s, 190.8, 358, 710, "Two levels, one harness")
    text(s, 190.8, 388, 710, 46, ["**Tool tests:** runner → toolbox → MCP server, with no model in the loop.",
                                  "**Agent evals:** runner → prompt agent → toolbox → MCP server."],
         size=14.4, c=DARK, anchor="t", after=4)
    text(s, 190.8, 442, 710, 32, "If an agent eval fails while the matching tool tests pass, look at the agent. If both "
         "fail, start with the toolbox or the tool.", size=11.5, c=MUTED, anchor="t")
    footer(s, page)


def s03_what(prs, page):
    s = new_slide(prs, "Four runners share one set of conventions. Tool tests and agent evals are deterministic: JSONL "
                       "cases in, JSON results out. Two LLM judges cover what patterns can't check: ASSERT generates its "
                       "own test conversations from a written behavior and judges the replies, and run_cloud_eval has "
                       "Foundry's evaluators judge the replies an agent-eval run got. report.py turns any result (tool, "
                       "agent, cloud or assert) into Markdown, optionally against a baseline run. Exit codes let CI tell "
                       "failing cases from a broken setup.")
    header(s, 2, "What it does", "Three ways to evaluate, one set of conventions",
           "Each runner calls the live Foundry project and writes a JSON result; one report script turns any result "
           "into Markdown.")
    cards3(s, [
        ("Tool tests", ["Call each tool through the toolbox's MCP endpoint and check the result: fields, values, "
                        "errors, schemas and latency. No model is involved, so results repeat.",
                        "**Sample suite:** 14 cases over the three tools."], BLUE),
        ("Agent evals", ["Send each turn to a prompt agent and check which tools it called, with which arguments, "
                         "whether its reply matches a pattern and whether its figures came from the tools.",
                         "**Sample suite:** 12 turns over the same tools."], TEAL),
        ("LLM judges", ["For what patterns can't check: ASSERT judges conversations it generates from a written "
                        "behavior, and Foundry's evaluators judge the replies of an agent-eval run.",
                        "**Samples:** 2 behaviors with 54 cases; 3\u00a0evaluators."], PURPLE)])
    panel(s, 39.6, 347.8, 879.8, 139)
    stack(s, 56.2, 365, 115.2, [("INPUTS", BLUE), ("RUNNERS", TEAL), ("REPORTS", PURPLE)])
    ptitle(s, 190.8, 358, 710, "One flow for every runner")
    text(s, 190.8, 387, 710, 58, [
        "**Inputs:** JSONL cases validated against JSON Schemas, ASSERT's YAML behaviors, or an agent-eval result.",
        "**Results:** JSON under `results\\`; tool and agent runs also record the commit and toolbox or agent version.",
        "**Reports:** `report.py` writes Markdown, with a reason for every failure and a baseline diff."],
        size=13.2, c=DARK, anchor="t", after=2)
    highlight(s, 190.8, 452, 710, "Tool, agent and evaluator runners exit 0 all passed  ·  1 some failed  ·  2 setup error")
    footer(s, page)


def s04_arch(prs, page):
    s = new_slide(prs, "Left: the four runners, run from a laptop with az login or from GitHub Actions through OIDC. "
                       "Tool tests call the toolbox's MCP endpoint directly. Agent evals and ASSERT call the prompt agent "
                       "through the Responses API; the agent uses the toolbox as its MCP tool, and Foundry stores each "
                       "reply as a response that later turns continue. run_cloud_eval creates an evaluation through the "
                       "Evals API: Foundry reads an agent-eval run's stored responses by ID and has its evaluators judge "
                       "them with gpt-4-1-mini, or with --target calls the agent itself. The toolbox reaches the MCP "
                       "server through a connection, either unauthenticated or OAuth2 identity passthrough with Entra "
                       "ID. Runners and Foundry send spans to Application Insights.")
    header(s, 3, "Architecture", "The harness and what it calls")
    subtitle(s, "Runners on a laptop or in CI  ·  one Foundry project  ·  services behind the toolbox")
    colhead(s, 51.8, 151.1, 116.6, "Runners")
    colhead(s, 191.5, 151.1, 411.8, "Microsoft Foundry project")
    colhead(s, 630.7, 151.1, 276.5, "Connected services")
    box(s, 191.5, 170, 411.8, 318, fill=PANEL, line=LINE, lw=1.2, shape=ROUND, adj=0.1)
    for cy, col in ((212, BLUE), (290, TEAL), (446, PURPLE)):
        conn(s, 168.4, cy, 207, cy, col)
        conn(s, 311, cy, 334, cy, col)
    conn(s, 168.4, 352, 259, 302.25, PURPLE)
    conn(s, 168.4, 368, 207, 368, ORANGE)
    conn(s, 418, 258, 418, 244, BLUE)
    conn(s, 418, 322, 418, 342, TEAL)
    conn(s, 418, 394, 418, 420, PURPLE)
    conn(s, 502, 212, 520, 212, TEAL)
    conn(s, 590, 212, 630.7, 212, TEAL, 2)
    conn(s, 565, 240, 630.7, 290, PURPLE)
    conn(s, 502, 290, 520, 290, ORANGE)
    conn(s, 502, 316, 630.7, 368, ORANGE)
    conn(s, 502, 446, 520, 446, ORANGE)
    runners = [("Tool tests", "tooltests.run", "MCP client", BLUE),
               ("Agent evals", "run_agent_evals", "Responses API client", TEAL),
               ("ASSERT", "run_assert.py", "LLM-judged behaviors", PURPLE),
               ("Foundry evaluators", "run_cloud_eval", "Evals API client", PURPLE)]
    for cy, (lab, name, cap, k) in zip((212, 290, 368, 446), runners):
        node(s, 51.8, cy - 29, 116.6, 58, lab, name, cap, kind=k, lw=1.8 if k != BLUE else 1.2)
    pill(s, 207, 199.75, 104, 24.5, "MCP ENDPOINT")
    pill(s, 207, 277.75, 104, 24.5, "RESPONSES API")
    pill(s, 207, 433.75, 104, 24.5, "EVALS API")
    node(s, 334, 180, 168, 64, "Toolbox", "sample-customer-toolbox", "tools named sample___<tool>", kind=BLUE, lw=1.8)
    node(s, 334, 258, 168, 64, "Prompt agent", "customer-assistant", "the toolbox is its MCP tool", kind=TEAL, lw=1.8)
    node(s, 520, 184, 70, 56, "Connection", caption="none or OAuth2", kind=TEAL, fill=WHITE)
    node(s, 520, 264, 70, 52, "Model", caption="gpt-4-1-mini", kind=ORANGE)
    node(s, 207, 342, 104, 52, "Judge model", caption="gpt-5.6-sol", kind=ORANGE)
    node(s, 334, 342, 168, 52, "Stored responses", "one per agent turn", "read by ID to chain and judge", kind=TEAL)
    node(s, 334, 420, 168, 52, "Evaluation", "Foundry evaluators", "two of this repo's, one built-in", kind=PURPLE)
    node(s, 520, 420, 70, 52, "Judge", caption="gpt-4-1-mini", kind=ORANGE)
    service(s, 630.7, 181, 276.5, 62, "MCP server", "Container Apps", "sample-mcp",
            "customers · markets · quotes\nsynthetic data, typed outputs", TEAL)
    service(s, 630.7, 259, 276.5, 62, "Identity", "OAuth2 passthrough", "Entra ID",
            "per-user tokens\napp roles filter data", PURPLE)
    service(s, 630.7, 337, 276.5, 62, "Telemetry", "OpenTelemetry", "App Insights",
            "runner and Foundry spans\none trace per eval turn", ORANGE, lw=1.2)
    node(s, 630.7, 417, 276.5, 58, "Access", caption="Foundry User on the project, and on the resource\nfor the "
         "evaluators' judge  ·  az login or CI via OIDC", kind=None, fill=WHITE)
    footer(s, page)


def s05_stack(prs, page):
    s = new_slide(prs, "The scripts under scripts\\ deploy everything the harness needs, in this order. All data is "
                       "synthetic. Each deploy_agent.py run publishes a new agent version; runners call the latest unless "
                       "FOUNDRY_AGENT_VERSION pins one, so an older version can be rerun on the same cases. The Entra "
                       "twin runs the same server image with AUTH_MODE=entra behind an OAuth2 toolbox, so the identity "
                       "cases have something to prove.")
    header(s, 4, "Sample stack", "A sample stack to test against",
           "The repo deploys a small, synthetic stack so the harness has something real to test: an MCP server, a "
           "toolbox in front of it and an agent that uses it.")
    cards3(s, [
        ("MCP server", ["Python on Azure Container Apps with one warm replica. Three tools, `get_customer`, "
                        "`get_market_conditions` and `get_stock_quote`, over 5 synthetic customers, each with an "
                        "output schema.",
                        "**Industries:** manufacturing, healthcare, energy, retail."], TEAL),
        ("Toolbox", ["`deploy_toolbox.py` creates the toolbox and its connection to the server, with no auth or "
                     "OAuth2 passthrough. Foundry exposes the tools as `sample___<tool>`.",
                     "**Callers** need an Entra token for https://ai.azure.com."], BLUE),
        ("Prompt agent", ["`deploy_agent.py` publishes a new version of `customer-assistant` on gpt-4-1-mini, with "
                          "the toolbox as its MCP tool, tool approval set to never and grounding rules in its "
                          "instructions.",
                          "**Used by** all agent runners, at its latest version or a pinned one."], PURPLE)])
    panel(s, 39.6, 347.8, 879.8, 139)
    stack(s, 56.2, 365, 115.2, [("SIGNED-IN USER", PURPLE), ("OAUTH2 TOOLBOX", BLUE), ("ENTRA MCP SERVER", TEAL)])
    ptitle(s, 190.8, 358, 710, "An Entra-protected twin")
    text(s, 190.8, 387, 710, 56, "The same image runs with `AUTH_MODE=entra` behind its own toolbox. App roles decide "
         "which customer regions each caller may read, and a `whoami` tool shows who the server thinks is calling. "
         "`setup_entra.py` creates the app registrations.", size=13.2, c=DARK, anchor="t")
    highlight(s, 190.8, 452, 710, "Synthetic data only: no real customers, markets or prices.")
    footer(s, page)


SLIDES = [s01_cover, s02_why, s03_what, s04_arch, s05_stack]
