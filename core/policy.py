"""Permission classes and the policy that decides whether a validated tool call may run."""

from __future__ import annotations

import dataclasses
import enum
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from config.settings import PERMISSION_MODES

if TYPE_CHECKING:
    from tools.registry import Tool


class Permission(enum.Enum):
    READ_ONLY = "read_only"
    CONTROLLED_ACTION = "controlled_action"
    SENSITIVE_ACTION = "sensitive_action"


@dataclasses.dataclass
class ConfirmationRequest:
    tool_name: str
    permission: Permission
    operation: str
    affected_paths: list[str]
    reason: str = ""


@dataclasses.dataclass
class Decision:
    allowed: bool
    reason: str
    confirmed: bool = False


class PermissionPolicy:
    """Decides whether a validated tool call may run. The model has no input into this.

    - READ_ONLY runs in every mode.
    - CONTROLLED_ACTION runs in "standard" mode, needs confirmation in "confirm_all",
      and is denied in "read_only".
    - SENSITIVE_ACTION always needs explicit, per-operation confirmation from the user.

    Once untrusted content (web results, file contents) has entered a request, the request is
    "tainted": CONTROLLED_ACTIONs then need confirmation too, so instructions injected into a page
    or file cannot quietly open URLs carrying data, or write to memory.
    """

    def __init__(self, mode: str = "standard", confirm: Callable[[ConfirmationRequest], bool] | None = None):
        if mode not in PERMISSION_MODES:
            raise ValueError(f"Unknown permission mode {mode!r}")
        self.mode = mode
        self.confirm = confirm
        # Set by the CLI: True when the user interrupted the whole request (e.g. with the wake word).
        self.cancel_requested: Callable[[], bool] | None = None

    def evaluate(self, tool: "Tool", args: dict[str, Any], tainted: bool = False) -> Decision:
        try:
            level = tool.permission_for(args)
        except Exception as exc:  # noqa: BLE001 - an unclassifiable call is never allowed
            return Decision(False, f"Could not evaluate permissions for this call: {exc}")
        if level is Permission.READ_ONLY:
            return Decision(True, "read-only")
        if self.mode == "read_only":
            return Decision(False, "JARVIS is in read-only mode; actions that change things are disabled.")
        if level is Permission.CONTROLLED_ACTION and self.mode == "standard" and not tainted:
            return Decision(True, "controlled action allowed by policy")
        if self.confirm is None:
            return Decision(False, "This action needs the user's confirmation, and no interactive terminal is available.")
        try:
            operation, paths = tool.describe_call(args)
        except Exception as exc:  # noqa: BLE001
            return Decision(False, f"Could not describe this operation for confirmation: {exc}")
        reason = ""
        if tainted and level is Permission.CONTROLLED_ACTION and self.mode == "standard":
            reason = "Requested after reading web or file content, which could contain injected instructions."
        try:
            approved = bool(self.confirm(ConfirmationRequest(tool.name, level, operation, paths, reason)))
        except (EOFError, KeyboardInterrupt):
            approved = False
        if self.cancel_requested is not None and self.cancel_requested():
            raise KeyboardInterrupt  # cancel the request, not just this one prompt
        if approved:
            return Decision(True, "confirmed by the user", confirmed=True)
        return Decision(False, "The user declined this operation.")
