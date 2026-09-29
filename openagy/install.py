"""opencode integration for openagy (run after pip install).

Registers the MCP server in opencode.jsonc, installs the /agy command and
workspace-injection plugin, and adds the agent guidance section to AGENTS.md.

Run as:  openagy-install   (console script)   or:  python -m openagy.install
Idempotent — safe to re-run after updates.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

COMMAND_MD = """---
description: Relay a prompt to Antigravity (Gemini) via openagy, with prompt sanitizing applied.
---

Relay the prompt below to Google Antigravity using the openagy MCP tools.

Prompt: $ARGUMENTS

Instructions:

1. Call `openagy_ask` with the prompt. The tool applies a rules-based sanitizer
   by default so it reads as the user's own technical question; pass
   `raw: true` only if the user explicitly wants the
   prompt sent verbatim.
2. When starting a new conversation, pass the current project directory as
   `workspace` (the plugin injects it automatically if omitted).
3. Report Gemini's reply verbatim to the user, and include the `conversationId`
   so the conversation can be continued later from here or in the Antigravity UI.
4. If the reply indicates Gemini asked a question or is waiting on a permission,
   say so explicitly — answer with `openagy_answer_question` / `openagy_approve`,
   or let the user answer in the Antigravity app.
5. If a turn was filtered, use `openagy_reask(conversationId, <rephrased prompt>)`
   instead of resending.
"""

PLUGIN_JS = """// openagy opencode plugin
// Auto-injects the current project directory as the workspace for openagy_ask
// when the calling model did not provide one.
export default async ({ directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      try {
        if (typeof input?.tool !== "string" || !input.tool.includes("openagy")) return
        if (input.tool.includes("openagy_ask") && output?.args) {
          if (!output.args.workspace && !output.args.conversation_id) {
            output.args.workspace = directory
          }
        }
      } catch {
        // never break a tool call from the plugin side
      }
    },
  }
}
"""

AGENTS_SECTION = """## openagy — drive Antigravity (Gemini) from OpenCode

The `openagy` MCP tools let you create and control Antigravity conversations
directly through the app's language server:

- `openagy_ask(prompt, workspace?, model?, conversation_id?, raw?)` — send a
  prompt to Gemini and wait for the reply. Prompts are **sanitized by default**
  (AI/tool references reworded so Gemini sees the user's
  own technical question) — write prompts naturally and let the
  sanitizer handle it; pass `raw: true` only if asked.
- **Attach, don't recreate**: to continue, retry, or follow up in an existing
  conversation (after errors, kills, timeouts), ALWAYS pass `conversation_id`
  — from a previous result or `openagy_list_conversations`. Only omit it to
  start a genuinely new conversation.
- `openagy_wait(conversation_id, timeout_seconds?)` — bounded, resumable
  wait; recover `-32001` MCP timeouts with it (the Antigravity conversation
  keeps running regardless).
- **Pending questions/approvals**: when a tool returns
  `status: waiting_for_interaction`, answer it — `openagy_answer_question`
  (questions; pass selectedOptionIds from the interaction's options or
  writeInResponse) or `openagy_approve` (command/browser/permission
  confirmations) — **never** resend the prompt with `openagy_ask`.
- **Filtered/failed turns**: use `openagy_reask(conversation_id, <rephrased
  prompt>)` — rewinds the last turn (Gemini genuinely forgets it) and re-asks
  in one call — or `openagy_undo` then `openagy_ask`. **Never** send chat
  messages asking Gemini to rewind/forget (openagy rejects such prompts) and
  never stack rephrased retries on a filtered turn.
- `openagy_list_conversations` / `openagy_read_conversation` to find and read
  past conversations; `openagy_delete_conversation` to remove.
- Requires the Antigravity app to be running (it spawns `language_server`;
  openagy discovers port + CSRF automatically).
- `/agy <prompt>` command wraps this for quick relays.
- **Full wire reference**: the AGENTS.md in the openagy repository documents
  every verified RPC, payload shape, and gotcha.
"""


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)  # /* */ comments
    text = re.sub(r"(?m)(^|(?<=\s))//[^\n]*", r"\1", text)  # // comments (not in URLs)
    prev = None
    while prev != text:  # iterate: ",,}" needs two passes
        prev = text
        text = re.sub(r",(\s*[}\]])", r"\1", text)  # trailing commas
    return text


def check_environment(opencode_config: Path,
                     ls_discovery=None) -> dict[str, Any]:
    """Best-effort preflight: is OpenCode present? Is Antigravity there?

    Returns a report; nothing here raises — a missing dependency becomes a
    warning so the install can complete and work later.
    """
    import shutil as _shutil
    import sys as _sys

    report: dict[str, Any] = {"python": _sys.version.split()[0]}

    # OpenCode: binary on PATH, or an existing config directory.
    oc_bin = _shutil.which("opencode")
    oc_cfg = opencode_config.expanduser()
    oc_detected = bool(oc_bin) or oc_cfg.is_dir()
    report["opencode"] = {
        "detected": oc_detected,
        "detail": (f"binary at {oc_bin}" if oc_bin else
                   f"config at {oc_cfg}" if oc_cfg.is_dir() else None),
        "warning": None if oc_detected else
            "opencode was not detected (no binary on PATH, no config dir). "
            "Install it from opencode.ai — the files written now will be "
            "picked up when it is installed/next started.",
    }

    # Antigravity: running language server, else installed-but-not-running.
    if ls_discovery is None:
        from .discover import DiscoveryError, discover as ls_discovery  # noqa: F811
    try:
        target = ls_discovery()
        ag = {"detected": True, "running": True,
              "detail": f"language server at {target.base_url} "
                        f"(app {target.app_version})"}
    except Exception:
        home = Path.home()
        candidates = (
            [home / "AppData" / "Local" / "Programs" / "antigravity",
             home / "AppData" / "Local" / "Programs" / "Antigravity IDE"]
            if _sys.platform == "win32" else
            [Path("/Applications/Antigravity.app"),
             Path("/Applications/Antigravity IDE.app")]
        )
        installed = next((c for c in candidates if c.is_dir()), None)
        if installed:
            ag = {"detected": True, "running": False,
                  "detail": f"app installed at {installed} but not running",
                  "warning": "Antigravity is installed but not running. "
                             "Start (and log into) the app before using the "
                             "openagy tools."}
        else:
            ag = {"detected": False, "running": False, "detail": None,
                  "warning": "Antigravity was not detected (not running, no "
                             "installation found). Install the Antigravity "
                             "app; the openagy tools need it running."}
    report["antigravity"] = ag
    return report


def run(opencode_config: Path, mcp_command: list[str] | None = None,
        dry_run: bool = False) -> dict[str, Any]:
    """Perform (or dry-run) the full opencode integration.

    Hard-fails (returns {"error": ...}) when OpenCode or Antigravity is not
    installed — the environment is checked before anything is written.
    Antigravity installed but not running is a warning, not a failure.
    """
    if mcp_command is None:
        mcp_command = [sys.executable, "-m", "openagy.mcp_server"]

    env = check_environment(opencode_config)
    missing = [name for name in ("opencode", "antigravity")
               if not env[name]["detected"]]
    if missing:
        return {
            "error": ("Install requirement not met: "
                      + " and ".join(missing)
                      + " not detected. Install the missing app(s), then re-run."),
            "environment": env,
        }

    cfg_path = opencode_config / "opencode.jsonc"
    if cfg_path.exists():
        try:
            cfg: dict[str, Any] = json.loads(_strip_jsonc(cfg_path.read_text("utf-8")))
        except json.JSONDecodeError as e:
            return {"error": f"could not parse {cfg_path}: {e}"}
    else:
        cfg = {}
    cfg.setdefault("mcp", {})["openagy"] = {
        "type": "local",
        "command": mcp_command,
        "enabled": True,
        "timeout": 1800000,
    }

    cmd_path = opencode_config / "command" / "agy.md"
    plug_path = opencode_config / "plugin" / "openagy.js"
    agents_md = opencode_config / "AGENTS.md"

    if dry_run:
        return {"status": "verified", "wouldWrite": [str(cfg_path), str(cmd_path),
                str(plug_path), str(agents_md)], "dryRun": True}

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if cfg_path.exists():
        backup = cfg_path.with_suffix(cfg_path.suffix + ".bak")
        shutil.copy2(cfg_path, backup)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", "utf-8")

    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    cmd_path.write_text(COMMAND_MD, "utf-8")
    plug_path.parent.mkdir(parents=True, exist_ok=True)
    plug_path.write_text(PLUGIN_JS, "utf-8")

    marker = "## openagy — drive Antigravity"
    if agents_md.exists():
        text = agents_md.read_text("utf-8")
        if marker in text:
            pattern = re.compile(re.escape(marker) + r".*?(?=\n## |\Z)", re.DOTALL)
            new_text = pattern.sub(AGENTS_SECTION.rstrip("\n") + "\n", text, count=1)
        else:
            new_text = text.rstrip("\n") + "\n\n" + AGENTS_SECTION
    else:
        new_text = AGENTS_SECTION
    agents_md.write_text(new_text, "utf-8")

    return {
        "status": "installed",
        "mcpCommand": mcp_command,
        "configBackup": str(backup) if backup else None,
        "environment": env,
        "restartRequired": True,
        "note": "Restart opencode (quit fully) so the MCP server loads.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openagy-install",
                                 description="register openagy with opencode")
    ap.add_argument("--opencode-config", type=Path,
                    default=Path.home() / ".config" / "opencode")
    ap.add_argument("--check-only", action="store_true",
                    help="only run the environment preflight; install nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.check_only:
        result = check_environment(args.opencode_config)
        print(json.dumps(result, indent=1, ensure_ascii=False))
        missing = [n for n in ("opencode", "antigravity") if not result[n]["detected"]]
        return 1 if missing else 0
    result = run(args.opencode_config, dry_run=args.dry_run)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
