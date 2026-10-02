# openagy — Antigravity control reference for agents

Everything an agent needs to drive Google Antigravity headlessly: create
conversations, send prompts, read replies, manage projects and visibility.
All wire details below were verified live against Antigravity 2.17.0-2.19.1
(2026-09-28). Code lives in this directory; run `python -m openagy --help`
or use the `openagy_*` MCP tools from opencode.

**Distribution**: pip package (`pip install openagy`, updates with
`pip install -U openagy`); opencode registration via `openagy-install`.
The MCP `status` tool reports the running version.

## Layout

| Path | What |
|---|---|
| `openagy/discover.py` | find the live language server (port + CSRF token) |
| `openagy/client.py` | Connect-JSON RPC client (`AntigravityClient`) |
| `openagy/api.py` | high-level `Openagy.ask/read/list/delete/status` |
| `openagy/sanitize.py` | rules-based prompt sanitizer |
| `openagy/transcripts.py` | conversation listing + step distillation |
| `openagy/cli.py` | manual testing CLI (`python -m openagy <cmd>`) |
| `openagy/mcp_server.py` | MCP stdio server (registered in opencode.jsonc) |
| `openagy/protos.py` | regenerate the wire-schema descriptor reference **from the user's own install** (`python -m openagy.protos`) — nothing Google-derived ships in the repo |
| `~/.openagy/conversations.json` | openagy-created conversation → model map |

Requires: Antigravity app running and logged in (it spawns
`language_server.exe`); Python ≥3.10 with `mcp` + `psutil`.

## Architecture (one screen)

```
OpenCode ──MCP──> openagy
                   │ 1. psutil: find language_server.exe --standalone
                   │ 2. its 127.0.0.1 TCP listen ports
                   │ 3. HTTPS GET / (self-signed cert) → window.__APP_CONFIG__
                   │    → { csrfToken, appVersion }
                   ▼
   POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/<Method>
   Content-Type: application/json        (unary)
   Content-Type: application/connect+json (server-streaming, framed)
   Header: x-codeium-csrf-token: <token>
```

Port and CSRF token are per-app-session (change on every app restart) —
always re-discover; `openagy.discover.discover()` caches 30 s. The hub UI
is served by the language server itself; `window.__APP_CONFIG__` in the
index HTML contains the CSRF token, so no process command-line parsing is
strictly needed once you know the port.

## Wire protocol details

- Unary: `POST` with JSON body, proto JSON mapping → field names in
  lowerCamelCase (`cascadeId`), enums by name string
  (`"MODEL_PLACEHOLDER_M318"`, `"CORTEX_STEP_TYPE_USER_INPUT"`).
- Server-streaming (`JetboxSubscribeToSummaries`, `ProjectUpdatesStream`):
  request body must be Connect-enveloped: 1 flag byte `0x00` + 4-byte
  big-endian length + JSON. Response is a stream of the same envelopes;
  flag `0x02` marks the JSON trailer frame. Headers:
  `Content-Type: application/connect+json`,
  `Connect-Protocol-Version: 1`.
- TLS: self-signed cert — disable verification (context with
  `check_hostname=False`, `verify_mode=CERT_NONE`).
- Errors come back as Connect error JSON:
  `{"code":"invalid_argument","message":"..."}` with HTTP 4xx.
- Stale token/port after an app restart: re-run discovery and retry once
  (`client.rpc()` already does this).

## RPC cookbook (all verified live)

All bodies are JSON; service path is
`exa.language_server_pb.LanguageServerService`. Timestamps are RFC 3339 UTC
with `Z` (e.g. `2026-09-28T23:11:24.787055200Z`).

### GetAuthStatus — health check
```json
{}   →  {"authResult":{"hasValidAuth":true,"grantedScopes":[...]}}
```

### GetAvailableModels — model registry
```json
{"metadata":{"ideName":"antigravity"}}
 →  {"response":{"models":{"<id>":{...,"model":"MODEL_PLACEHOLDER_M318"},...},
                 "defaultAgentModelId":"gemini-3.8-flash-high"}}
```
Model `id`s (e.g. `gemini-3.8-flash-high`, user-added `custom-*`) map to
`Model` enum names via the `model` field. Placeholder enums
(`MODEL_PLACEHOLDER_M0..M650`) are how newer/custom models are addressed.

### GetWorkspaceInfos — home dirs
```json
{} → {"homeDirPath":"C:/Users/owens","homeDirUri":"file:///C:/Users/owens",
       "geminiDirUri":"file:///C:/Users/owens/.gemini"}
```

### RetrieveUserQuotaSummary — usage limits
```json
{"request":{"project":""},"forceRefresh":true}
 → {"response":{"groups":[{"displayName":"Gemini Models","buckets":[
      {"bucketId":"gemini-weekly","window":"weekly",
       "remainingFraction":0.988,"resetTime":"2026-10-09T17:39:50Z",
       "description":"..."}]}]}}
```
Per-model-group quota: weekly and rolling 5-hour buckets with remaining
fraction and reset times. Exposed as the `quota` MCP tool (adds
`remainingPercent` and a `lowestRemainingPercent` rollup).

### ReadProjects — fetch projects by explicit ids
```json
{"ids":["<uuid>",...]}
 → {"projects":[{"id":"...","name":"radiacode","projectResources":{"resources":
      [{"folderUri":"file:///c%3A/Users/owens/radiacode"}]},"settings":{},
      "isWorkspaceOnly":false}]}
```
Empty `ids` returns nothing — the UI sources ids from the local registry
files (see below), not from a list RPC.

### CreateProject — GUI's "pseudo-project" flow
```json
{"project":{"id":"<new uuid>","name":"<dir name>",
  "projectResources":{"resources":[{"folderUri":"file:///c%3A/Users/owens/<dir>"}]},
  "isWorkspaceOnly":false,"settings":{}}}
```
409 `already_exists` if the name is taken. Registry is persisted at
`~/.gemini/config/projects/<project-uuid>.json` — read this directory to
list all projects without RPCs (openagy does this).

### UpdateProject — repair/edit a project
Same body shape as CreateProject. Used to fix a malformed `folderUri`
(`file%3A///c:/...` → `file:///c%3A/...`) that made StartCascade fail with
`unknown uri scheme`. Id must exist.

### StartCascade — create a conversation
```json
{"cascadeId":"<new uuid>","source":1,
 "projectEnvConfig":{"projectId":"<uuid>","defaultProjectEnvironment":{}}},
 "metadata":{"ideName":"antigravity","ideVersion":"2.17.0"}}
```
or, for standalone (no project): `"workspaceUris":["file:///c:/Users/..."]`
instead of `projectEnvConfig`. **Never both** — HTTP 400
`cannot specify both workspace URIs and project environment config`.
`source`: `1` = CASCADE_CLIENT (what the UI uses), `15` = SDK.
Response: `{"cascadeId":..., "projectEnvInfo":{"environmentId":...}}` when
project-bound. Prefer project-bound — see sidebar visibility rules.

### SendUserCascadeMessage — send a user turn
```json
{"cascadeId":"<id>","items":[{"text":"..."}],
 "cascadeConfig":{"plannerConfig":{"planModel":"MODEL_PLACEHOLDER_M318",
   "knowledgeConfig":{"enabled":false}},
   "conversationHistoryConfig":{"enabled":false}}}
```
`planModel` is **required on every turn** (including follow-ups) or the
executor fails with `plan model not specified`. For user-added custom
models also pass `customModelInfoOverride` = the model entry from
GetAvailableModels. Attachments: extra items with `item`/`file` scope
(`exa.codeium_common_pb.TextOrScopeItem`). Optional `blocking:true`.

### WaitForConversationFullyIdle — wait for the agent to finish
```json
{"conversationId":"<id>","inactivityTimeoutSeconds":240,
 "stabilizationDurationSeconds":3,"returnOnExecutorError":true}
```
Returns `{}` when idle. HTTP timeout must exceed the two values. If the
conversation is waiting on a user question/permission, this returns while
the transcript ends in an `askQuestion` step — read steps to detect.

### GetCascadeTrajectorySteps — read the transcript
```json
{"cascadeId":"<id>","stepOffset":0}
```
Steps have `type` (`CORTEX_STEP_TYPE_*`), `status`, `metadata.createdAt`,
and a type-specific payload: `userInput.items[].text`,
`plannerResponse.response` (model text; also `thinking`, `toolCalls[]`
with `name`), `askQuestion.questions[].question`, `errorMessage.shortError`.
`openagy.transcripts.summarize_steps()` distills this into
`[{index, role: user|model|question|error|tool, text}]`.

### GetConversationMetadata
```json
{"conversationId":"<id>"} → {"metadata":{"workspaces":[...],"projectId":...}}
```

### JetboxWriteSummary — register conversation in the sidebar store
Required after StartCascade or the conversation is invisible in the UI:
```json
{"cascadeId":"<id>","summary":{
  "summary":"<initial prompt text>",
  "workspaces":[{"workspaceFolderAbsoluteUri":"file:///c:/Users/owens/<dir>"}],
  "lastUserInputTime":"<RFC3339>","createdTime":"<RFC3339>",
  "trajectoryMetadata":{"projectId":"<uuid>","workspaceUris":["file:///c%3A/..."]}}}
```
The LS later rewrites title/status/step_count from the trajectory itself.

### JetboxSubscribeToSummaries — live sidebar feed (streaming)
Connect-streaming (see framing above). Initial snapshot then updates:
`{"updates":{"<cascadeId>":{...CascadeTrajectorySummary...}},"deletes":[...]}`.
`openagy` reads the equivalent persisted store directly from
`~/.gemini/antigravity/agyhub_summaries_proto.pb`
(`exa.jetski_cortex_pb.CascadeTrajectorySummaries`, a map).

### HandleCascadeUserInteraction — answer questions & approvals
When Gemini waits on the user, the pending step has status
`CORTEX_STEP_STATUS_WAITING`. Answer it (never resend the prompt):
```json
{"cascadeId":"<id>","interaction":{
  "trajectoryId":"<from step metadata.sourceTrajectoryStepInfo>",
  "stepIndex":<int>,
  "askQuestion":{"responses":[{"selectedOptionIds":["1"]}],
                 "cancelled":false}}}
```
The `interaction` oneof cases: `askQuestion` (responses: entries with
`selectedOptionIds` / `writeInResponse` / `skipped`, plus `cancelled`),
confirm-style `{confirm: bool}` (`runCommand`, `openBrowserUrl`, `mcp`,
`sendCommandInput`, `browserAction`, `executeBrowserJavaScript`,
`captureBrowserScreenshot`, `clickBrowserPixel`, `readUrlContent`,
`runExtensionCode`, `deploy`, `approvalInteraction`), and
`permission`/`filePermission` (`{allow, scope, [absolutePathUri]}` with
`PERMISSION_SCOPE_ONCE|_CONVERSATION|_WORKSPACE|_GLOBAL|_PROJECT`).
openagy auto-detects the pending step (`client.pending_interaction`) and
exposes it as `openagy_answer_question` / `openagy_approve`.

### RevertToCascadeStep — 'Undo to this point'
```json
{"cascadeId":"<id>","stepIndex":N,"conversationOnly":true,
 "overrideConfig":{"plannerConfig":{"planModel":"MODEL_PLACEHOLDER_M318",
   "knowledgeConfig":{"enabled":false}},"conversationHistoryConfig":{"enabled":false}}}
```
Keeps steps 0..N (inclusive) and truncates the rest; the model's context is
rebuilt — excised replies are genuinely forgotten (verified live). The
`overrideConfig` with a plan model is REQUIRED (500 'plan model not
specified' without it). `conversationOnly=true` leaves workspace files
untouched; `false` also rolls back file edits (preview with
`GetRevertPreview {cascadeId, stepIndex}` → codeEditPreviews).
openagy exposes this as `openagy_undo(conversation_id, step_index?,
keep_message?, conversation_only?)` — default removes the last full turn.

### JetboxDeleteSummary + DeleteCascadeTrajectory — delete a conversation
```json
{"cascadeId":"<id>"}   (call both)
```

## Sidebar visibility rules (the three-part recipe)

A conversation shows in the Antigravity sidebar iff ALL of:
1. It was registered via `JetboxWriteSummary` (StartCascade alone leaves it
   invisible — the LS only indexes it for search), AND
2. It is **project-bound** (`projectEnvConfig` at StartCascade, non-empty
   `trajectoryMetadata.projectId`), OR the
   `enableStandaloneConversations` feature flag is enabled (default OFF —
   project-less conversations go to a hidden "Standalone Conversations"
   section), AND
3. (Trivially) its summary isn't archived/deleted.

Project lookup: `openagy.AntigravityClient.ensure_project(workspace_dir)`
matches against `~/.gemini/config/projects/*.json` (tolerant URI compare,
drive-letter case and percent-encoding ignored) or creates a project
exactly like the GUI. This mirrors `irb`/`mX` in the hub bundle.

## Formats & conventions

- **Workspace URIs**: canonical `workspaces[]` form `file:///c:/Users/...`
  (lowercase drive, plain colon); `trajectoryMetadata.workspaceUris` uses
  the percent-encoded form `file:///c%3A/Users/...`. Match tolerantly.
- **Timestamps**: RFC 3339 UTC, `...Z` suffix; nanosecond precision ok.
- Model enum names, not numbers, in JSON.
- Conversation/cascade/trajectory ids are UUID v4 strings; the cascade id
  doubles as the "conversation id" in most RPCs.

## Common errors → causes

| Error | Cause / fix |
|---|---|
| `plan model not specified` | `SendUserCascadeMessage` lacked `cascadeConfig.plannerConfig.planModel` (needed on every turn) |
| `cannot specify both workspace URIs and project environment config` | Drop `workspaceUris` when passing `projectEnvConfig` |
| `unknown uri scheme` (workspace resolution) | Project `folderUri` malformed — fix via `UpdateProject` |
| HTTP 409 `already_exists` (CreateProject) | Project name taken — look it up in `~/.gemini/config/projects/` instead |
| HTTP 401/403 or connection refused | App restarted, port/CSRF rotated — re-discover |
| Conversation invisible in UI | Missing `JetboxWriteSummary`, or standalone without the flag — see visibility rules |

## Storage map (read-only unless noted)

```
~/.gemini/
  antigravity/
    agyhub_summaries_proto.pb        sidebar summary store (map<uuid, CascadeTrajectorySummary>)
    conversation_summaries.db        SQLite search index (title/preview/project_id)
    conversations/<uuid>.db         per-conversation SQLite, protobuf step blobs
    custom_models.json              user-added models (antigravity-add-model patch)
  config/projects/<uuid>.json       project registry (read/write via LS RPCs)
~/.openagy/conversations.json       openagy's conversation→model memory
```

## Using openagy

- **opencode MCP tools**: `openagy_status`, `openagy_list_models`,
  `openagy_quota`, `openagy_list_conversations`, `openagy_ask(prompt,
  workspace?, model?, conversation_id?, raw?, inactivity_timeout?)`,
  `openagy_wait(conversation_id, timeout_seconds?, include_tools?)`,
  `openagy_answer_question(conversation_id, answers?, cancelled?)`,
  `openagy_approve(conversation_id, allow?, scope?)`, `openagy_undo(
  conversation_id, step_index?, keep_message?, conversation_only?)`,
  `openagy_reask(conversation_id, prompt, keep_message?)`,
  `openagy_read_conversation(id, include_tools?)`,
  `openagy_delete_conversation(id)`. Prompts are sanitized
  by default (see sanitize.py; `raw: true` to bypass).
- **CLI**: `python -m openagy status|models|quota|list|ask|wait|answer|
  approve|undo|reask|read|delete` (from this directory).
- **Python**: `from openagy.api import Openagy; Openagy().ask(prompt, workspace=...)`
  returns `{conversationId, status, reply, messages, error?}`.
- Follow-ups: pass the same `conversationId`; openagy reuses the recorded
  model. Continue in the GUI anytime — it's the same store.

## Undo / rewind (filtered or failed turns)

When a turn gets content-filtered or goes sideways, **rewind before re-asking**
— `openagy_undo(conversation_id)` removes the last full turn (user message +
response) and Gemini genuinely forgets it. Then send a differently-phrased
prompt with `openagy_ask`. Stacking rephrased retries on top of a filtered
turn keeps the flagged content in context and tips the filters off further;
rewinding excises it.

- `openagy_reask(conversation_id, <rephrased prompt>)` does both in one
  call: rewinds the last turn, then sends the new prompt and waits.
- `keep_message=true` keeps the last user message and removes only its
  response (regenerate).
- `step_index=N` keeps steps 0..N (explicit rewind point).
- `conversation_only=false` also rolls back workspace file edits (the result
  includes the revert preview of affected files).

**Never send a chat message asking Gemini to rewind/forget** — Gemini cannot
do it itself, and the meta-message keeps the filtered content in play.
openagy intercepts rollback-style follow-ups (`[ROLLBACK_REQUEST]`, "rewind
and forget", "treat this as a signal"...) and rejects them with
`status: rejected_rollback_request` before they are sent.

## Answering questions & approvals (pending interactions)

When Gemini needs user input mid-turn, the pending step sits in
`CORTEX_STEP_STATUS_WAITING` and `WaitForConversationFullyIdle` BLOCKS until
it is answered — so openagy detects it separately: `openagy_ask` and
`openagy_wait` return early with `status: "waiting_for_interaction"` plus an
`interaction` descriptor (questions with option ids, command, url...).

- **Questions** (`kind: "askQuestion"`): answer with
  `openagy_answer_question(conversation_id, answers=[...])` — one answer
  object per pending question, by position:
  `{"selectedOptionIds": ["<id>"]}` (ids from the interaction's options),
  `{"writeInResponse": "free text"}`, or `{"skipped": true}`;
  `cancelled=true` dismisses. The tool then waits for Gemini's continuation
  and returns the reply.
- **Confirmations/permissions** (`kind` of `runCommand`, `openBrowserUrl`,
  `mcp`, `filePermission`, `permission`, `browserAction`, ...): answer with
  `openagy_approve(conversation_id, allow, scope?)`.
- **Never** resend the prompt with `openagy_ask` to answer a pending
  interaction — that starts a new turn and leaves the question dangling.

## Long-running tasks & timeouts

`WaitForConversationFullyIdle` blocks until the conversation is truly done —
verified live (it does NOT return early at `inactivityTimeoutSeconds` while
the agent is still working; treat that value as a wait budget/cap). Consequences:

- An MCP tool call that waits can outlive opencode's client timeout (MCP
  error `-32001`). **A timeout does not stop the Antigravity conversation** —
  only the caller gave up. opencode.jsonc sets `mcp.openagy.timeout` to
  1,800,000 ms (30 min) for this reason.
- Recovery pattern for agents: on `-32001` or a `waitError`, call
  `openagy_wait(conversation_id)` (bounded, resumable — returns
  `status: running` with partial progress, call again to keep waiting) or
  `openagy_read_conversation` (instant `status`/`idle` fields). **Never
  resend the prompt** — that starts a new turn in the same conversation.
- `openagy_ask` accepts `inactivity_timeout` up to ~1700s; keep it under the
  MCP timeout. For anything longer, ask with a modest timeout and babysit
  with `openagy_wait`.

## Attaching to existing conversations (retries, follow-ups)

**Always retry or continue in the existing conversation — never recreate.**

- `openagy_ask(conversation_id=<id>, prompt=...)` attaches to any existing
  conversation: follow-ups, retries after errors, resuming after the user
  killed/stopped it in the GUI (a killed conversation is still attachable,
  same as the Antigravity UI's retry), or continuing a conversation another
  agent/user started (find ids with `openagy_list_conversations`).
- Every openagy result — including error payloads — carries
  `conversationId`; error results also include a `note` telling you to retry
  in place.
- Only omit `conversation_id` to start a genuinely new conversation.
- After an Antigravity-side error (`errorMessage` step), resending in the
  same conversation is the correct retry path, mirroring the UI.
