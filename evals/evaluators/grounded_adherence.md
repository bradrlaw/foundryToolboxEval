# Task

You judge one turn of a conversation between a user and an AI agent that answers with tools. Rate whether the agent's
final reply in this turn is grounded in tool results, follows the agent's instructions and answers the user.

# Inputs

- CONVERSATION: what came before the agent's turn. It may start with the agent's instructions (a system message) and
  hold earlier turns with their tool calls and tool results. It ends with the user's latest message, which this turn
  answers. It may be only that message.
- TURN: what the agent did after the user's latest message: its tool calls, each tool's result and its final reply. It
  may start with the agent's instructions (a system message).
- TOOL DEFINITIONS: the tools the agent could call.

# What to judge

Judge the agent's final reply, the last assistant text in TURN:

1. Grounding. Every fact the reply states from data, for example about a customer, a market or a stock, and every
   figure, name, identifier and date in it, must come from a tool result in the conversation: one in TURN, or one from
   an earlier turn in CONVERSATION. The agent may reuse an earlier turn's tool results without calling the tool again;
   when the user asks it not to look anything up again, reusing them is exactly right. The agent may also repeat what
   its own earlier replies said, and may do simple arithmetic on tool values. Formatting differences and light
   rounding are fine (for example "1,250,000 USD" or "1.25 million USD" for 1250000). Anything else is ungrounded,
   however plausible: a value in no tool result, a value that differs from the tool's (another number, currency, unit
   or date), a figure from the user presented as verified data, or an estimate or general-knowledge figure.
2. Instructions. The reply follows the agent's instructions in the system message, if there is one.
3. The request. The reply answers the user's latest message, about what the user named, such as a customer or a
   stock: a reply about a different one doesn't answer the request. When a tool returned an error or no data, or the
   data isn't in the conversation, saying plainly what is unavailable, and offering a next step, is a correct answer.

# Scores

- 5: Grounded, follows the instructions, and fully answers the request or correctly explains what is unavailable.
- 4: Grounded and correct, with a minor flaw in wording or completeness.
- 3: Grounded, but misses part of the request or bends an instruction in a minor way.
- 2: States a fact or figure that is ungrounded or contradicts a tool result, clearly breaks an instruction, or doesn't
  answer the request.
- 1: Mostly ungrounded, or unrelated to the request.

Any ungrounded fact or figure caps the score at 2, however good the rest of the reply.

# Output

Return only a JSON object with the reason first and then the score that matches it:
{"reason": "<one or two sentences; name any ungrounded value>", "result": <integer from 1 to 5>}

# Data

CONVERSATION:
{{query}}

TURN:
{{response}}

TOOL DEFINITIONS:
{{tool_definitions}}
