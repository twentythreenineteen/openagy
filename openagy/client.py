"""Connect-JSON RPC client for the Antigravity language server.

Wire format (verified against the live app):
  POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/<Method>
  Content-Type: application/json
  x-codeium-csrf-token: <token>

Conversations ("cascades") are created with StartCascade, driven with
SendUserCascadeMessage, awaited with WaitForConversationFullyIdle and read back
with GetCascadeTrajectorySteps.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from .discover import DiscoveryError, Target, discover, invalidate_cache

SERVICE = "exa.language_server_pb.LanguageServerService"

# Trajectory source values (wire enum)
SOURCE_CASCADE_CLIENT = 1
SOURCE_SDK = 15


class RpcError(RuntimeError):
    def __init__(self, method: str, status: int | None, message: str):
        super().__init__(f"{method} failed (HTTP {status}): {message}")
        self.method = method
        self.status = status
        self.detail = message


class AntigravityClient:
    """Thin JSON-RPC-over-Connect client to a live language server."""

    def __init__(self, target: Target | None = None):
        self._target = target

    @property
    def target(self) -> Target:
        if self._target is None:
            self._target = discover()
        return self._target

    # ------------------------------------------------------------------ core
    def rpc(self, method: str, body: dict[str, Any] | None = None,
            timeout: float = 120.0) -> dict[str, Any]:
        """Call a unary LanguageServerService method with one retry on stale auth."""
        try:
            return self._rpc_once(method, body or {}, timeout)
        except (RpcError, DiscoveryError, urllib.error.URLError, OSError):
            # Port/token may have rotated (app restart). Re-discover once.
            invalidate_cache()
            self._target = None
            return self._rpc_once(method, body or {}, timeout)

    def _rpc_once(self, method: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        import json
        import ssl

        url = f"{self.target.base_url}/{SERVICE}/{method}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": self.target.csrf_token,
            "User-Agent": "openagy",
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RpcError(method, e.code, detail) from None
        except urllib.error.URLError as e:
            raise RpcError(method, None, str(e)) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}

    # ------------------------------------------------------------ utilities
    @staticmethod
    def workspace_uri(path: str) -> str:
        """Convert a Windows/posix path to the URI form Antigravity expects.

        Canonical hub form: lowercase drive letter, plain colon
        (e.g. file:///c:/Users/me/proj); trajectoryMetadata.workspaceUris
        uses the percent-encoded variant (file:///c%3A/...).
        """
        path = path.replace("\\", "/")
        if len(path) >= 2 and path[1] == ":":
            path = path[0].lower() + path[1:]
        if not path.startswith("/"):
            path = "/" + path
        return f"file://{path}"

    @staticmethod
    def workspace_uri_encoded(path: str) -> str:
        return AntigravityClient.workspace_uri(path).replace(":", "%3A", 1)

    # ------------------------------------------------------------- projects
    def get_quota(self, force_refresh: bool = True) -> dict[str, Any]:
        """RetrieveUserQuotaSummary — usage limits per model group.

        Returns groups (e.g. 'Gemini Models') with buckets: weekly and
        rolling 5-hour limits, remaining fraction, and reset times.
        """
        resp = self.rpc("RetrieveUserQuotaSummary", {
            "request": {"project": ""}, "forceRefresh": bool(force_refresh),
        }, timeout=60.0)
        return resp.get("response", resp)

    def list_projects(self) -> list[dict[str, Any]]:
        """List all known projects (id, name, folder URIs) from the local
        registry at ~/.gemini/config/projects/*.json (complete and fast; the
        ReadProjects RPC needs explicit ids, which the UI also sources from
        these files).
        """
        import json as _json
        from pathlib import Path

        projects_dir = Path.home() / ".gemini" / "config" / "projects"
        projects: list[dict[str, Any]] = []
        if not projects_dir.is_dir():
            return projects
        for f in sorted(projects_dir.glob("*.json")):
            try:
                p = _json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            uris = [
                r.get("folderUri", "")
                for r in (p.get("projectResources") or {}).get("resources", [])
            ]
            projects.append({"id": p.get("id"), "name": p.get("name"), "folderUris": uris})
        return projects

    def ensure_project(self, workspace_dir: str) -> str:
        """Return the project id for a workspace, creating one if needed.

        Mirrors the hub frontend's pseudo-project flow: reuse the project
        whose folder URI matches, otherwise CreateProject with a new UUID.
        """
        uri = self.workspace_uri(workspace_dir)
        for p in self.list_projects():
            for fu in p["folderUris"]:
                if self._uris_match(fu, uri):
                    return p["id"]
        name = uri.rstrip("/").rsplit("/", 1)[-1] or "workspace"
        project_id = str(uuid.uuid4())
        self.rpc("CreateProject", {
            "project": {
                "id": project_id,
                "name": name,
                "projectResources": {"resources": [{"folderUri": self.workspace_uri_encoded(workspace_dir)}]},
                "isWorkspaceOnly": False,
                "settings": {},
            }
        })
        return project_id

    @staticmethod
    def _uris_match(a: str, b: str) -> bool:
        """Compare workspace URIs tolerantly (percent-encoding, drive case)."""
        from urllib.parse import unquote

        def norm(u: str) -> str:
            u = unquote(u or "")
            if len(u) >= 10 and u[9] == ":":
                u = u[:9] + u[9].lower() + u[10:]  # drive letter after file:///
            return u.rstrip("/").lower()
        return norm(a) == norm(b)

    # ------------------------------------------------------------- metadata
    def get_available_models(self) -> dict[str, Any]:
        resp = self.rpc("GetAvailableModels", {"metadata": {"ideName": "antigravity"}})
        return resp.get("response", resp)

    def default_model_id(self) -> str:
        return str(self.get_available_models().get("defaultAgentModelId", ""))

    def model_enum(self, model_id: str) -> tuple[str, dict[str, Any]]:
        """Resolve a model id (e.g. 'gemini-3.8-flash-high') to (enum name, info).

        Raises KeyError for unknown model ids, listing valid ones.
        """
        models = self.get_available_models().get("models", {})
        if model_id in models:
            info = models[model_id]
            enum = info.get("model")
            if enum:
                return enum, info
        raise KeyError(
            f"Unknown model id '{model_id}'. Available: " +
            ", ".join(sorted(models))
        )

    # -------------------------------------------------------- conversations
    def create_conversation(self, workspace_dir: str, cascade_id: str | None = None) -> str:
        """StartCascade: create a conversation bound to a workspace directory.

        The conversation is attached to a project (reused or created) so it
        appears in the Antigravity sidebar's project sections — standalone
        (project-less) conversations are hidden unless the
        `enableStandaloneConversations` flag is on.
        """
        cascade_id = cascade_id or str(uuid.uuid4())
        try:
            project_id = self.ensure_project(workspace_dir)
        except RpcError:
            project_id = None  # fall back to standalone; conversation still works
        body: dict[str, Any] = {
            "cascadeId": cascade_id,
            "source": SOURCE_CASCADE_CLIENT,
            "metadata": {"ideName": "antigravity", "ideVersion": self.target.app_version},
        }
        if project_id:
            # Project-bound conversations derive their workspace from the
            # project; passing workspaceUris too is rejected by the LS.
            body["projectEnvConfig"] = {
                "projectId": project_id,
                "defaultProjectEnvironment": {},
            }
        else:
            body["workspaceUris"] = [self.workspace_uri(workspace_dir)]
        resp = self.rpc("StartCascade", body)
        self._last_env_id = (resp.get("projectEnvInfo") or {}).get("environmentId")
        self._last_project_id = project_id
        return cascade_id

    def register_summary(self, cascade_id: str, workspace_dir: str,
                         initial_text: str) -> dict[str, Any]:
        """Persist a conversation summary so it appears in the Antigravity sidebar.

        Mirrors the hub frontend's create flow (JetboxWriteSummary with a
        CascadeTrajectorySummary); without this, StartCascade-created
        conversations exist but are never listed in the UI.
        """
        from datetime import datetime, timezone

        uri = self.workspace_uri(workspace_dir)
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        tm: dict[str, Any] = {"workspaceUris": [self.workspace_uri_encoded(workspace_dir)]}
        if getattr(self, "_last_project_id", None):
            tm["projectId"] = self._last_project_id
            if getattr(self, "_last_env_id", None):
                tm["environmentId"] = self._last_env_id
        return self.rpc("JetboxWriteSummary", {
            "cascadeId": cascade_id,
            "summary": {
                "summary": initial_text,
                "workspaces": [{"workspaceFolderAbsoluteUri": uri}],
                "lastUserInputTime": now,
                "createdTime": now,
                "trajectoryMetadata": tm,
            },
        })

    def _cascade_config(self, model_id: str | None) -> dict[str, Any] | None:
        if not model_id:
            return None
        enum, info = self.model_enum_id_safe(model_id)
        if enum is None:
            return None
        planner: dict[str, Any] = {
            "planModel": enum,
            "knowledgeConfig": {"enabled": False},
        }
        # Custom (user-added) models need their ModelInfo passed along; Google
        # models resolve server-side from the enum alone.
        if info and info.get("apiProvider") != "API_PROVIDER_GOOGLE_GEMINI":
            planner["customModelInfoOverride"] = info
        return {
            "plannerConfig": planner,
            "conversationHistoryConfig": {"enabled": False},
        }

    def model_enum_id_safe(self, model_id: str) -> tuple[str | None, dict[str, Any]]:
        try:
            return self.model_enum(model_id)
        except KeyError:
            return None, {}

    def send_message(self, cascade_id: str, text: str,
                    model_id: str | None = None,
                    attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Send a user message to a conversation. Returns the raw RPC response."""
        body: dict[str, Any] = {
            "cascadeId": cascade_id,
            "items": [{"text": text}],
        }
        cfg = self._cascade_config(model_id)
        if cfg:
            body["cascadeConfig"] = cfg
        if attachments:
            body["items"].extend(attachments)
        return self.rpc("SendUserCascadeMessage", body, timeout=300.0)

    def wait_until_idle(self, conversation_id: str, inactivity_timeout: float = 240.0,
                        stabilization: float = 3.0,
                        return_on_error: bool = True) -> dict[str, Any]:
        """Block until the conversation stops producing output (or errors)."""
        return self.rpc(
            "WaitForConversationFullyIdle",
            {
                "conversationId": conversation_id,
                "inactivityTimeoutSeconds": int(inactivity_timeout),
                "stabilizationDurationSeconds": int(stabilization),
                "returnOnExecutorError": return_on_error,
            },
            timeout=inactivity_timeout + stabilization + 30.0,
        )

    def get_steps(self, cascade_id: str, step_offset: int = 0) -> list[dict[str, Any]]:
        resp = self.rpc("GetCascadeTrajectorySteps", {
            "cascadeId": cascade_id, "stepOffset": step_offset,
        })
        return resp.get("steps", [])

    def get_conversation_metadata(self, conversation_id: str) -> dict[str, Any]:
        return self.rpc("GetConversationMetadata", {"conversationId": conversation_id})

    def delete_conversation(self, cascade_id: str) -> dict[str, Any]:
        """Remove the conversation summary and its trajectory."""
        self.rpc("JetboxDeleteSummary", {"cascadeId": cascade_id})
        return self.rpc("DeleteCascadeTrajectory", {"cascadeId": cascade_id})

    # -------------------------------------------------- user interactions
    # Map: step payload key (camelCase wire field) -> interaction case name
    # on the CascadeUserInteraction schema.
    _CONFIRM_KINDS = {
        "runCommand": "runCommand",
        "openBrowserUrl": "openBrowserUrl",
        "runExtensionCode": "runExtensionCode",
        "executeBrowserJavaScript": "executeBrowserJavaScript",
        "captureBrowserScreenshot": "captureBrowserScreenshot",
        "clickBrowserPixel": "clickBrowserPixel",
        "sendCommandInput": "sendCommandInput",
        "readUrlContent": "readUrlContent",
        "generateImage": "browserAction",
        "deploy": "deploy",
    }
    _BROWSER_ACTION_KINDS = {
        "browserInput", "browserMoveMouse", "browserSelectOption", "browserScrollUp",
        "browserScrollDown", "browserScroll", "browserResizeWindow",
        "browserDragPixelToPixel", "browserMouseWheel", "browserMouseUp",
        "browserMouseDown", "browserRefreshPage", "browserClickElement",
        "browserPressKey", "browserGetNetworkRequest", "browserListNetworkRequests",
    }

    def pending_interaction(self, cascade_id: str, step_offset: int = 0) -> dict[str, Any] | None:
        """Find the pending user interaction (a step in WAITING status).

        Returns a descriptor with the interaction kind, trajectory id and
        step index needed by HandleCascadeUserInteraction, plus the relevant
        payload details (questions, command, url, path...), or None when the
        conversation is not waiting on the user.
        """
        steps = self.get_steps(cascade_id, step_offset=step_offset)
        for s in reversed(steps):
            if s.get("status") != "CORTEX_STEP_STATUS_WAITING":
                continue
            return self._describe_waiting_step(s)
        return None

    def _describe_waiting_step(self, s: dict[str, Any]) -> dict[str, Any]:
        st = s.get("step", s)
        m = s.get("metadata", {}).get("sourceTrajectoryStepInfo", {})
        info: dict[str, Any] = {
            "stepType": s.get("type", ""),
            "trajectoryId": m.get("trajectoryId"),
            "stepIndex": m.get("stepIndex"),
        }
        if "askQuestion" in st:
            info["kind"] = "askQuestion"
            info["questions"] = st["askQuestion"].get("questions", [])
            return info
        for key, payload in st.items():
            if not isinstance(payload, dict):
                continue
            if key in self._CONFIRM_KINDS:
                info["kind"] = self._CONFIRM_KINDS[key]
                info["details"] = {k: payload[k] for k in
                                   ("command", "url", "javascriptSource", "title", "input",
                                    "absolutePathUri", "pageId", "subdomain") if k in payload}
                return info
            if key in self._BROWSER_ACTION_KINDS:
                info["kind"] = "browserAction"
                info["details"] = {k: payload[k] for k in ("url", "pageId", "text") if k in payload}
                return info
        # Generic permission gate (e.g. viewFile with filePermissionRequest).
        for key, payload in st.items():
            if isinstance(payload, dict) and "filePermissionRequest" in payload:
                info["kind"] = "filePermission"
                info["details"] = {"absolutePathUri": payload.get("absolutePathUri")}
                return info
        info["kind"] = "permission"
        return info

    def send_interaction(self, cascade_id: str, pending: dict[str, Any],
                         payload: dict[str, Any]) -> dict[str, Any]:
        """Answer a pending interaction via HandleCascadeUserInteraction."""
        interaction: dict[str, Any] = {
            "trajectoryId": pending["trajectoryId"],
            "stepIndex": pending["stepIndex"],
        }
        interaction.update(payload)
        return self.rpc("HandleCascadeUserInteraction", {
            "cascadeId": cascade_id, "interaction": interaction,
        }, timeout=120.0)

    # ------------------------------------------------------------ rewind
    def revert_to_step(self, cascade_id: str, step_index: int,
                       conversation_only: bool = True,
                       model_enum: str | None = None) -> dict[str, Any]:
        """RevertToCascadeStep — the UI's 'Undo to this point'.

        Keeps steps 0..step_index (inclusive) and truncates everything after;
        the model's context is rebuilt from the remaining steps (verified
        live: excised replies are genuinely forgotten). The LS requires
        overrideConfig.plannerConfig.planModel or it fails with 500
        'plan model not specified'. conversation_only=True leaves workspace
        files untouched; False also rolls back file edits (checkpoint).
        """
        body: dict[str, Any] = {
            "cascadeId": cascade_id,
            "stepIndex": int(step_index),
            "conversationOnly": bool(conversation_only),
        }
        if model_enum:
            body["overrideConfig"] = {
                "plannerConfig": {
                    "planModel": model_enum,
                    "knowledgeConfig": {"enabled": False},
                },
                "conversationHistoryConfig": {"enabled": False},
            }
        return self.rpc("RevertToCascadeStep", body, timeout=120.0)

    def revert_preview(self, cascade_id: str, step_index: int) -> list[dict[str, Any]]:
        """GetRevertPreview — file diffs that a full revert would roll back."""
        resp = self.rpc("GetRevertPreview", {
            "cascadeId": cascade_id, "stepIndex": int(step_index),
        }, timeout=60.0)
        return [
            {"fileUri": p.get("fileUri"), "actionType": p.get("actionType")}
            for p in resp.get("codeEditPreviews", [])
        ]
