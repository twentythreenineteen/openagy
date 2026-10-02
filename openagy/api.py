"""High-level operations shared by the CLI and the MCP server."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .client import AntigravityClient, RpcError
from .sanitize import sanitize, sanitize_light
from .transcripts import is_idle, list_conversations, summarize_steps, conversation_status

_REGISTRY_PATH = Path.home() / ".openagy" / "conversations.json"

_INTERACTION_NOTE = (
    "Gemini is waiting for user input ('{kind}'). Answer it with "
    "openagy_answer_question (for questions: pass selectedOptionIds from the "
    "interaction's options, or writeInResponse for free text) or "
    "openagy_approve (for confirmations/permissions) — do NOT resend the "
    "prompt with openagy_ask." )

# Prompts that ask Gemini to rewind/forget are a known agent failure mode:
# Gemini cannot rewind itself, and such meta-messages keep the filtered
# content in play. Detected on follow-ups and rejected with guidance.
_ROLLBACK_RE = re.compile(
    r"(\[ROLLBACK_REQUEST\]|rewind and forget|please rewind|"
    r"forget the (?:previous|last) (?:message|turn|prompt)|treat this as a signal)",
    re.IGNORECASE,
)


def _load_registry() -> dict[str, str]:
    try:
        return json.loads(_REGISTRY_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_registry(reg: dict[str, str]) -> None:
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY_PATH.write_text(json.dumps(reg, indent=1), "utf-8")


class Openagy:
    def __init__(self, client: AntigravityClient | None = None):
        self.client = client or AntigravityClient()

    # ------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        from . import __version__
        t = self.client.target
        return {
            "version": __version__,
            "baseUrl": t.base_url,
            "appVersion": t.app_version,
            "defaultModel": self.client.default_model_id(),
        }

    # ------------------------------------------------------------- models
    def list_models(self) -> dict[str, Any]:
        resp = self.client.get_available_models()
        models = []
        for mid, info in sorted(resp.get("models", {}).items()):
            models.append({
                "id": mid,
                "displayName": info.get("displayName"),
                "model": info.get("model"),
                "recommended": info.get("recommended", False),
            })
        return {"default": resp.get("defaultAgentModelId"), "models": models}

    # --------------------------------------------------------------- quota
    def quota(self, force_refresh: bool = True) -> dict[str, Any]:
        """Quota usage per model group (weekly + 5-hour limits, resets)."""
        resp = self.client.get_quota(force_refresh=force_refresh)
        groups: list[dict[str, Any]] = []
        lowest: float | None = None
        for g in resp.get("groups", []):
            buckets = []
            for b in g.get("buckets", []):
                if b.get("disabled"):
                    continue
                pct = round((b.get("remainingFraction") or 0.0) * 100, 1)
                if lowest is None or pct < lowest:
                    lowest = pct
                buckets.append({
                    "id": b.get("bucketId"),
                    "window": b.get("window"),
                    "remainingPercent": pct,
                    "resetTime": b.get("resetTime"),
                    "description": b.get("description"),
                })
            groups.append({
                "name": g.get("displayName"),
                "description": g.get("description"),
                "buckets": buckets,
            })
        result: dict[str, Any] = {"groups": groups, "lowestRemainingPercent": lowest}
        if lowest is not None and lowest < 20:
            result["note"] = ("Quota is running low on at least one limit — consider "
                              "deferring heavy work or switching model groups.")
        return result

    # ------------------------------------------------------ conversations
    def list(self, limit: int = 25) -> list[dict[str, Any]]:
        return list_conversations(limit=limit)

    def read(self, conversation_id: str, include_tools: bool = False) -> dict[str, Any]:
        steps = self.client.get_steps(conversation_id)
        status = conversation_status(conversation_id)
        return {
            "conversationId": conversation_id,
            "status": status,
            "idle": is_idle(conversation_id),
            "messages": summarize_steps(steps, include_tools=include_tools),
        }

    def wait(self, conversation_id: str, timeout_seconds: float = 240.0,
             include_tools: bool = False) -> dict[str, Any]:
        """Bounded wait for a conversation to finish; resumable.

        Returns status=idle with the full transcript, status=running with a
        partial transcript if the conversation is still working when the bound
        is hit (call again to continue waiting — Antigravity keeps the
        conversation running regardless), or status=waiting_for_interaction
        with the pending question/confirmation to answer.
        """
        steps = self.client.get_steps(conversation_id)
        start_index = 0
        result: dict[str, Any] = {
            "conversationId": conversation_id,
            "messages": summarize_steps(steps, include_tools=include_tools),
        }
        outcome, pending = self._await_turn(conversation_id, timeout_seconds, start_index)
        if outcome == "waiting" and pending:
            result["status"] = "waiting_for_interaction"
            result["interaction"] = pending
            result["note"] = _INTERACTION_NOTE.format(kind=pending.get("kind", "?"))
            return result
        if outcome == "timeout" and not is_idle(conversation_id):
            result["status"] = "running"
            result["note"] = ("Still working in Antigravity. Call openagy_wait again to keep "
                              "waiting, or openagy_read_conversation to peek at progress.")
            return result
        result["status"] = "idle"
        messages = summarize_steps(self.client.get_steps(conversation_id),
                                  include_tools=include_tools)
        result["messages"] = messages
        model_msgs = [m["text"] for m in messages if m.get("role") == "model" and m.get("text")]
        if model_msgs:
            result["reply"] = model_msgs[-1]
        errs = [m["text"] for m in messages if m.get("role") == "error" and m.get("text")]
        if errs:
            result["error"] = errs[-1]
        return result

    def delete(self, conversation_id: str) -> dict[str, Any]:
        self.client.delete_conversation(conversation_id)
        registry = _load_registry()
        if conversation_id in registry:
            del registry[conversation_id]
            _save_registry(registry)
        return {"conversationId": conversation_id, "deleted": True}

    # ----------------------------------------------------------------- ask
    def ask(self, prompt: str, workspace: str | None = None,
            model: str | None = None, conversation_id: str | None = None,
            raw: bool = False, wait: bool = True,
            inactivity_timeout: float = 240.0) -> dict[str, Any]:
        """Send a prompt (new or existing conversation) and optionally wait.

        Returns conversationId, the reply (last model message) and the tail of
        the transcript since this prompt.
        """
        registry = _load_registry()
        new_convo = conversation_id is None
        if new_convo:
            workspace = workspace or os.getcwd()
            conversation_id = self.client.create_conversation(workspace)
            text = prompt if raw else sanitize(prompt)
            model_id = model or self.client.default_model_id()
            registry[conversation_id] = model_id
            _save_registry(registry)
            # Register the summary so the conversation shows in the sidebar.
            self.client.register_summary(conversation_id, workspace, text)
            start_index = 0
        else:
            existing = self.client.get_steps(conversation_id)
            start_index = len(existing)
            text = prompt if raw else sanitize_light(prompt)
            # Guard: agents sometimes ask Gemini to rewind/forget instead of
            # calling openagy_undo. Intercept before it pollutes the context.
            if _ROLLBACK_RE.search(prompt):
                return {
                    "conversationId": conversation_id,
                    "status": "rejected_rollback_request",
                    "error": ("This prompt reads like a rollback request, but Gemini cannot "
                              "rewind or forget by itself — asking it to do so keeps the "
                              "filtered content in context."),
                    "note": ("Nothing was sent. To retry after a filtered/failed turn: call "
                             "openagy_undo(conversation_id) to remove the last turn (Gemini "
                             "genuinely forgets it), or openagy_reask(conversation_id, "
                             "<rephrased prompt>) to rewind and re-ask in one step."),
                }
            # The language server needs a plan model on every turn; reuse the
            # model this conversation was created with when we know it.
            model_id = model or registry.get(conversation_id) or self.client.default_model_id()

        # Send. On failure, still return the conversationId so the caller can
        # retry IN THIS conversation instead of starting a new one.
        try:
            self.client.send_message(conversation_id, text, model_id=model_id)
        except RpcError as e:
            return {
                "conversationId": conversation_id,
                "status": "send_error",
                "error": str(e),
                "note": ("Send failed, but the conversation still exists. Retry by calling "
                         "openagy_ask again with conversation_id (do NOT create a new "
                         "conversation unless this one was deleted)."),
            }

        result: dict[str, Any] = {"conversationId": conversation_id, "sanitized": not raw}

        if not wait:
            result["status"] = "sent"
            return result

        return self._finish_turn(conversation_id, result, start_index, inactivity_timeout)

    # ------------------------------------------------------- turn lifecycle
    def _await_turn(self, conversation_id: str, inactivity_timeout: float,
                    start_index: int = 0) -> tuple[str, dict[str, Any] | None]:
        """Wait for a turn to finish, detecting pending user interactions.

        Returns ("done", None), ("waiting", <interaction descriptor>) or
        ("timeout", None). WaitForConversationFullyIdle genuinely blocks while
        an interaction pends, so it runs in a daemon thread while the main
        thread polls the trajectory for WAITING steps.
        """
        holder: dict[str, Any] = {}

        def run_wait() -> None:
            try:
                self.client.wait_until_idle(conversation_id, inactivity_timeout=inactivity_timeout)
                holder["done"] = True
            except RpcError as e:
                holder["error"] = str(e)

        threading.Thread(target=run_wait, daemon=True).start()
        deadline = time.monotonic() + max(10.0, inactivity_timeout)
        while time.monotonic() < deadline:
            pending = self.client.pending_interaction(conversation_id, step_offset=start_index)
            if pending:
                return "waiting", pending
            if holder:
                return "done", None
            time.sleep(3.0)
        return "timeout", None

    def _finish_turn(self, conversation_id: str, result: dict[str, Any],
                     start_index: int, inactivity_timeout: float) -> dict[str, Any]:
        """Common post-send logic: wait, detect interactions, extract reply."""
        outcome, pending = self._await_turn(conversation_id, inactivity_timeout, start_index)

        if outcome == "waiting" and pending:
            result["status"] = "waiting_for_interaction"
            result["interaction"] = pending
            result["note"] = _INTERACTION_NOTE.format(kind=pending.get("kind", "?"))
            return result
        if outcome == "timeout":
            result["status"] = "running" if not is_idle(conversation_id) else "done"
            if result["status"] == "running":
                result["note"] = ("Still working in Antigravity (conversation keeps running even if "
                                  "this call times out). Call openagy_wait to keep waiting, or "
                                  "openagy_read_conversation to peek at progress.")
        else:
            # The wait RPC returned: the turn is complete.
            result["status"] = "done"

        steps = self.client.get_steps(conversation_id, step_offset=start_index)
        messages = summarize_steps(steps)
        result["messages"] = messages
        model_msgs = [m["text"] for m in messages if m.get("role") == "model" and m.get("text")]
        if model_msgs:
            result["reply"] = model_msgs[-1]
        errs = [m["text"] for m in messages if m.get("role") == "error" and m.get("text")]
        if errs:
            result["error"] = errs[-1]
            result["note"] = ("The conversation errored but still exists — retry in it by "
                              "calling openagy_ask with conversation_id "
                              f"'{conversation_id}' (same as the Antigravity UI's retry).")
        elif result.get("status") == "done" and not model_msgs and "error" not in result:
            result["note"] = ("No model response captured; the conversation may be waiting for "
                              "user input (question/permission). Use openagy_read_conversation to inspect; "
                              f"respond or follow up via openagy_ask with conversation_id '{conversation_id}'.")
        return result

    # ------------------------------------------------- answer/approve tools
    def answer_question(self, conversation_id: str,
                        answers: list[dict[str, Any]] | None = None,
                        cancelled: bool = False,
                        inactivity_timeout: float = 240.0) -> dict[str, Any]:
        """Answer a pending askQuestion interaction, then wait for the reply.

        Each answer maps to one pending question (by position):
        {"selectedOptionIds": ["1"], "writeInResponse": "...", "skipped": true}.
        """
        pending = self.client.pending_interaction(conversation_id)
        result: dict[str, Any] = {"conversationId": conversation_id}
        if not pending:
            result["error"] = "No pending interaction found — the conversation is not waiting."
            return result
        if pending.get("kind") != "askQuestion":
            result["error"] = (f"Pending interaction is '{pending.get('kind')}' "
                               "(a confirmation), not a question — use openagy_approve.")
            result["interaction"] = pending
            return result

        questions = pending.get("questions", [])
        responses: list[dict[str, Any]] = []
        answers = answers or []
        if cancelled:
            payload = {"askQuestion": {"responses": [], "cancelled": True}}
        else:
            if len(answers) < len(questions):
                # Tolerate single-answer shorthand for a single question.
                if len(questions) == 1 and len(answers) == 1:
                    pass
                else:
                    result["error"] = (f"{len(questions)} question(s) pending, got "
                                       f"{len(answers)} answer(s). Provide one answer per question "
                                       "(see the 'interaction' field).")
                    result["interaction"] = pending
                    return result
            for a in answers:
                e: dict[str, Any] = {}
                if a.get("selectedOptionIds"):
                    e["selectedOptionIds"] = [str(x) for x in a["selectedOptionIds"]]
                if a.get("writeInResponse"):
                    e["writeInResponse"] = str(a["writeInResponse"])
                if a.get("skipped"):
                    e["skipped"] = True
                if not e:
                    e["skipped"] = True
                responses.append(e)
            payload = {"askQuestion": {"responses": responses}}

        start_index = len(self.client.get_steps(conversation_id))
        self.client.send_interaction(conversation_id, pending, payload)
        result["answered"] = True
        return self._finish_turn(conversation_id, result, start_index, inactivity_timeout)

    def approve(self, conversation_id: str, allow: bool = True,
               scope: str = "PERMISSION_SCOPE_ONCE",
               inactivity_timeout: float = 240.0) -> dict[str, Any]:
        """Approve or deny a pending confirmation (command, browser, MCP,
        file access...). Auto-detects the pending interaction."""
        pending = self.client.pending_interaction(conversation_id)
        result: dict[str, Any] = {"conversationId": conversation_id}
        if not pending:
            result["error"] = "No pending interaction found — the conversation is not waiting."
            return result
        kind = pending.get("kind")
        if kind == "askQuestion":
            result["error"] = "Pending interaction is a question — use openagy_answer_question."
            result["interaction"] = pending
            return result

        payload: dict[str, Any]
        if kind in ("permission", "filePermission"):
            payload = {kind: {"allow": allow, "scope": scope}}
            uri = (pending.get("details") or {}).get("absolutePathUri")
            if kind == "filePermission" and uri:
                payload[kind]["absolutePathUri"] = uri
        else:
            payload = {kind: {"confirm": allow}}

        start_index = len(self.client.get_steps(conversation_id))
        self.client.send_interaction(conversation_id, pending, payload)
        result["approved"] = allow
        return self._finish_turn(conversation_id, result, start_index, inactivity_timeout)

    # ------------------------------------------------------------- rewind
    def undo(self, conversation_id: str, step_index: int | None = None,
             keep_message: bool = False, conversation_only: bool = True) -> dict[str, Any]:
        """Rewind a conversation ('Undo to this point').

        Default (step_index=None, keep_message=False): removes the last full
        turn (user message + response) so it can be re-asked differently — the
        right move when a turn got content-filtered, instead of stacking
        rephrased retries. keep_message=True keeps the last user message and
        only removes its response. An explicit step_index keeps steps 0..N and
        truncates the rest; the model's context forgets everything removed.
        """
        from .transcripts import summarize_steps as _summarize

        result: dict[str, Any] = {"conversationId": conversation_id}
        steps = self.client.get_steps(conversation_id)
        if not steps:
            result["error"] = "Conversation has no steps to rewind."
            return result

        registry = _load_registry()
        model_id = registry.get(conversation_id) or self.client.default_model_id()
        enum, _ = self.client.model_enum_id_safe(model_id)
        if enum is None:
            result["error"] = f"Could not resolve model '{model_id}' for the revert."
            return result

        note_extra = ""
        if step_index is None:
            user_positions = [i for i, s in enumerate(steps)
                               if "userInput" in s.get("step", s)]
            if not user_positions:
                result["error"] = "No user message found to rewind to."
                return result
            last_user = user_positions[-1]
            if keep_message:
                step_index = last_user
            elif last_user == 0:
                # First turn: cannot rewind before it, keep the message.
                step_index = 0
                note_extra = " (first turn: the user message was kept; only its response was removed)"
            else:
                step_index = last_user - 1
        step_index = max(0, min(int(step_index), len(steps) - 1))

        result["revertedToStepIndex"] = step_index
        if not conversation_only:
            result["fileRollback"] = self.client.revert_preview(conversation_id, step_index)

        try:
            self.client.revert_to_step(conversation_id, step_index,
                                       conversation_only=conversation_only,
                                       model_enum=enum)
        except RpcError as e:
            result["error"] = str(e)
            return result

        remaining = self.client.get_steps(conversation_id)
        result["status"] = "rewound"
        result["remainingSteps"] = len(remaining)
        result["messages"] = _summarize(remaining)
        result["note"] = (
            f"Conversation rewound to step {step_index}{note_extra}. The model no longer "
            "remembers anything after it. Send the next prompt with openagy_ask "
            "(same conversation_id) — e.g. a differently-phrased question after a "
            "filtered turn, rather than stacking retries."
        )
        return result

    def reask(self, conversation_id: str, prompt: str,
              raw: bool = False, keep_message: bool = False,
              conversation_only: bool = True,
              inactivity_timeout: float = 240.0) -> dict[str, Any]:
        """Rewind the last turn and send a rephrased prompt in one step.

        The single-call 'retry differently' primitive for filtered/failed
        turns: undo the last full turn (Gemini genuinely forgets it), then
        send the new prompt and wait for the reply.
        """
        if _ROLLBACK_RE.search(prompt):
            return {
                "conversationId": conversation_id,
                "status": "rejected_rollback_request",
                "error": ("This prompt reads like a rollback request, but Gemini cannot "
                          "rewind or forget by itself."),
                "note": ("Nothing was sent. Pass the actual rephrased prompt to "
                         "openagy_reask — the rewind happens automatically before it."),
            }
        undo_result = self.undo(conversation_id, keep_message=keep_message,
                                conversation_only=conversation_only)
        if "error" in undo_result:
            return undo_result

        result: dict[str, Any] = {
            "conversationId": conversation_id,
            "rewoundToStepIndex": undo_result.get("revertedToStepIndex"),
            "sanitized": not raw,
        }
        registry = _load_registry()
        model_id = registry.get(conversation_id) or self.client.default_model_id()
        text = prompt if raw else sanitize_light(prompt)
        try:
            self.client.send_message(conversation_id, text, model_id=model_id)
        except RpcError as e:
            return {
                "conversationId": conversation_id,
                "status": "send_error",
                "error": str(e),
                "rewoundToStepIndex": undo_result.get("revertedToStepIndex"),
                "note": "The turn was rewound, but sending the new prompt failed; retry with openagy_ask.",
            }
        return self._finish_turn(conversation_id, result, 0, inactivity_timeout)
