"""Tests for Zen lane rotation: when the gateway pins a session to a dead
upstream lane (streams end finish_reason='network_error' with zero output),
retrying with the SAME session id re-hits the same corpse. The fix rotates
the session identity so the next attempt draws a fresh lane.

Only official-shaped ids pass the free-tier gate, so every id this client
mints or derives must match ``ses_`` + 12 hex + 14 base62.
"""

import re as _re

from opencode_py.providers.zen import ZenProvider, zen_session_id

_SES_OK = _re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
_SESSION_HEADERS = (
    "x-opencode-session",
    "x-opencode-session-id",
    "x-session-affinity",
    "x-session-id",
)


def zen(session_id):
    return ZenProvider(api_key=None, model="x-preview-f-free", session_id=session_id)


def _sids(provider):
    return {h: provider.extra_headers.get(h) for h in _SESSION_HEADERS}


def test_session_headers_are_official_shaped():
    p = zen("sess-abc")
    # The caller's internal id is never sent verbatim: only official-shaped
    # ids pass the gate, so it is deterministically derived into one.
    sid = p.extra_headers["x-opencode-session"]
    assert sid != "sess-abc" and _SES_OK.match(sid)
    # Official v2 sends the same id under four names.
    assert set(_sids(p).values()) == {sid}
    # No request-id header any more (official v2 does not send one).
    assert "x-opencode-request" not in p.extra_headers
    # Deterministic per input, so lane affinity survives provider rebuilds.
    assert zen("sess-abc").extra_headers["x-opencode-session"] == sid


def test_rotate_session_changes_wire_headers():
    p = zen("sess-abc")
    before = _sids(p)
    p.rotate_session()
    after = _sids(p)
    rotated = after["x-opencode-session"]
    assert rotated != before["x-opencode-session"] and _SES_OK.match(rotated)
    # Rotation rewrites all four aliases together.
    assert set(after.values()) == {rotated}
    # ...and persists for fresh instances (lane affinity across rebuilds).
    assert zen("sess-abc").extra_headers["x-opencode-session"] == rotated


def test_rotation_persists_across_provider_rebuilds():
    """rotation.build_provider constructs a NEW ZenProvider per attempt with
    the same engine sid — the rotated epoch must survive that."""
    zen("sess-persist")  # register base
    p1 = zen("sess-persist")
    p1.rotate_session()
    p2 = zen("sess-persist")  # next attempt's fresh instance
    r1 = p2.extra_headers["x-opencode-session"]
    assert _SES_OK.match(r1)
    p2.rotate_session()
    p3 = zen("sess-persist")
    r2 = p3.extra_headers["x-opencode-session"]
    assert _SES_OK.match(r2) and r2 != r1


def test_sessions_rotate_independently():
    a = zen("sess-a")
    b = zen("sess-b")
    a.rotate_session()
    ra = a.extra_headers["x-opencode-session"]
    rb = b.extra_headers["x-opencode-session"]
    assert _SES_OK.match(ra) and _SES_OK.match(rb)
    assert ra != rb


def test_no_session_id_still_mints_and_rotates():
    p = ZenProvider(api_key=None, model="m", session_id=None)
    before = p.extra_headers["x-opencode-session"]
    assert _SES_OK.match(before)
    p.rotate_session()
    after = p.extra_headers["x-opencode-session"]
    assert _SES_OK.match(after) and after != before
    assert zen_session_id(None) != zen_session_id(None)
