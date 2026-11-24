"""Free-tier gate marker for the OpenCode Zen gateway.

The gateway at ``opencode.ai/inference/openai/v1`` rejects anonymous free-model
requests with ``429 FreeUsageLimitError`` unless the request carries the
official OpenCode client signature. That signature has three parts:

1. The transport TLS fingerprint must be the one Bun produces for the official
   client (see ``zen.BUN_JA3``).
2. ``User-Agent`` must match ``opencode/latest/<version>/<channel>`` and the
   session headers must carry an official-shaped ``ses_`` id.
3. The system message must carry the opening of the prompt that matches the
   request shape: the *title-generation* prompt for tool-less requests, the
   *complete agent* prompt (plus a ``read`` tool) when tools are sent. Similar-
   looking text is rejected (see ``zen_prompts`` for the measurements).

The marker is sent verbatim, ahead of this client's own instructions, purely as
a fingerprint. The trailing notice tells the model to ignore it.
"""

from .zen_prompts import AGENT_PROMPT, MARKER_NOTICE, TITLE_PROMPT

# Kept for callers/tests that only know the tool-less marker.
GATE_MARKER = TITLE_PROMPT
GATE_NOTICE = MARKER_NOTICE


def gate_marked_messages(messages: list[dict], has_tools: bool = False) -> list[dict]:
    """Return messages with the official gate marker prepended to system text.

    The gateway checks the system message content, so every outbound request
    needs a marker even when the caller supplied its own system prompt. Which
    marker is accepted depends on the request shape: ``has_tools`` selects the
    full agent prompt (the only content the gateway accepts alongside a tool
    list) instead of the shorter title prompt.

    Copies the affected message rather than mutating saved history.
    """
    marked = (AGENT_PROMPT if has_tools else TITLE_PROMPT) + MARKER_NOTICE
    out = list(messages)
    for i, message in enumerate(out):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            out[i] = {**message, "content": marked + "\n\n" + content}
        elif isinstance(content, list):
            out[i] = {**message, "content": [{"type": "text", "text": marked}, *content]}
        else:
            out[i] = {**message, "content": marked}
        return out
    return [{"role": "system", "content": marked}, *out]
