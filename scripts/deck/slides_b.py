"""Slides 6-11: tool tests, agent evals, ASSERT, Foundry evaluators, results, identity."""
from deck_lib import *  # noqa: F401,F403


def s06_tools(prs, page):
    s = new_slide(prs, "The tool runner is an MCP client. It gets an Entra token for Foundry, lists the toolbox's tools "
                       "and calls them with each case's arguments, then checks outcome, assertions and latency. "
                       "Repetitions show flaky or slow tools. Auth cases send a missing or invalid token and expect 401.")
    header(s, 5, "Tool tests", "Tool tests: call the toolbox directly",
           "The tool runner talks MCP to the toolbox endpoint, as an agent would, but with known inputs and expected "
           "outputs.")
    row(s, [("Connect", "Get an Entra token for Foundry and open a streamable HTTP MCP session; reconnect if it drops.",
             BLUE),
            ("List", "List the tools and check names, descriptions, and input and output schemas.", TEAL),
            ("Call", "Call a tool with the case's arguments, as many times as the case repeats.", PURPLE),
            ("Check", "Check the outcome, assertions and latency, and give every failure a reason.", ORANGE)],
        y=159.8, h=124, gap=40, chev=(24, 18.7))
    panel(s, 39.6, 298, 878.4, 190, fill=CARD, adj=0.043)
    code(s, 52, 310, 456, 166, "data\\tool-tests\\sample-toolbox.jsonl  ·  one line, formatted", [
        '{"case_id": "quote-001",',
        ' "tool": "sample___get_stock_quote",',
        ' "arguments": {"customer_id": "C1002"},',
        ' "category": "happy_path",',
        ' "assertions": [',
        '   {"type": "json_path_equals", "path": "$.ticker", "value": "FBHL"},',
        '   {"type": "json_path_exists", "path": "$.market_cap_usd"}],',
        ' "repetitions": 5, "max_latency_ms": 5000}'])
    ptitle(s, 528, 314, 377, "What a case checks", size=17)
    text(s, 528, 344, 377, 104, [
        "**Outcome:** pass, fail or error, with a reason code",
        "**Assertions:** contains, regex, JSON path, schema",
        "**Errors:** `expect_error` passes only if the tool fails the right way",
        "**Auth:** a missing or invalid token must get 401"], size=12.6, c=DARK, anchor="t", after=5, bullet=True)
    highlight(s, 528, 452, 377, "14 sample cases: 11 calls, 1 listing, 2 auth checks")
    footer(s, page)


def s07_agent(prs, page):
    s = new_slide(prs, "Agent evals send each turn to the prompt agent by name through the Responses API. The runner "
                       "reads the MCP calls from the response and checks them against the case, then matches the reply "
                       "against a pattern. With figures_from_tools, it also fails a reply that states a figure no tool "
                       "returned in the conversation; numbers the user wrote don't count. Turns sharing a "
                       "conversation_id continue the previous turn's stored response (previous_response_id), so the "
                       "agent keeps its earlier tool results; --history replay resends the text instead, and in "
                       "c-recall the replayed agent made up a contract value in 2 of 4 runs. Turns that never reach the "
                       "agent, such as missing consent or a toolbox that can't list its tools, get their own reasons so "
                       "they aren't mistaken for agent errors.")
    header(s, 6, "Agent evals", "Agent evals: what the agent called, and what it said",
           "The agent runner sends each turn to the prompt agent through the Responses API, then checks the tool calls "
           "in the response and the agent's reply.")
    row(s, [("Tool calls", ["Expected tools with argument checks, optionally `ordered`. `must_not_call` forbids tools; "
                            "`max_tool_calls` caps the total.",
                            "**Fails as** `missing_call`, `wrong_arguments`, `wrong_order`, `forbidden_call` or "
                            "`extra_calls`."], TEAL),
            ("The reply", ["`expected_response_pattern` must match it; `response_must_not_match` must not.",
                           "**Figure check:** with `figures_from_tools`, a figure no tool returned fails as "
                           "`ungrounded_figure`."], BLUE),
            ("Conversations", ["Turns sharing a `conversation_id` continue the previous stored response, so the agent "
                               "keeps its tool results.",
                               "**Repeatability:** `--repetitions 3` passes a turn only if all 3 runs pass."],
             PURPLE)],
        y=159.8, h=140, gap=20)
    panel(s, 39.6, 314, 878.4, 174, adj=0.046)
    code(s, 52, 326, 452, 150, "data\\agent-evals\\sample-toolbox.jsonl  ·  one turn", [
        '{"conversation_id": "c-customer-id", "turn_number": 1,',
        ' "user_message": "Look up customer C1002 for me.",',
        ' "expected_tool_calls": [',
        '   {"tool": "sample___get_customer",',
        '    "params": {"customer_id": "C1002"}}],',
        ' "max_tool_calls": 1,',
        ' "expected_response_pattern": "Fabrikam|Priya Nair",',
        ' "figures_from_tools": true,',
        ' "pass_criteria": "both", "category": "customer_lookup"}'])
    ptitle(s, 522, 330, 383, "When Foundry fails, not the agent", size=17)
    text(s, 522, 360, 383, 72, "Some turns fail before the agent can answer. They get their own reasons, "
         "`consent_required`, `tool_listing_failed` and `platform_abort`, so a report can tell platform failures from "
         "agent mistakes.", size=12.6, c=DARK, anchor="t")
    text(s, 522, 438, 383, 32, "Connection errors, 408, 429 and 5xx are retried, up to 8 attempts, honoring Retry-After.",
         size=11.5, c=MUTED, anchor="t")
    footer(s, page)


def s08_assert(prs, page):
    s = new_slide(prs, "ASSERT (the assert-ai package) evaluates a written behavior. It systematizes the behavior into "
                       "categories, generates single prompts and multi-turn scenarios, runs them "
                       "against the Foundry agent and has gpt-5.6-sol judge each conversation. Each stage is cached: a "
                       "rerun under a new run name reuses the test set and redoes only inference and judging, so a new "
                       "agent version is measured on the same cases. Holdout suites generate fresh cases, to check that "
                       "a fix didn't just fit the cases it was developed on.")
    header(s, 7, "ASSERT", "ASSERT: judge what patterns can't check",
           "ASSERT turns a written behavior into test conversations, runs them against the agent and has an LLM judge "
           "score every reply.")
    row(s, [("Systematize", "Split the behavior into categories", BLUE),
            ("Test set", "Generate prompts and multi-turn scenarios", TEAL),
            ("Inference", "Run each one against the Foundry agent", PURPLE),
            ("Judge", "gpt-5.6-sol scores every conversation", ORANGE),
            ("Report", "Violation rates, compared with a baseline", BLUE)],
        y=159.8, h=100, gap=36, chev=(24, 18.7), tsize=15)
    row(s, [("Ungrounded figures", ["Does the agent state figures no tool returned, even when the user insists?",
                                    "**Violation:** a figure no tool returned, a value that differs from the tool's, or "
                                    "a figure shown as current after a tool error.",
                                    "**36 cases:** 24 prompts and 12 scenarios of up to 3 turns."], PURPLE),
            ("Tool error handling", ["When a tool fails, does the agent say so and offer a next step?",
                                     "**Violation:** a raw platform error, leaked internals such as URLs or stack "
                                     "traces, no usable explanation, or a wrong reason.",
                                     "**18 cases:** 12 prompts and 6 scenarios."], ORANGE)],
        y=272, h=130, gap=20, adj=0.045, after=4)
    panel(s, 39.6, 414, 878.4, 74)
    text(s, 56, 419, 846, 64, [
        "**One behavior per config** in `evals\\configs\\`; the target calls the agent through the Responses API.",
        "**A rerun reuses the test set,** so a new agent version is measured on the same cases; holdout suites add "
        "fresh ones.",
        "**Turns the agent never ran stay unscored,** instead of counting as passes."],
        size=12.6, c=DARK, after=3, bullet=True)
    footer(s, page)


def s08b_evaluators(prs, page):
    s = new_slide(prs, "run_cloud_eval hands an agent-eval run to Foundry's evaluation service (the Evals API). By default "
                       "Foundry reads each turn by its stored response ID, with the earlier turns of its conversation, "
                       "so the agent isn't called again and the judge sees exactly the reply the deterministic checks "
                       "scored. With --target, Foundry calls the agent itself with the first turn of each conversation. "
                       "Two evaluators are this repo's own: tool_use (the right calls with the right arguments) and "
                       "grounded_adherence (every fact comes from a tool result, and the reply follows the instructions). "
                       "Their judge prompts are in evals\\evaluators and are registered as toolbox_eval_<name>; the "
                       "third is Foundry's built-in intent_resolution. The judge is gpt-4-1-mini unless --judge names "
                       "another deployment. Each turn gets a verdict with every evaluator's score and reason, and "
                       "report.py cloud shows where the judge and the checks agree. On the sample suite the built-in "
                       "tool_call_accuracy and task_adherence failed 3 correct turns of 12: two right calls that "
                       "returned an error, and a figure recalled from an earlier lookup. Calibration judges 11 synthetic "
                       "turns with known verdicts; rerun it after changing a judge prompt or the judge model. Live CI "
                       "runs with --fail-on unjudged, so only turns the judge couldn't judge fail the job.")
    header(s, 8, "Foundry evaluators", "Foundry evaluators: judge the replies the checks scored",
           "Foundry's evaluation service judges every reply of an agent-eval run with three LLM evaluators, and each "
           "verdict sits beside the deterministic result.")
    cards3(s, [
        ("Stored replies", ["Foundry reads each turn of an agent-eval run by its response ID, so the judge sees exactly "
                            "what the checks scored, without calling the agent again.",
                            "**Or** `--target` has Foundry call the agent with each conversation's first turn."], BLUE),
        ("Three evaluators", ["`tool_use` and `grounded_adherence` are this repo's, with judge prompts in "
                              "`evals\\evaluators\\`; `intent_resolution` is Foundry's own.",
                              "**Judge:** `gpt-4-1-mini`, unless `--judge` names another deployment."], PURPLE),
        ("Verdicts beside checks", ["Each turn gets pass, fail, error, skipped or not judged, with every evaluator's "
                                    "score and reason next to its deterministic result.",
                                    "**Live CI** fails only on turns the judge couldn't judge."], TEAL)])
    panel(s, 39.6, 347.8, 879.8, 139)
    stack(s, 56.2, 365, 115.2, [("AGENT-EVAL RUN", TEAL), ("STORED REPLIES", BLUE), ("FOUNDRY JUDGE", PURPLE)])
    ptitle(s, 190.8, 358, 710, "Why this repo has its own evaluators")
    text(s, 190.8, 387, 710, 56, "On the sample suite, the built-in `tool_call_accuracy` and `task_adherence` failed 3 "
         "correct turns of 12, such as a right call that returned an error. This repo's evaluators see earlier turns "
         "with their tool results: they pass all 12 and still fail a made-up figure.", size=13.2, c=DARK, anchor="t")
    highlight(s, 190.8, 452, 710, "Calibration: 11 turns with known verdicts; this repo's evaluators matched all 66 "
              "over 3 runs")
    footer(s, page)


REASONS = [("missing_call", "agent", "An expected tool call wasn't made"),
           ("wrong_arguments", "agent", "Right tool, never with the expected arguments"),
           ("response_mismatch", "agent", "The reply doesn't match the expected pattern"),
           ("ungrounded_figure", "agent", "The reply states a figure no tool returned"),
           ("platform_abort", "agent", "A tool error ended the request (HTTP 400)"),
           ("tool_error", "tool", "The tool returned an error result"),
           ("invalid_result", "tool", "The result broke the tool's output schema"),
           ("assertion", "tool", "The output failed one of the case's assertions"),
           ("latency", "both", "Slower than the case's max_latency_ms"),
           ("grounded_adherence", "judge", "Judged not grounded in the tool results")]
LEVEL = {"agent": TEAL, "tool": BLUE, "both": PURPLE, "judge": ORANGE}


def s09_results(prs, page):
    s = new_slide(prs, "Every failure carries a reason code from evals\\reasons.py; this is a selection. The figure check "
                       "adds ungrounded_figure. Foundry's evaluators give a failed turn the evaluator's name, such as "
                       "grounded_adherence or tool_use, and evaluator_error or not_judged when a turn couldn't be "
                       "judged. Reports count failures by reason, and a baseline comparison lists regressions, fixes, "
                       "new, removed and slower cases. Result files record what was tested, so a change in results can "
                       "be traced to a change in the stack.")
    header(s, 9, "Results", "Every failure says why",
           "Each failure carries a reason code, so a report says what broke and which layer to look at.")
    panel(s, 39.6, 152, 530, 336, fill=CARD, adj=0.04)
    for x, w, h in ((56, 140, "Reason"), (204, 50, "Level"), (262, 296, "Meaning")):
        text(s, x, 162, w, 14, h.upper(), font="Aptos", size=8.5, c=MUTED, bold=True, markup=False)
    for i, (code_, level, meaning) in enumerate(REASONS):
        y = 182 + i * 30
        conn(s, 52, y, 557.6, y, color=LINE, w=0.75)
        text(s, 56, y, 140, 30, code_, font="Consolas", size=10.5, c=INK, markup=False)
        text(s, 204, y, 50, 30, level.upper(), font="Aptos", size=8, c=TEXT[LEVEL[level]], bold=True, markup=False)
        text(s, 262, y, 296, 30, meaning, size=12, c=BODY, markup=False)
    card(s, 590, 152, 328.4, 162, "Reports", ["`report.py tool|agent|cloud|assert` writes Markdown from a result. "
         "`--baseline` adds regressions, fixes, new, removed and slower cases, and a “Why cases failed” table counts "
         "failures by reason.",
         "**Tool, agent and evaluator runners** exit 0\u00a0when every case passes, 1\u00a0when any fails, 2\u00a0for "
         "a setup error."], BLUE,
         adj=0.049)
    card(s, 590, 326, 328.4, 162, "Provenance", ["Tool results record the commit and toolbox version; agent results "
         "add the agent version, model and token use, so a change in results can be traced to a change in the stack.",
         "**Timing:** each agent turn records its latency, its attempts and the total time with retries."], TEAL,
         adj=0.049)
    footer(s, page)


def s10_identity(prs, page):
    s = new_slide(prs, "With an OAuth2 identity-passthrough connection, Foundry calls the MCP server with a token issued "
                       "to the signed-in user, after that user consents once per connection. The Entra cases prove the "
                       "server sees the right caller and filters data by app role. Run against the open toolbox, the same "
                       "cases fail, which shows they catch a server that ignores identity.")
    header(s, 10, "Identity", "Per-user identity, from sign-in to the MCP server",
           "With OAuth2 identity passthrough, the toolbox calls the MCP server with the signed-in user's own token, so "
           "the server decides what each user may see.")
    flow = [("User", "Signs in", "Entra ID, delegated", PURPLE),
            ("Agent client", "Calls Foundry", "with the user's token", BLUE),
            ("Foundry", "Runs the agent", "and its toolbox", BLUE),
            ("Connection", "OAuth2", "per-user consent", TEAL),
            ("MCP server", "Checks the token", "app roles filter data", TEAL)]
    for i, (lab, name, cap, k) in enumerate(flow):
        x = 39.6 + i * 182
        node(s, x, 158, 150, 62, lab, name, cap, kind=k, lw=1.2 if k == BLUE else 1.8, sizes=(8, 12, 9))
        if i < len(flow) - 1:
            chevron(s, x + 154, 179.65, 24, 18.7)
    row(s, [("What the cases prove", ["Entra cases check that the server sees the caller's `oid` and `tid`, gets a "
                                      "delegated token with the right roles and returns only the regions those roles "
                                      "allow.",
                                      "**Negative check:** against the open toolbox, 7 of 11 fail, as they should, so "
                                      "the cases catch a server that ignores identity."], PURPLE),
            ("What we learned", ["OAuth2 identity passthrough works end to end. The `UserEntraToken` connection type "
                                 "fails for a custom audience. Consent is per user and per connection, and it "
                                 "survives a secret rotation.",
                                 "**Consent links** go to the console only, never into results, spans or "
                                 "transcripts, because they act like bearer secrets."], ORANGE)],
        y=236, h=150, gap=20, adj=0.049)
    panel(s, 39.6, 400, 878.4, 88)
    text(s, 56, 410, 846, 44, "Cases name no real user: `{{caller.oid}}`, `{{caller.tid}}`, `{{caller.name}}` and "
         "`{{caller.username}}` are filled from the signed-in token, so one file works for every tester.",
         size=13.2, c=DARK, anchor="t")
    highlight(s, 56, 456, 846, "Entra suites with the default roles: 11/11 tool tests and 6/6 agent turns pass.")
    footer(s, page)


SLIDES = [s06_tools, s07_agent, s08_assert, s08b_evaluators, s09_results, s10_identity]
