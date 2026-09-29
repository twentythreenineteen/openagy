# openagy

Control **Google Antigravity** from **OpenCode**: create conversations, send
prompts (with optional sanitizing), wait for replies, read and
delete conversations — all through Antigravity's own backend, so conversations
persist normally in the Antigravity sidebar.

Rigorously tested with [Abliterated Model Large V2](https://abliteration.ai)
([optional referral link](https://abliteration.ai/sign-up?referral_code=ref_HlZfEMeyIrsX)).

> **Agent-facing wire reference**: see [AGENTS.md](AGENTS.md) for the full
> RPC cookbook (payload shapes, streaming framing, sidebar-visibility rules,
> error table) — it is the document to read before extending openagy.

## Installation (Windows / macOS / Linux)

Prerequisites: **Python ≥ 3.10** on PATH, and the **Antigravity app**
installed and logged in (start it once before using the tools — openagy
drives the `language_server` process it spawns).

One command to install the package, one to register it with opencode —
standard pip, no wrapper scripts:

```bash
# from GitHub (once pushed):
python3 -m pip install git+https://github.com/twentythreenineteen/openagy

# or from PyPI (once published via a vX.Y.Z tag push):
python3 -m pip install openagy

# or from a checkout / extracted zip:
python3 -m pip install /path/to/openagy

# then, in all cases:
python3 -m openagy.install        # or: openagy-install
```

`openagy.install` pip-installs nothing itself — it registers the package
with opencode: the `openagy` MCP server (30-min tool timeout; existing
opencode.jsonc is backed up to `.bak`), the `/agy` command, the
workspace-injection plugin, and the agent guidance section in
`~/.config/opencode/AGENTS.md`. It **verifies both apps up front and exits
with an error if OpenCode or Antigravity is not installed** (Antigravity
installed-but-not-running is only a warning — start the app before using the
tools). Then **restart opencode** and try `/agy hello from openagy`.

**Updates**: `pip install -U ...` (same source as above); re-run
`python3 -m openagy.install` only if an update changes the integration
files. The MCP `status` tool reports the running version.

**Publishing** (maintainer): push a `v*` tag — `.github/workflows/pypi.yml`
builds and publishes to PyPI via trusted publishing (enable the repo as a
trusted publisher at pypi.org once).

Platform notes: everything (discovery via psutil, `~/.gemini` stores,
workspace URIs, TLS handling) is platform-agnostic and Antigravity keeps its
data under `~/.gemini` on macOS too. Fully tested on Windows; macOS is
best-effort — if discovery fails there, check that `python3 -c "import
psutil"` works and that Antigravity is running.

Works by talking directly to the **language server** (`language_server.exe`,
the binary the Antigravity app spawns) over its Connect-JSON API. No UI
automation, no Electron patching required (works with or without the
`antigravity-add-model` patch).

## Architecture

```
OpenCode ── MCP (stdio) ──> openagy.mcp_server
                              │  discover.py: psutil → language_server.exe →
                              │  listening ports → GET / → __APP_CONFIG__.csrfToken
                              ▼
            https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/<Method>
                              (POST, JSON, header x-codeium-csrf-token)
                              │
                              ▼
                     Antigravity language server ──> Gemini
                              │
                              ▼
            ~/.gemini/antigravity/conversations/*.db  (persistent, user-visible)
```

### RPCs used

| RPC | Purpose |
|---|---|
| `StartCascade` | create a conversation bound to a workspace |
| `JetboxWriteSummary` | register the conversation in the hub summary store **so it appears in the sidebar** (frontend does this on create; the LS does not) |
| `SendUserCascadeMessage` | send a user message (items: `[{text: ...}]`) |
| `WaitForConversationFullyIdle` | block until the agent stops producing output |
| `GetCascadeTrajectorySteps` | read back the transcript (user/model/tool steps) |
| `GetAvailableModels` | model ids → enum (`MODEL_PLACEHOLDER_M*`) mapping |
| `CreateProject` / `UpdateProject` | pseudo-project flow for sidebar-visible conversations |
| `JetboxDeleteSummary` + `DeleteCascadeTrajectory` | delete a conversation |

**No Google-derived code ships in the repo.** The wire-schema descriptors
(90 `FileDescriptorProto`s, incl. `language_server.proto`) are reference
material only - regenerate them from your own install anytime with
`python -m openagy.protos` (needs the app running).

## Tools exposed (MCP)

- `openagy_status()` — port, app version, default model
- `openagy_list_models()` — model ids (`gemini-3.8-flash-high`, `custom-*`)
- `openagy_list_conversations(limit)` — recent conversations
- `openagy_ask(prompt, workspace?, model?, conversation_id?, raw?, inactivity_timeout?)`
  — send + wait + return the reply and transcript tail
- `openagy_wait(conversation_id, timeout_seconds?, include_tools?)`
  — bounded, resumable wait; returns `status: idle|running` + partial
  progress (call again to keep waiting). Use to recover from MCP `-32001`
  timeouts — the Antigravity conversation keeps running either way.
- `openagy_answer_question(conversation_id, answers?, cancelled?)`
  — answer a pending question (`status: waiting_for_interaction`,
  kind `askQuestion`): option ids or free text, one per question.
- `openagy_approve(conversation_id, allow?, scope?)`
  — approve/deny pending confirmations (runCommand, browser, MCP, file
  permission...), auto-detected.
- `openagy_undo(conversation_id, step_index?, keep_message?, conversation_only?)`
  — 'Undo to this point': rewinds the conversation (default: removes the
  last full turn; the model genuinely forgets it). Use before re-asking
  after a filtered/failed turn instead of stacking rephrased retries.
- `openagy_reask(conversation_id, prompt, keep_message?)`
  — rewind + re-ask in one call (the retry primitive for filtered turns).
  Rollback-style meta-prompts ("rewind and forget...") are intercepted and
  rejected before they reach Gemini.
- `openagy_read_conversation(conversation_id, include_tools?)`
  — transcript + live `status`/`idle` fields
- `openagy_delete_conversation(conversation_id)`

opencode.jsonc sets `mcp.openagy.timeout` to 1,800,000 ms (30 min) so
lengthy tasks survive a single `openagy_ask` call.

## Sanitizer (prompt sanitizing)

`openagy.sanitize` applies deterministic, grammar-safe substitutions
(`OpenCode` → "my terminal assistant", "the user" → "the person asking",
third-party model references → "another assistant/model", etc.) plus a
first-person wrapper, so Gemini sees the user's own technical question instead
of an AI-relayed task. New conversations get the full wrapper
(`sanitize`), follow-ups get substitutions only (`sanitize_light`). Pass
`raw=True` to `openagy_ask` to bypass.

## CLI (manual testing)

```
python -m openagy status
python -m openagy models
python -m openagy list --limit 10
python -m openagy ask "..." --workspace C:\some\project [--model gemini-3.8-flash-high]
python -m openagy ask "..." --conversation <id>          # follow-up
python -m openagy read <id> [--tools]
python -m openagy delete <id>
```

## OpenCode integration (already configured)

- MCP server registered in `~/.config/opencode/opencode.jsonc` (`mcp.openagy`,
  local, stdio; PYTHONPATH points here).
- Plugin `~/.config/opencode/plugin/openagy.js` auto-injects the current
  project directory as `workspace` for `openagy_ask`.
- Command `/agy <prompt>` (`~/.config/opencode/command/agy.md`) relays a
  prompt through openagy and reports Gemini's reply.

Restart opencode after changing config.

## Requirements

- Python ≥ 3.10 with `mcp` (FastMCP) and `psutil`
- A running, logged-in Antigravity app (it spawns `language_server.exe`)
- Windows (discovery uses psutil on Windows paths; the RPC layer is portable)
