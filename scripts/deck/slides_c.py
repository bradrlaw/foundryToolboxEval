"""Slides 12-16: telemetry, CI, findings, get started, next steps."""
from deck_lib import *  # noqa: F401,F403

LANES = [("Agent eval", TEAL, [("eval.turn", "foundry-agent-evals"), ("invoke_agent", "responsesapi"),
                               ("chat · execute_tool", "responsesapi"), ("tools/call", "agentsv2")]),
         ("Tool test", BLUE, [("toolbox.test_case", "foundry-toolbox-tests"), ("tools/call", "MCP client span"), None,
                              ("tools/call", "agentsv2")])]


def s11_telemetry(prs, page):
    s = new_slide(prs, "The tool-test, agent-eval and ASSERT runners send OpenTelemetry spans to Application Insights and "
                       "send the W3C trace header with every request, so Foundry's spans (cloud roles responsesapi and "
                       "agentsv2) join the runner's trace. One query shows where the time went for a turn or a tool "
                       "call. docs\\telemetry.md has the KQL. Foundry's evaluators run inside Foundry and add no spans; "
                       "their verdicts are in the result file and the Foundry portal.")
    header(s, 11, "Telemetry", "One trace from eval turn to toolbox call",
           "Runners send OpenTelemetry spans to Application Insights and pass the trace context to Foundry, so "
           "Foundry's own spans join the same trace.")
    panel(s, 39.6, 156, 878.4, 160, fill=CARD, adj=0.043)
    for li, (lab, col, spans) in enumerate(LANES):
        cy = 199 + li * 74
        text(s, 56, cy - 20, 118, 40, [(lab.upper(), {"font": "Aptos", "size": 9, "c": TEXT[col], "bold": True}),
                                       ("cloud role under each span", {"font": "Aptos", "size": 8, "c": MUTED})],
             markup=False)
        xs = [186 + i * 184 for i in range(4)]
        live = [i for i, sp in enumerate(spans) if sp]
        for a, b in zip(live, live[1:]):
            conn(s, xs[a] + 164, cy, xs[b], cy, col)
            if b - a > 1:
                text(s, xs[a + 1], cy - 17, 164, 13, "direct MCP call, no agent", font="Aptos", size=8.5, c=MUTED,
                     align="c", markup=False)
        for i in live:
            name, role = spans[i]
            node(s, xs[i], cy - 27, 164, 54, name=name, caption=role, kind=col, lw=1.2, fill=WHITE, sizes=(8, 11, 9))
    row(s, [("Where the time goes", ["A tool call through the open toolbox takes about 0.3 s round trip, 90–115 ms of "
                                     "it inside Foundry. Through OAuth2 passthrough it takes 1.1–1.5 s, with 490–980 ms "
                                     "in the toolbox.",
                                     "**Budget for it:** set each case's `max_latency_ms` with the passthrough "
                                     "overhead in mind."], ORANGE),
            ("Find it later", ["Spans carry the case, tool and outcome as attributes, and `docs\\telemetry.md` has the "
                               "KQL to pull a run.",
                               "**Agent spans** also record the agent version, token use, and each tool call's "
                               "arguments and output.",
                               "**CI runs** set the role instance to `github-actions-<run id>`."], BLUE)],
        y=330, h=158, gap=20, adj=0.049)
    footer(s, page)


def s12_ci(prs, page):
    s = new_slide(prs, "ci.yml needs no Azure access: the unit tests are offline by design. live.yml signs in with a "
                       "user-assigned managed identity through GitHub's OIDC token, so there is no client secret to "
                       "rotate or leak. The identity has Foundry User on the Foundry resource, because the evaluators' "
                       "judge calls need it there; everything else needs it on the project only. The live environment "
                       "can only be used from main. The workflow runs the tool tests, the agent evals and Foundry's "
                       "evaluators on that agent-eval run's stored replies. The evaluators' step runs with --fail-on "
                       "unjudged: the judge's failures are reported but don't fail the job, because an LLM judge can "
                       "fail correct turns. Result files stay on the runner; only the Markdown reports are published to "
                       "the job summary.")
    header(s, 12, "Continuous integration", "Offline checks on every push, live runs every day",
           "Unit tests run offline on pushes and pull requests. A scheduled workflow signs in to Azure without a secret "
           "and runs the suites and Foundry's evaluators against the live project.")
    row(s, [("ci.yml  ·  offline", ["Unit tests on Ubuntu with Python 3.11 and 3.14 and on Windows with 3.14. No "
                                    "Azure and no `.env`: the tests never touch the network. Dependabot keeps actions "
                                    "and packages current.",
                                    "**Also:** `pip check`, a compile pass and JSONL validation against the schemas."],
             BLUE),
            ("live.yml  ·  live project", ["Daily at 12:00 UTC or on demand. Signs in as a managed identity through "
                                           "OIDC, in a protected `live` environment only `main` can use, and runs the "
                                           "tool tests, the agent evals and Foundry's evaluators.",
                                           "**Fails the job** if a case fails or a turn goes unjudged, once every step "
                                           "has posted its report."], TEAL)],
        y=159.8, h=152, gap=23.4, adj=0.049)
    panel(s, 39.6, 322, 878.4, 165)
    stack(s, 56.2, 339, 115.2, [("GITHUB ACTIONS", BLUE), ("ENTRA · OIDC", PURPLE), ("FOUNDRY PROJECT", TEAL)])
    ptitle(s, 190.8, 334, 710, "Least privilege, nothing left behind")
    text(s, 190.8, 364, 710, 74, [
        "One role, Foundry User, on the Foundry resource for the evaluators' judge, and no client secret.",
        "Reports go to the job summary; result files never leave the runner.",
        "Tokens are fetched right after sign-in, because GitHub's ID token expires within minutes."],
        size=13.2, c=DARK, anchor="t", after=3, bullet=True)
    highlight(s, 190.8, 448, 710, "Latest live run: 14/14 tool tests, 12/12 agent turns, and the judge passed 12/12, "
              "in 4 min 20 s.")
    footer(s, page)


def s13_findings(prs, page):
    s = new_slide(prs, "These come from running the harness against the sample stack; deploy\\README.md has the detail. "
                       "The agent finding is the most actionable: pressed for figures no tool returned, the agent "
                       "filled gaps with its own. On a fresh holdout set of 36 cases, v2 did so in 14 and v4, with "
                       "grounding rules in its instructions, in 9 (p = 0.06). The same instructions on gpt-6-astra had "
                       "none in 35 scored cases (p = 0.004 against v4) and no tool-error violation, used 17 to 19% more "
                       "tokens and passed all 36 agent-eval turns over 3 runs. Foundry's built-in evaluators failed 3 "
                       "correct turns of 12, so this repo uses its own, calibrated on turns with known verdicts. The "
                       "figure check catches most invented figures without a judge, but can't tell a user's figure "
                       "repeated to reject it from one adopted, so it suits turns that should only relay tool data.")
    header(s, 13, "Findings", "What the evaluations found",
           "Running the harness against the sample stack turned up behavior worth knowing before building on a toolbox.")
    cards3(s, [
        ("Toolbox", ["Tools appear as `<server>___<tool>` and typed outputs pass through. A tool error now reaches the "
                     "model as text; through October 2026 it ended the whole request with HTTP 400.",
                     "**Takeaway:** test tool errors through an agent, not just directly."], BLUE),
        ("Hosting", ["Starting from zero replicas took the MCP server about 20 s; the toolbox gave up after about "
                     "10 s, while direct calls waited and passed.",
                     "**Takeaway:** keep at least one replica warm."], TEAL),
        ("Agent", ["Pressed for figures no tool returned, v2 supplied them in 14 of 36 fresh cases. Grounding rules "
                   "(v4) cut that to 9; the same rules on gpt-6-astra, to 0 of 35.",
                   "**Takeaway:** rules helped; the stronger model closed the gap."], PURPLE)],
        chevrons=False)
    panel(s, 39.6, 347.8, 879.8, 139)
    stack(s, 56.2, 365, 115.2, [("IDENTITY", PURPLE), ("LATENCY", ORANGE), ("JUDGES", BLUE)])
    ptitle(s, 190.8, 358, 710, "Identity, latency and judges")
    text(s, 190.8, 387, 710, 90, [
        "**Identity and latency:** OAuth2 passthrough works, but a call takes 1.1–1.5 s against 0.3 s without auth, "
        "most of it in the toolbox. `UserEntraToken` fails for a custom audience.",
        "**Judges:** Foundry's built-in evaluators failed 3 correct turns of 12, so this repo brings its own. The "
        "figure check caught 19 of 23 ASSERT violations but also flagged 18–26% of the replies the judge passed."],
        size=13.2, c=DARK, anchor="t", after=6)
    footer(s, page)


def s14_start(prs, page):
    s = new_slide(prs, "To test your own toolbox, no code changes are needed: set the project endpoint, toolbox and "
                       "agent names in .env, write JSONL cases, and run. docs\\existing-environment.md walks through it. "
                       "The signed-in user needs Foundry User (formerly Azure AI User) on the project, or on the Foundry "
                       "resource to run Foundry's evaluators, plus Cognitive Services OpenAI User on the judge model's "
                       "resource for ASSERT. After changing a judge prompt or the judge model, run "
                       "python -m evals.calibrate_evaluators --repetitions 3 before trusting the verdicts.")
    header(s, 14, "Get started", "Point it at your own toolbox",
           "A `.env` file and JSONL cases drive everything, so you can test your own toolbox and agent without changing "
           "code.")
    row(s, [("Install", "Python 3.11+ and a venv, then install `requirements.txt`.", BLUE),
            ("Configure", "Copy `.env.example` to `.env` and set the project, toolbox and agent.", TEAL),
            ("Write cases", "Add JSONL under `data\\` and check it with `validate_eval_jsonl.py`.", PURPLE),
            ("Run and compare", "Run the suites, then compare each report with a baseline.", ORANGE)],
        y=159.8, h=118, gap=40, chev=(24, 18.7))
    panel(s, 39.6, 292, 878.4, 196, fill=CARD, adj=0.04)
    code(s, 52, 304, 853.6, 146, "PowerShell", [
        r"python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt",
        "# .env: FOUNDRY_PROJECT_ENDPOINT, plus FOUNDRY_TOOLBOX_NAME and FOUNDRY_AGENT_NAME to test your own",
        r".\.venv\Scripts\python scripts\validate_eval_jsonl.py data\tool-tests\mine.jsonl data\agent-evals\mine.jsonl",
        r".\.venv\Scripts\python -m tooltests.run data\tool-tests\mine.jsonl",
        r".\.venv\Scripts\python -m evals.run_agent_evals data\agent-evals\mine.jsonl --repetitions 3",
        r".\.venv\Scripts\python -m evals.run_cloud_eval",
        r".\.venv\Scripts\python scripts\run_assert.py evals\configs\<behavior>.yaml",
        r".\.venv\Scripts\python scripts\report.py agent --baseline results\agent-evals\<older run>.json"],
        size=10.5)
    text(s, 52, 458, 853.6, 20, "Roles: Foundry User on the project, or on the Foundry resource for Foundry's "
         "evaluators; Cognitive Services OpenAI User for ASSERT's judge.", size=11.5, c=MUTED, markup=False)
    footer(s, page)


def s15_next(prs, page):
    s = new_slide(prs, "Only identity items remain open in docs\\plan.md: a role matrix with several test identities in "
                       "one run, the Entra suites in live CI, which needs a test user who has consented, and hosted "
                       "agents, which forward x-agent-foundry-call-id to the toolbox. Two checks remain too: whether "
                       "UserEntraToken still fails for a custom audience, and whether consent survives the connection's "
                       "refresh-token expiry. Since the first version of this deck, conversations chain stored "
                       "responses, Foundry's evaluators run with this repo's calibrated prompts in live CI, the agent "
                       "evals check figures, and a fresh holdout set showed that grounding rules help and a stronger "
                       "model closes the gap.")
    header(s, 15, "Next steps", "What's next",
           "What's left in the plan is identity: more identities per run, the Entra suites in live CI and hosted "
           "agents.")
    items = [("Role matrix", "Run several test identities in one run, each case tagged with the identity it expects.",
              ORANGE),
             ("Entra suites in live CI", ["Add the Entra suites to the daily run, which needs a test user who has "
                                          "consented.",
                                          "**Also:** confirm consent survives the connection's refresh-token expiry."],
              PURPLE),
             ("Hosted agents", ["Cover hosted agents, which forward their call ID (`x-agent-foundry-call-id`) to the "
                                "toolbox.",
                                "**Recheck** `UserEntraToken` for custom audiences."], BLUE)]
    row(s, items, y=159.8, h=140, gap=20)
    panel(s, 39.6, 314, 878.4, 174, adj=0.046)
    stack(s, 56.2, 351.5, 115.2, [("CONVERSATIONS", TEAL), ("JUDGES", PURPLE), ("GROUNDING", BLUE)])
    ptitle(s, 190.8, 350, 710, "Done since the first version")
    text(s, 190.8, 382, 710, 94, [
        "**Chained turns:** later turns continue the stored response, so the agent keeps its tool results; ASSERT "
        "runs record provenance.",
        "**Foundry evaluators:** this repo's own `tool_use` and `grounded_adherence`, calibrated on known verdicts "
        "and run in live CI.",
        "**Grounding:** a figure check in the agent evals, and a fresh holdout set on which a stronger model cut "
        "invented figures from 9 to 0."],
        size=13.2, c=DARK, anchor="t", after=6, bullet=True)
    footer(s, page)


SLIDES = [s11_telemetry, s12_ci, s13_findings, s14_start, s15_next]
