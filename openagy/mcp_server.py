"""MCP server exposing openagy tools (stdio transport).

Run by OpenCode; registered in opencode.jsonc under mcp.local.openagy.
"""

from __future__ import annotations

import functools
from typing import Any

from mcp.server.fastmcp import FastMCP

from .api import Openagy
from .client import RpcError
from .discover import DiscoveryError

mcp = FastMCP("openagy")
_api = Openagy()


def _guard(fn):
    """Convert expected exceptions into readable tool errors, preserving any
    conversation_id argument so the agent can retry in the same conversation."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (DiscoveryError, RpcError, KeyError) as e:
            out = {"error": str(e), "errorType": type(e).__name__}
            cid = kw.get("conversation_id")
            if cid:
                out["conversationId"] = cid
                out["note"] = ("The conversation still exists — retry via openagy_ask with "
                               "conversation_id; do not create a new conversation.")
            return out
    return wrapper


@mcp.tool()
@_guard
def status() -> dict[str, Any]:
    """Check whether Antigravity is reachable; returns port, app version and default model."""
    return _api.status()


@mcp.tool()
@_guard
def list_models() -> dict[str, Any]:
    """List Antigravity's available chat models (ids like 'gemini-3.8-flash-high' or 'custom-...')."""
    return _api.list_models()


@mcp.tool()
@_guard
def quota(force_refresh: bool = True) -> dict[str, Any]:
    """Check Antigravity quota usage: per-model-group limits (weekly and
    5-hour windows), remaining percentages, and reset times. Call before
    starting heavy or long-running work, or when requests fail."""
    return _api.quota(force_refresh=force_refresh)


@mcp.tool()
@_guard
def list_conversations(limit: int = 25) -> list[dict[str, Any]]:
    """List recent Antigravity conversations (id, title, workspace, status, killed).

    Use to find a conversation's conversationId for attaching (follow-ups,
    retries) with openagy_ask / openagy_wait / openagy_read_conversation."""
    return _api.list(limit)


@mcp.tool()
@_guard
def ask(prompt: str, workspace: str | None = None, model: str | None = None,
                conversation_id: str | None = None, raw: bool = False,
                inactivity_timeout: float = 240.0) -> dict[str, Any]:
    """Send a prompt to Antigravity (Gemini) and wait for the reply.

    ATTACHING: to continue, retry, or follow up in an EXISTING conversation,
    ALWAYS pass its conversation_id (from a previous result, or found via
    openagy_list_conversations) — including after errors, kills, or timeouts.
    Only omit conversation_id to start a genuinely new conversation; never
    recreate a conversation to retry.

    New conversations: pass the project directory as workspace. Prompts are
    sanitized by default to read as the user's own technical question; pass
    raw=True to send verbatim. The conversation persists in Antigravity's
    sidebar for the user to continue.

    For lengthy tasks: raise inactivity_timeout (up to ~1700s) rather than
    assuming failure. If this call times out (MCP -32001), the Antigravity
    conversation KEEPS RUNNING — recover with openagy_wait(conversation_id)
    or openagy_read_conversation, never by resending the prompt.
    """
    return _api.ask(
        prompt, workspace=workspace, model=model, conversation_id=conversation_id,
        raw=raw, wait=True, inactivity_timeout=inactivity_timeout,
    )


@mcp.tool()
@_guard
def wait(conversation_id: str, timeout_seconds: float = 240.0,
                 include_tools: bool = False) -> dict[str, Any]:
    """Wait for an Antigravity conversation to finish (bounded, resumable).

    Returns status 'idle' with the transcript and latest reply, status
    'running' with partial progress when timeout_seconds elapses — call again
    to keep waiting — or status 'waiting_for_interaction' with the pending
    question/confirmation when Gemini is waiting on the user. Use this to
    recover an openagy_ask call that timed out, or to babysit long tasks.
    """
    return _api.wait(conversation_id, timeout_seconds=timeout_seconds,
                     include_tools=include_tools)


@mcp.tool()
@_guard
def answer_question(conversation_id: str,
                            answers: list[dict[str, Any]] | None = None,
                            cancelled: bool = False,
                            inactivity_timeout: float = 240.0) -> dict[str, Any]:
    """Answer a question Gemini asked in an Antigravity conversation.

    Use when openagy_ask/openagy_wait returned status 'waiting_for_interaction'
    with kind 'askQuestion' (never resend the prompt instead). Provide one
    answer object per pending question, by position: {"selectedOptionIds":
    ["<id> from the interaction's options"]} and/or {"writeInResponse":
    "free text"} or {"skipped": true}. Then waits for Gemini's continuation
    and returns the reply. Set cancelled=true to dismiss the question.
    """
    return _api.answer_question(conversation_id, answers=answers, cancelled=cancelled,
                               inactivity_timeout=inactivity_timeout)


@mcp.tool()
@_guard
def approve(conversation_id: str, allow: bool = True,
                    scope: str = "PERMISSION_SCOPE_ONCE",
                    inactivity_timeout: float = 240.0) -> dict[str, Any]:
    """Approve or deny a pending confirmation in an Antigravity conversation.

    Use when openagy_ask/openagy_wait returned status 'waiting_for_interaction'
    with a confirmation kind (runCommand, openBrowserUrl, mcp, filePermission,
    permission, browserAction, ...). Auto-detects the pending interaction;
    scope applies to permission/filePermission kinds (PERMISSION_SCOPE_ONCE |
    _CONVERSATION | _WORKSPACE | _GLOBAL | _PROJECT). Then waits for Gemini's
    continuation and returns the reply.
    """
    return _api.approve(conversation_id, allow=allow, scope=scope,
                       inactivity_timeout=inactivity_timeout)


@mcp.tool()
@_guard
def undo(conversation_id: str, step_index: int | None = None,
                 keep_message: bool = False,
                 conversation_only: bool = True) -> dict[str, Any]:
    """Rewind an Antigravity conversation ('Undo to this point').

    Default: removes the LAST full turn (user message + response) so it can be
    re-asked differently. Use this when a turn was content-filtered or went
    sideways — rewind first, then send a differently-phrased prompt with
    openagy_ask, instead of stacking rephrased retries on top (which further
    tips off the filters). The model genuinely forgets everything removed.

    Options: keep_message=True keeps the last user message and only removes its
    response; step_index=N keeps steps 0..N and truncates the rest;
    conversation_only=False also rolls back workspace file edits (the result
    lists them via revert preview first).

    Gemini cannot rewind or forget by itself — never send a chat message
    asking it to (such prompts are intercepted and rejected). This tool is the
    rewind.
    """
    return _api.undo(conversation_id, step_index=step_index,
                     keep_message=keep_message, conversation_only=conversation_only)


@mcp.tool()
@_guard
def reask(conversation_id: str, prompt: str, raw: bool = False,
                  keep_message: bool = False,
                  inactivity_timeout: float = 240.0) -> dict[str, Any]:
    """Rewind the last turn of an Antigravity conversation and re-ask in one step.

    THE retry primitive for filtered or failed turns: removes the last full
    turn (Gemini genuinely forgets it — verified), then sends your rephrased
    prompt in the same conversation and waits for the reply. Pass the actual
    rephrased prompt; do NOT also call openagy_undo first (it is included).
    keep_message=True keeps the last user message and only regenerates the
    response.
    """
    return _api.reask(conversation_id, prompt, raw=raw,
                     keep_message=keep_message,
                     inactivity_timeout=inactivity_timeout)


@mcp.tool()
@_guard
def read_conversation(conversation_id: str, include_tools: bool = False) -> dict[str, Any]:
    """Read an Antigravity conversation's transcript (user/model messages, questions, errors).

    The result includes 'status' (CASCADE_RUN_STATUS_*) and 'idle' — use it to
    check on a conversation without waiting."""
    return _api.read(conversation_id, include_tools=include_tools)


@mcp.tool()
@_guard
def delete_conversation(conversation_id: str) -> dict[str, Any]:
    """Delete an Antigravity conversation (summary + trajectory)."""
    return _api.delete(conversation_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
