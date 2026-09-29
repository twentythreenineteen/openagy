"""Read conversation state: summaries DB listing + trajectory step parsing."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

SUMMARIES_DB = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"


def conversation_state(conversation_id: str, db_path: Path | None = None) -> dict[str, Any]:
    """Return {'status': CASCADE_RUN_STATUS_*|None, 'killed': bool} for a
    conversation from the summaries index. Cheap; no RPC needed."""
    db = db_path or SUMMARIES_DB
    if not db.exists():
        return {"status": None, "killed": False}
    uri = f"file:{db.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        row = con.execute(
            "SELECT status, killed FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return {"status": None, "killed": False}
        return {"status": row[0], "killed": bool(row[1])}
    finally:
        con.close()


def conversation_status(conversation_id: str, db_path: Path | None = None) -> str | None:
    """Back-compat wrapper: the run status string only."""
    return conversation_state(conversation_id, db_path)["status"]


def is_idle(conversation_id: str) -> bool:
    """True when the conversation is not actively working.

    Unknown status counts as idle so agents don't wait forever on
    conversations that were deleted or never registered. A killed
    conversation (stopped via the GUI) also counts as idle — it can be
    resumed by sending a new message, exactly like in the Antigravity UI.
    """
    state = conversation_state(conversation_id)
    return state["killed"] or state["status"] in (
        None, "CASCADE_RUN_STATUS_IDLE", "CASCADE_RUN_STATUS_UNSPECIFIED",
    )


def list_conversations(limit: int = 25, db_path: Path | None = None) -> list[dict[str, Any]]:
    """List conversations newest-first from the summaries SQLite index (read-only).

    This mirrors what the Antigravity sidebar shows and does not need an RPC
    round-trip. Falls back to an empty list if the DB does not exist yet.
    """
    db = db_path or SUMMARIES_DB
    if not db.exists():
        return []
    uri = f"file:{db.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT conversation_id, title, preview, last_modified_time, "
            "workspace_uris, status, killed FROM conversation_summaries "
            "ORDER BY last_modified_time DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            ws = json.loads(r["workspace_uris"]) if r["workspace_uris"] else []
        except json.JSONDecodeError:
            ws = []
        out.append({
            "conversationId": r["conversation_id"],
            "title": r["title"],
            "preview": (r["preview"] or "")[:200],
            "lastModified": r["last_modified_time"],
            "workspaces": ws,
            "status": r["status"],
            "killed": bool(r["killed"]),
        })
    return out


def summarize_steps(steps: list[dict[str, Any]], include_tools: bool = False,
                    max_chars: int = 60000) -> list[dict[str, Any]]:
    """Distill GetCascadeTrajectorySteps output into a compact transcript.

    Keeps user inputs, model (planner) responses, questions asked of the user,
    and errors; optionally includes tool-call summaries.
    """
    out: list[dict[str, Any]] = []
    for i, s in enumerate(steps):
        step = s.get("step", s)
        entry: dict[str, Any] = {"index": s.get("stepIndex", i)}
        stype = s.get("type", "")

        if "userInput" in step:
            texts = [it.get("text", "") for it in step["userInput"].get("items", []) if "text" in it]
            if texts:
                entry["role"] = "user"
                entry["text"] = "\n".join(texts)
        elif "plannerResponse" in step:
            pr = step["plannerResponse"]
            entry["role"] = "model"
            entry["text"] = pr.get("response", "")
            if pr.get("thinking"):
                entry["thinking"] = pr.get("thinking", "")
            if pr.get("toolCalls"):
                entry["toolCalls"] = [
                    (tc.get("name") or tc.get("function", {}).get("name") or str(tc))
                    for tc in pr["toolCalls"]
                ]
        elif "askQuestion" in step:
            qs = step["askQuestion"].get("questions", [])
            entry["role"] = "question"
            entry["text"] = "\n".join(q.get("question", str(q)) for q in qs)
        elif "errorMessage" in step:
            entry["role"] = "error"
            entry["text"] = step["errorMessage"].get("shortError") or \
                step["errorMessage"].get("error", {}).get("shortError", "unknown error")
        elif include_tools and stype.startswith("CORTEX_STEP_TYPE_") and not stype.endswith(
                ("USER_INPUT", "PLANNER_RESPONSE", "ERROR_MESSAGE")):
            entry["role"] = "tool"
            entry["text"] = _tool_summary(step, stype)

        if entry.get("role") and entry.get("text"):
            out.append(entry)

    # Global size guard: truncate the longest entries first.
    total = 0
    for e in out:
        total += len(e.get("text", ""))
        if total > max_chars:
            e["text"] = e["text"][: max(0, max_chars - (total - len(e["text"])))] + " …[truncated]"
            total = max_chars
    return out


def _tool_summary(step: dict[str, Any], stype: str) -> str:
    kind = stype.replace("CORTEX_STEP_TYPE_", "").lower()
    for key in ("viewFile", "runCommand", "grepSearch", "findFile", "fileChange",
                "writeToFile", "listDirectory", "openBrowserUrl", "invokeSubagent"):
        if key in step:
            obj = step[key]
            label = obj.get("command") or obj.get("absolutePathUri") or obj.get("query") or \
                obj.get("directoryPathUri") or obj.get("url") or obj.get("prompt") or \
                obj.get("targetFileUri") or ""
            return f"[{kind}] {label}"[:300]
    return f"[{kind}]"
