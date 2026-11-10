# Task

You judge one turn of a conversation between a user and an AI agent that answers with tools. Rate only the agent's
tool calls in this turn: whether it made the calls the user's request needed, with the right tools and arguments, and
no others. Another evaluator judges the final reply, so what the reply says must not affect this score.

# Inputs

- CONVERSATION: what came before the agent's turn. It may start with the agent's instructions (a system message) and
  hold earlier turns with their tool calls and tool results. It ends with the user's latest message, which this turn
  answers. It may be only that message.
- TURN: what the agent did after the user's latest message: its tool calls, each tool's result and its final reply. It
  may start with the agent's instructions (a system message).
- TOOL DEFINITIONS: the tools the agent could call.

# How to judge

1. Decide what data the user's latest message needs, and whether a call is needed to get it. No call is needed:
   - when the user asks the agent not to look anything up, even if the data isn't in the conversation. Then making no
     call, and saying the data isn't available or asking whether to look it up, is right;
   - when an earlier tool result in CONVERSATION already holds the answer;
   - when the request needs no tool data, for example arithmetic or a greeting;
   - when the request is unclear and the agent asks the user what they mean.

   Otherwise the agent needs to call the tools that provide the data.
2. Check each call in TURN:
   - The tool fits the request.
   - The arguments meet the tool's definition and match what the user asked for, or come from earlier tool results.
     Optional arguments may be left out or null, and when a tool takes one of several identifiers (for example an ID,
     a name or a ticker), one is enough. An argument that differs from what the user asked for, such as a misspelled
     name or another customer's ID, is wrong.
   - The call is related to the request and doesn't repeat a lookup whose result the agent already has.
3. Treat a tool error by its cause. A tool result can be an error or say the data is unavailable: for example, no
   customer matches the ID, a privately held company has no stock quote, the industry isn't supported, or access is
   denied. If the call was right, that result answers the request and the agent needs no retry: the call counts as
   right. If the call was wrong, the error is the agent's mistake, unless it retried with the right arguments.
4. Score the calls only, never the reply. If the calls were right, they score as right even when the reply misstates,
   invents or contradicts data, or ignores a tool result. If no call was needed and none was made, the score is 5 even
   when the reply states figures that no tool returned: that is the reply's mistake, not a tool-use mistake. Read the
   reply only to see whether the agent asked the user a question instead of calling a tool.

# Scores

- 5: The calls the request needed were made and right, and no call was unneeded; or no call was needed and none was
  made, whatever the reply says. A right call whose result is an error or says the data is unavailable counts as
  right.
- 4: Right, with one unneeded or repeated call that didn't change the outcome.
- 3: The needed data was retrieved, but with several unneeded calls, or only after a wrong call the agent corrected.
- 2: A needed call is missing, or a call used the wrong tool or a wrong argument and the agent didn't correct it.
- 1: No tool was used although the request clearly needed one, or every call is unrelated to the request.

# Output

Return only a JSON object with the reason first and then the score that matches it:
{"reason": "<one or two sentences: the data the request needed, and whether the calls got it>", "result": <integer
from 1 to 5>}

# Data

CONVERSATION:
{{query}}

TURN:
{{response}}

TOOL DEFINITIONS:
{{tool_definitions}}
