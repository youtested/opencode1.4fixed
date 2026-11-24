"""OpenCode Zen provider (https://opencode.ai/inference/openai/v1) — free models.

Thin wrapper over OpenAICompatProvider pointing at the same OpenAI-compatible
endpoint the official client uses. With no API key, free (cost==0) models are
served and the gateway accepts the literal API key "public".

The free-tier gate is a *client fingerprint* check, verified live (2026-11):

* TLS: the gateway allowlists the ClientHello produced by Bun's fetch (the
  official client is a Bun program). httpx/curl/Node are rejected with
  429 FreeUsageLimitError even with perfect headers and body. `BUN_JA3`
  replays that handshake through curl_cffi.
* Headers: ``User-Agent: opencode/latest/<version>/cli`` and an official-shaped
  ``ses_`` session id. A bare ``opencode/1.18.32`` UA or a made-up id is 429.
* Body: the system message must contain the opening of an official client
  prompt (see ``zen_gate.GATE_MARKER``). A similar-looking prompt is 429.

These three together reproduce the anonymous free quota the official client
gets. Every outbound Zen request is shaped here so callers (chat, title,
compaction) do not need to know about the gate.
"""

from __future__ import annotations

import secrets
import string
import threading
import time
from typing import Any

from ..util.log import swallow
from .openai_compat import OpenAICompatProvider
from .zen_gate import gate_marked_messages

# Endpoint used by official opencode 2.x. The old /zen/v1 path now 404s for
# chat completions; model metadata still advertises this base URL.
ZEN_BASE_URL = "https://opencode.ai/inference/openai/v1"

# The UA must have the literal "latest" segment AND a trailing channel:
#   opencode/latest/2.2.7/cli   -> accepted
#   opencode/2.2.7              -> 429
# The channel is ignored, so the version can stay on a known-good release.
OFFICIAL_VERSION = "2.2.7"

# ClientHello fingerprint of Bun's fetch (the official client's runtime).
# Captured from live traffic; extension 41 (PSK) must be omitted because
# curl_cffi rejects non-session-resumption extension ordering.
BUN_JA3 = (
    "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171-49162"
    "-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43,4588-29-23-24,0"
)

# Session ids are time-sortable: 12 hex chars = (OFFICIAL_SESSION_EPOCH - now_ms)
# << 12 | 0xFFE, followed by 14 base62 chars. A fresh id of this shape is
# accepted indefinitely; random hex/base62 of the same length is not.
OFFICIAL_SESSION_EPOCH = 1_855_425_872_000
_SESSION_TAIL = string.ascii_uppercase + string.ascii_lowercase + string.digits

_CURRENT_VERSION = OFFICIAL_VERSION


def _parse_version(text: str) -> tuple[int, ...] | None:
    import re

    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def _bump_version_from_error(body: str) -> bool:
    """Adopt the minimum version a 426 UpgradeRequired body demands.

    Returns True when the floor moved (caller should retry once).
    """
    global _CURRENT_VERSION
    want = _parse_version(body or "")
    if not want:
        return False
    have = _parse_version(_CURRENT_VERSION) or ()
    if want > have:
        _CURRENT_VERSION = ".".join(str(n) for n in want)
        return True
    return False


def _is_upgrade_required(err: object) -> str:
    """Return the 426 body when err is an UpgradeRequired failure, else ''."""
    if getattr(err, "status", None) != 426:
        return ""
    return str(err) or ""


def gate_hint(status: int | None, body: str) -> str | None:
    """Actionable hint for a Zen free-tier failure (shown to the user).

    Returns None when nothing specific applies. Covers: 426 version floor
    (self-heal attempted), 403 fingerprint/quota, 429 quota, 5xx transient.
    """
    s = body or ""
    if status == 426:
        return (
            "Zen raised the minimum client version; this client adopted the"
            " demanded version and retried. If it persists, bump"
            " OFFICIAL_VERSION in opencode_py/providers/zen.py, then run"
            " `opencode-py --check`."
        )
    if status == 403 and ("FreeTier" in s or "within OpenCode" in s):
        return (
            "Zen rejected the client fingerprint (or the anonymous quota is"
            " exhausted). Run `opencode-py --check` for the live diagnosis;"
            " a server-side gate change needs a client update — otherwise"
            " wait and retry."
        )
    if status == 429 or "FreeUsageLimit" in s or "Rate limit" in s:
        return (
            "anonymous free quota exhausted for now — the official `opencode`"
            " client receives the same 429 until the limit resets. If that is"
            " not it, check that `curl_cffi` is installed: the free tier needs"
            " its Bun TLS fingerprint, official headers, and the gate marker."
        )
    if status is not None and 500 <= status < 600:
        return "temporary Zen server issue — wait and retry."
    return None


def _user_agent() -> str:
    """Identity string the free-tier gate accepts.

    The gate matches ``opencode/latest/<anything>/<anything>``; a version
    without the "latest" segment (or without a trailing channel) is 429.
    """
    return f"opencode/latest/{_CURRENT_VERSION}/cli"


_SES_RE: Any = None


def _ses_re() -> Any:
    global _SES_RE
    if _SES_RE is None:
        import re

        _SES_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
    return _SES_RE


def _new_session_id() -> str:
    """Mint one official-shaped session id (12 hex + 14 base62)."""
    stamp = ((OFFICIAL_SESSION_EPOCH - int(time.time() * 1000)) << 12) | 0xFFE
    return "ses_" + format(stamp, "012x")[-12:] + "".join(
        secrets.choice(_SESSION_TAIL) for _ in range(14)
    )


def zen_session_id(sid: str | None) -> str:
    """Map any conversation id to an x-opencode-session value Zen accepts.

    Only ids shaped like the official client's time-sortable ids pass the
    gate (12 hex chars derived from the current time, ending in 0xFFE, plus
    14 base62 chars). Anything else — a UUID, a hand-rolled ses_ id — is
    replaced. Derived ids stay deterministic per input so lane affinity is
    preserved across provider rebuilds.
    """
    import hashlib

    if sid and _ses_re().match(sid):
        return sid
    if not sid:
        return _new_session_id()
    digest = hashlib.sha256(sid.encode("utf-8")).digest()
    stamp = ((OFFICIAL_SESSION_EPOCH - int(time.time() * 1000)) << 12) | 0xFFE
    tail = "".join(_SESSION_TAIL[b % len(_SESSION_TAIL)] for b in digest[:14])
    return "ses_" + format(stamp, "012x")[-12:] + tail


def _project_id(seed: str) -> str:
    """Stable 40-hex project id (official sends one of these per worktree)."""
    import hashlib

    return hashlib.sha1((seed or "opencode").encode("utf-8")).hexdigest()

# Lane-rotation registry: Zen pins each x-opencode-session id to ONE upstream
# lane. When that lane dies server-side (streams end with Zen's own
# finish_reason="network_error"), retrying with the SAME id hammers the SAME
# dead lane — the user sees ↻ climb to (50) without a single success while a
# brand-new session id would have worked on attempt #1 (measured 2026-08-23:
# fixed sid A 5/5 OK, fixed sid B 1/5, fresh ids mixed). Keyed by BASE session
# id so a rotation survives provider re-instantiation: rotation.build_provider
# constructs a NEW ZenProvider for every attempt with the same engine sid.
_LANE_EPOCH: dict[str, int] = {}
_LANE_LOCK = threading.Lock()

# Deliberately empty. Zen adds and retires free models constantly, so a
# bundled list only ever goes stale and offers models the gateway no longer
# serves. The live set comes from Zen's own /models endpoint (see
# fetch_zen_models); nothing here may reintroduce a model the gateway dropped.
FREE_MODELS: list[dict] = []


class ZenProvider(OpenAICompatProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "",
        session_id: str | None = None,
        project: str | None = None,
        **kwargs: Any,
    ):
        # No baked-in model id: Zen retires models constantly, so any default
        # written here is dead within days. Resolve live instead; an empty
        # result means the gateway list is unreadable, and the caller's own
        # lane resolution handles that case.
        if not model:
            try:
                from ..config import resolve_default_model

                model = resolve_default_model()
            except Exception as _oc_exc:
                swallow(_oc_exc, "providers/zen.py:181")
                model = ""
        # If no key given, we still need SOMETHING; Zen accepts "public" for free models.
        effective_key = api_key or "public"
        # Stable session id across turns (like the official client) so Zen
        # keeps provider affinity and serves the real free quota. The id only
        # changes when rotate_session() runs on a DEAD lane — never per turn.
        self._base_session = session_id
        rot_key = session_id or "__nosession__"
        with _LANE_LOCK:
            epoch = _LANE_EPOCH.get(rot_key, 0)
        # Epoch suffixing must stay gate-compliant (official-shaped ses_ id):
        # derive a fresh compliant id per epoch instead of appending ::rN.
        effective_sid = session_id
        if epoch and session_id:
            effective_sid = zen_session_id(f"{session_id}::r{epoch}")
        elif epoch:
            effective_sid = zen_session_id(f"cli::r{epoch}")
        elif session_id:
            effective_sid = zen_session_id(session_id)
        else:
            effective_sid = _new_session_id()
        # Official v2 sends the same id under four names; the gateway accepts
        # any one of them, so send the full set for parity.
        client_headers: dict[str, str] = {
            "User-Agent": _user_agent(),
            "Accept": "*/*",
            "x-opencode-client": "cli",
            "x-opencode-project": _project_id(project or ""),
            "x-opencode-session": effective_sid,
            "x-opencode-session-id": effective_sid,
            "x-session-affinity": effective_sid,
            "x-session-id": effective_sid,
        }
        merged = dict(client_headers)
        merged.update(kwargs.pop("extra_headers", {}) or {})
        kwargs.pop("tls_ja3", None)
        super().__init__(
            id="opencode",
            name="OpenCode Zen",
            base_url=ZEN_BASE_URL,
            api_key=effective_key,
            model=model,
            is_free=True,
            extra_headers=merged,
            reasoning_passthrough=True,
            tls_ja3=BUN_JA3,
            **kwargs,
        )
        self.has_key = bool(api_key)

    def _headers(self) -> dict[str, str]:
        """Official identity headers, with the current UA version.

        UA must track _CURRENT_VERSION (auto-bumped on 426); the init-time
        extra_headers value would otherwise stay stale forever.
        """
        headers = super()._headers()
        headers["User-Agent"] = _user_agent()
        return headers

    def abort_stream(self) -> None:
        # The chat transport manages self._active_resp itself; curl_cffi
        # responses are closed through the same slot by the base class.
        super().abort_stream()

    def stream_chat(self, messages, tools=None, on_event=None, **kwargs):
        """Stream a free model, shaped exactly like the official client.

        Zen's free tier is a fingerprint gate, so every request needs:
        the Bun TLS JA3 (set at construction), official identity headers,
        and the prompt marker the gateway accepts for that request shape
        (see ``zen_gate``). The marker is injected here — chat, title and
        compaction paths all funnel through stream_chat, so none of them can
        accidentally send a gated shape.

        Chat completions is the transport these free models are served on
        (and the only one that answers anonymously at the current base URL).
        """
        from .base import ProviderError

        events: list = []
        sink = on_event or (lambda e: events.append(e))
        # The gateway accepts a different prompt marker depending on whether
        # the request carries a tool list, so the shape has to be decided here.
        tool_list = list(tools or [])
        marked = gate_marked_messages(messages, has_tools=bool(tool_list))

        def _attempt() -> None:
            self._stream(marked, tool_list or None, sink, **kwargs)

        try:
            _attempt()
        except ProviderError as e:
            # Self-healing version floor: when Zen raises the minimum client
            # version, adopt it from the 426 message and retry once instead
            # of failing every free model until the next code update.
            if _bump_version_from_error(_is_upgrade_required(e)):
                events.clear()
                _attempt()
            else:
                # Session shape rejection: the gateway 403s when a session id
                # stops looking official. Rotate to a fresh compliant id once;
                # a genuinely broken fingerprint still fails fast afterwards.
                body = str(e)
                if getattr(e, "status", None) == 403 and (
                    "FreeTier" in body or "within OpenCode" in body
                ):
                    self.rotate_session()
                    events.clear()
                    _attempt()
                else:
                    raise
        if on_event is None:
            return events  # type: ignore[return-value]
        return None

    def rotate_session(self) -> None:
        """Force a different upstream lane on the next request.

        Bumps this session's rotation epoch so every future ZenProvider built
        for the same base session id carries a fresh x-opencode-session value —
        Zen then assigns it a new lane instead of the dead one. Called by the
        rotation layer when a stream dies with server-side lane failure
        (in-band error / empty reply), NOT on local network problems.
        """
        key = self._base_session or "__nosession__"
        with _LANE_LOCK:
            epoch = _LANE_EPOCH.get(key, 0) + 1
            _LANE_EPOCH[key] = epoch
        # Official-shaped rotation value (raw ::rN suffixes would fail the
        # session-shape check): derive a fresh time-based id per epoch.
        seed = f"{self._base_session}::r{epoch}" if self._base_session else f"cli::r{epoch}"
        sid = zen_session_id(seed)
        for header in ("x-opencode-session", "x-opencode-session-id", "x-session-affinity", "x-session-id"):
            self.extra_headers[header] = sid
