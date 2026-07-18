"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless; opt-in session continuity via X-Hermes-Session-Id header; opt-in long-term memory scoping via X-Hermes-Session-Key header)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id; X-Hermes-Session-Key supported)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent and any configured model_routes aliases
- GET  /v1/capabilities            — machine-readable API capabilities for external UIs
- GET  /api/sessions               — list client-visible Hermes sessions
- POST /api/sessions               — create an empty Hermes session
- GET/PATCH/DELETE /api/sessions/{session_id} — read/update/delete a session
- GET  /api/sessions/{session_id}/messages — read session message history
- POST /api/sessions/{session_id}/fork — branch a session using SessionDB lineage
- POST /api/sessions/{session_id}/chat[/stream] — chat with a persisted session
- POST /v1/runs                    — start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}           — retrieve current run status
- GET  /v1/runs/{run_id}/events    — SSE stream of structured lifecycle events
- POST /v1/runs/{run_id}/approval — resolve a pending run approval
- POST /v1/runs/{run_id}/stop       — interrupt a running agent
- GET  /health                     — health check
- GET  /health/detailed            — rich status for cross-container dashboard probing

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1 and
authenticating with API_SERVER_KEY.

When ``gateway.multiplex_profiles`` is on, the default profile owns this
listener and secondary profiles are reached via a URL prefix — same contract
as the webhook adapter:

    GET  /p/<profile>/v1/models
    POST /p/<profile>/v1/chat/completions
    ...

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import errno
import hashlib
import hmac
import json
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Sentinel returned by _resolve_request_profile when a /p/<profile>/ prefix
# names a profile this gateway does not serve (→ 404). Distinct from None
# (no prefix / multiplexing off → handle as the default profile).
_PROFILE_REJECTED = object()

# Profile selected by the /p/<profile>/ URL prefix for the current request.
# Set by the profile-prefix middleware; read by handlers / _run_agent.
_api_request_profile: ContextVar[Optional[str]] = ContextVar(
    "api_server_request_profile", default=None
)

def _approval_event_choices(*, smart_denied: bool, allow_permanent: bool) -> list[str]:
    if smart_denied:
        return ["once", "deny"]
    return ["once", "session", "always", "deny"] if allow_permanent else ["once", "session", "deny"]


try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    MEDIA_TAG_CLEANUP_RE,
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
    validate_media_delivery_path,
)
from agent.redact import redact_sensitive_text
from gateway.readiness import collect_runtime_readiness

logger = logging.getLogger(__name__)

_DEFAULT_RUN_MODEL_ALLOWLIST = {
    "claude-sonnet-4-6",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
}


def _hermes_version() -> str:
    """Return the hermes-agent version string, or "dev" if it can't be resolved.

    Tries the installed package metadata first (authoritative for a pip/uv
    install), then the in-tree ``hermes_cli.__version__`` (covers editable /
    source checkouts where metadata may be stale or absent). Never raises —
    a version probe must not be able to break the health endpoint.
    """
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        pass
    try:
        from hermes_cli import __version__

        return __version__
    except Exception:
        return "dev"


# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 10_000_000  # 10 MB — accommodates long agent conversations with tool calls
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    """Parse a listen port without letting malformed env/config values crash startup."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """Normalize boolean-like API payload values.

    External clients should send real JSON booleans, but some OpenAI-compatible
    frontends and middleware serialize flags like ``stream`` as strings.  Using
    Python truthiness on those values misroutes requests because ``"false"`` is
    still truthy.  Treat only explicit bool-ish scalars as booleans; everything
    else falls back to the caller's default.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        if normalized in _FALSE_REQUEST_BOOL_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_chat_content(
    content: Any, *, _max_depth: int = 10, _depth: int = 0,
) -> str:
    """Normalize OpenAI chat message content into a plain text string.

    Some clients (Open WebUI, LobeChat, etc.) send content as an array of
    typed parts instead of a plain string::

        [{"type": "text", "text": "hello"}, {"type": "input_text", "text": "..."}]

    This function flattens those into a single string so the agent pipeline
    (which expects strings) doesn't choke.

    Defensive limits prevent abuse: recursion depth, list size, and output
    length are all bounded.
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content

    if isinstance(content, list):
        parts: List[str] = []
        total_len = 0
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        for item in items:
            if isinstance(item, str):
                if item:
                    part = item[:MAX_NORMALIZED_TEXT_LENGTH]
                    parts.append(part)
                    total_len += len(part)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text", "output_text"}:
                    text = item.get("text", "")
                    if text:
                        try:
                            part = str(text)[:MAX_NORMALIZED_TEXT_LENGTH]
                            parts.append(part)
                            total_len += len(part)
                        except Exception:
                            pass
                # Silently skip image_url / other non-text parts
            elif isinstance(item, list):
                nested = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
                if nested:
                    parts.append(nested)
                    total_len += len(nested)
            # Check accumulated size
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result

    # Fallback for unexpected types (int, float, bool, etc.)
    try:
        result = str(content)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result
    except Exception:
        return ""


# Content part type aliases used by the OpenAI Chat Completions and Responses
# APIs.  We accept both spellings on input and emit a single canonical internal
# shape (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``) that the
# rest of the agent pipeline already understands.
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_FILE_PART_TYPES = frozenset({"file", "input_file"})


def _normalize_multimodal_content(content: Any) -> Any:
    """Validate and normalize multimodal content for the API server.

    Returns a plain string when the content is text-only, or a list of
    ``{"type": "text"|"image_url", ...}`` parts when images are present.
    The output shape is the native OpenAI Chat Completions vision format,
    which the agent pipeline accepts verbatim (OpenAI-wire providers) or
    converts (``_preprocess_anthropic_content`` for Anthropic).

    Raises ``ValueError`` with an OpenAI-style code on invalid input:
      * ``unsupported_content_type`` — file/input_file/file_id parts, or
        non-image ``data:`` URLs.
      * ``invalid_image_url`` — missing URL or unsupported scheme.
      * ``invalid_content_part`` — malformed text/image objects.

    Callers translate the ValueError into a 400 response.
    """
    # Scalar passthrough mirrors ``_normalize_chat_content``.
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content
    if not isinstance(content, list):
        # Mirror the legacy text-normalizer's fallback so callers that
        # pre-existed image support still get a string back.
        return _normalize_chat_content(content)

    items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
    normalized_parts: List[Dict[str, Any]] = []
    text_accum_len = 0

    for part in items:
        if isinstance(part, str):
            if part:
                trimmed = part[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if not isinstance(part, dict):
            # Ignore unknown scalars for forward compatibility with future
            # Responses API additions (e.g. ``refusal``).  The same policy
            # the text normalizer applies.
            continue

        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()

        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is None:
                continue
            if not isinstance(text, str):
                text = str(text)
            if text:
                trimmed = text[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if part_type in _IMAGE_PART_TYPES:
            detail = part.get("detail")
            image_ref = part.get("image_url")
            # OpenAI Responses sends ``input_image`` with a top-level
            # ``image_url`` string; Chat Completions sends ``image_url`` as
            # ``{"url": "...", "detail": "..."}``.  Support both.
            if isinstance(image_ref, dict):
                url_value = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url_value = image_ref
            if not isinstance(url_value, str) or not url_value.strip():
                raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
            url_value = url_value.strip()
            lowered = url_value.lower()
            if lowered.startswith("data:"):
                if not lowered.startswith("data:image/") or "," not in url_value:
                    raise ValueError(
                        "unsupported_content_type:Only image data URLs are supported. "
                        "Non-image data payloads are not supported."
                    )
            elif not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError(
                    "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs."
                )
            image_part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
            if detail is not None:
                if not isinstance(detail, str) or not detail.strip():
                    raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
                image_part["image_url"]["detail"] = detail.strip()
            normalized_parts.append(image_part)
            continue

        if part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint."
            )

        # Unknown part type — reject explicitly so clients get a clear error
        # instead of a silently dropped turn.
        raise ValueError(
            f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
            "Only text and image_url/input_image parts are supported."
        )

    if not normalized_parts:
        return ""

    # Text-only: collapse to a plain string so downstream logging/trajectory
    # code sees the native shape and prompt caching on text-only turns is
    # unaffected.
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))

    return normalized_parts


def _content_has_visible_payload(content: Any) -> bool:
    """True when content has any text or image attachment.  Used to reject empty turns."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip():
                    return True
                if ptype in _IMAGE_PART_TYPES:
                    return True
    return False


def _multimodal_validation_error(exc: ValueError, *, param: str) -> "web.Response":
    """Translate a ``_normalize_multimodal_content`` ValueError into a 400 response."""
    raw = str(exc)
    code, _, message = raw.partition(":")
    if not message:
        code, message = "invalid_content_part", raw
    return web.json_response(
        _openai_error(message, code=code, param=param),
        status=400,
    )


def _session_chat_user_message(body: Dict[str, Any], *, param: str = "message") -> tuple[Any, Optional["web.Response"]]:
    """Parse and normalize session chat ``message`` / ``input`` like chat completions."""
    user_message = body.get("message") or body.get("input")
    if not _content_has_visible_payload(user_message):
        return None, web.json_response(
            _openai_error("Missing 'message' field", code="missing_message"),
            status=400,
        )
    try:
        return _normalize_multimodal_content(user_message), None
    except ValueError as exc:
        return None, _multimodal_validation_error(exc, param=param)


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts.  Falls back to in-memory SQLite
    if the on-disk path is unavailable.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        self._db_path: Optional[str] = db_path if db_path != ":memory:" else None
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._db_path = None
        # Use shared WAL-fallback helper so response_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted HERMES_HOME (same filesystem
        # issue addressed for state.db/kanban.db — see
        # hermes_state._WAL_INCOMPAT_MARKERS).
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="response_store.db")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                name TEXT PRIMARY KEY,
                response_id TEXT NOT NULL
            )"""
        )
        self._conn.commit()
        # response_store.db contains conversation history (tool payloads,
        # prompts, results). Tighten to owner-only after creation so other
        # local users on a shared box can't read it. Run once at __init__
        # rather than after every commit — chmod-on-every-write is wasted
        # syscalls on a hot path.
        self._tighten_file_permissions()

    def _tighten_file_permissions(self) -> None:
        """Force owner-only permissions on the DB and SQLite sidecars."""
        if not self._db_path:
            return
        for candidate in (
            Path(self._db_path),
            Path(f"{self._db_path}-wal"),
            Path(f"{self._db_path}-shm"),
        ):
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                logger.debug(
                    "Failed to restrict response store permissions for %s",
                    candidate,
                    exc_info=True,
                )

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU)."""
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupted JSON in response store for id=%s, evicting entry",
                response_id,
            )
            self._conn.execute(
                "DELETE FROM responses WHERE response_id = ?",
                (response_id,),
            )
            self._conn.commit()
            return None

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity."""
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # Evict oldest entries beyond max_size
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            # Collect IDs that will be evicted
            evict_ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?",
                    (count - self._max_size,),
                ).fetchall()
            ]
            if evict_ids:
                placeholders = ",".join("?" for _ in evict_ids)
                # Clear conversation mappings pointing to evicted responses
                self._conn.execute(
                    f"DELETE FROM conversations WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
                # Delete evicted responses
                self._conn.execute(
                    f"DELETE FROM responses WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        # Clear conversation mappings pointing to this response
        self._conn.execute(
            "DELETE FROM conversations WHERE response_id = ?", (response_id,)
        )
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str) -> Optional[str]:
        """Get the latest response_id for a conversation name."""
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str) -> None:
        """Map a conversation name to its latest response_id."""
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (name, response_id) VALUES (?, ?)",
            (name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight."""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


_MEDIA_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_MEDIA_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_MEDIA_DATA_URL_MAX_BYTES = 5 * 1024 * 1024  # skip images larger than 5MB


def _resolve_media_to_data_urls(text: str) -> str:
    """Replace ``MEDIA:<path>`` image tags with inline base64 data URLs.

    Remote OpenAI-compatible frontends can't read local file paths, so
    ``MEDIA:`` tags referencing images on the server are useless to them.
    Inline small local images as markdown data URLs; non-image or unreadable
    paths are left untouched.

    Uses the same anchored ``MEDIA_TAG_CLEANUP_RE`` matcher and
    ``validate_media_delivery_path`` safety check every other platform
    adapter's media delivery already goes through (gateway/platforms/base.py)
    — an absolute-path anchor plus a known-extension requirement, and a
    resolved-path check against the credential/system-path denylist. The
    prior pattern here matched any bare token after ``MEDIA:`` (including a
    relative/traversal path like ``../../etc/passwd.png``) and read the file
    directly with no denylist, so any image-suffixed, readable file the
    process could see was base64-exfiltrated to the API caller if its path
    merely appeared in the model's own final reply text.
    """
    if not text or "MEDIA:" not in text:
        return text
    import base64

    def _to_data_url(path_str: str) -> Optional[str]:
        # validate_media_delivery_path() strips wrapping quotes/backticks
        # and trailing punctuation internally, same as MEDIA_TAG_CLEANUP_RE's
        # other callers (extract_media / _strip_media_tag_directives) rely on.
        safe_path = validate_media_delivery_path(path_str)
        if not safe_path:
            return None
        p = Path(safe_path)
        suffix = p.suffix.lower()
        if suffix not in _MEDIA_IMG_EXT:
            return None
        try:
            if p.stat().st_size > _MEDIA_DATA_URL_MAX_BYTES:
                return None
            b64 = base64.b64encode(p.read_bytes()).decode()
        except OSError:
            return None
        return f"![image](data:{_MEDIA_MIME[suffix]};base64,{b64})"

    def _repl(m: "re.Match[str]") -> str:
        return _to_data_url(m.group("path")) or m.group(0)

    try:
        return MEDIA_TAG_CLEANUP_RE.sub(_repl, text)
    except Exception:
        return text


def _redact_api_error_text(value: Any, *, limit: int | None = None) -> str:
    """Redact API-bound error text before it crosses the HTTP boundary."""
    redacted = redact_sensitive_text(str(value), force=True)
    if limit is not None:
        return redacted[:limit]
    return redacted


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI-style error envelope."""
    return {
        "error": {
            "message": _redact_api_error_text(message),
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


_api_agent_request_reservation: ContextVar[Optional[dict[str, bool]]] = ContextVar(
    "api_agent_request_reservation", default=None
)


def _admit_api_agent_request(handler):
    """Reserve an authenticated API turn before its handler first awaits.

    Gateway shutdown and aiohttp requests share an event loop. Keeping the
    drain check and reservation in one non-awaiting block prevents a request
    admitted immediately before shutdown from becoming invisible while it is
    still parsing its body or resolving session state. The mutable reservation
    is intentionally shared with child tasks so agent/task bookkeeping releases
    this one slot exactly once.
    """
    @wraps(handler)
    async def _wrapped(self, request, *args, **kwargs):
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        draining = self._draining_response()
        if draining is not None:
            return draining
        reservation = {"active": True}
        token = _api_agent_request_reservation.set(reservation)
        self._pending_agent_requests += 1
        try:
            return await handler(self, request, *args, **kwargs)
        finally:
            if reservation["active"]:
                reservation["active"] = False
                self._pending_agent_requests = max(0, self._pending_agent_requests - 1)
            _api_agent_request_reservation.reset(token)

    return _wrapped


def _release_pending_api_work(adapter, reservation: dict[str, bool]) -> None:
    """Release a pending-work reservation exactly once."""
    if reservation["active"]:
        reservation["active"] = False
        adapter._pending_agent_requests = max(0, adapter._pending_agent_requests - 1)


@contextmanager
def _reserve_pending_api_work(adapter):
    """Keep externally-triggered background work visible across awaits.

    A handler can detach the reservation to an asyncio task; its done callback
    then owns release so shutdown cannot miss the handoff to background work.
    """
    reservation = {"active": True, "detached": False}
    adapter._pending_agent_requests += 1
    try:
        yield reservation
    finally:
        if not reservation["detached"]:
            _release_pending_api_work(adapter, reservation)


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length."""
        if request.method in {"POST", "PUT", "PATCH"}:
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            # aiohttp's client_max_size tripped mid-read (chunked bodies carry
            # no Content-Length) — return a proper 413 instead of letting the
            # handler's broad JSON except turn it into 400 "Invalid JSON".
            return web.json_response(
                _openai_error("Request body too large.", code="body_too_large"),
                status=413,
            )
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors)."""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics."""
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._inflight: Dict[tuple[str, str], "asyncio.Task[Any]"] = {}
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]

        inflight_key = (key, fingerprint)
        task = self._inflight.get(inflight_key)
        if task is None:
            async def _compute_and_store():
                resp = await compute_coro()
                import time as _t
                self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
                self._purge()
                return resp

            task = asyncio.create_task(_compute_and_store())
            self._inflight[inflight_key] = task

            def _clear_inflight(done_task: "asyncio.Task[Any]") -> None:
                if self._inflight.get(inflight_key) is done_task:
                    self._inflight.pop(inflight_key, None)

            task.add_done_callback(_clear_inflight)

        return await asyncio.shield(task)


_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


def _derive_chat_session_id(
    system_prompt: Optional[str],
    first_user_message: str,
) -> str:
    """Derive a stable session ID from the conversation's first user message.

    OpenAI-compatible frontends (Open WebUI, LibreChat, etc.) send the full
    conversation history with every request.  The system prompt and first user
    message are constant across all turns of the same conversation, so hashing
    them produces a deterministic session ID that lets the API server reuse
    the same Hermes session (and therefore the same Docker container sandbox
    directory) across turns.
    """
    seed = f"{system_prompt or ''}\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


_CRON_AVAILABLE = False
try:
    from cron.jobs import (
        list_jobs as _cron_list,
        get_job as _cron_get,
        create_job as _cron_create,
        update_job as _cron_update,
        remove_job as _cron_remove,
        pause_job as _cron_pause,
        resume_job as _cron_resume,
        trigger_job as _cron_trigger,
    )
    _CRON_AVAILABLE = True
except ImportError:
    _cron_list = None
    _cron_get = None
    _cron_create = None
    _cron_update = None
    _cron_remove = None
    _cron_pause = None
    _cron_resume = None
    _cron_trigger = None


def _notify_cron_provider_jobs_changed() -> None:
    """Tell the active cron scheduler provider the job set changed after a REST
    mutation (no-op for the built-in). Best-effort — never breaks the handler."""
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass

# Defense-in-depth: mirror the agent-facing cronjob tool, which scans the
# user-supplied prompt for exfiltration/injection payloads at create/update
# time (tools/cronjob_tools.py).  The REST cron endpoints are authenticated
# (every handler runs _check_auth, and connect() refuses to start without
# API_SERVER_KEY), so this is not the trust boundary — it's parity with the
# tool path so a malicious prompt is rejected the same way regardless of
# which surface created the job.  Imported defensively: a missing scanner
# must not disable the cron REST API.
try:
    from tools.cronjob_tools import _scan_cron_prompt as _scan_cron_prompt
except Exception:  # pragma: no cover - scanner is optional hardening
    _scan_cron_prompt = None


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through hermes-agent's AIAgent.
    """

    # Stateless request/response: every route (the OpenAI-spec
    # /v1/chat/completions and /v1/responses, and the proprietary /v1/runs SSE
    # stream) tears down its channel when the turn ends. There is no persistent
    # outbound channel to push a background completion to a client that already
    # received its response, and ``send()`` is a no-op stub. So async-delivery
    # tools (terminal notify_on_complete / watch_patterns, delegate_task
    # background=True) must NOT promise delivery on this path — see
    # ``async_delivery_supported()``.
    supports_async_delivery: bool = False

    # Same statelessness applies to the startup auto-resume prompt: no client
    # is waiting to answer "session restored — what next?", so a resumed turn
    # should complete the interrupted work rather than acknowledge (#57056).
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        raw_port = extra.get("port")
        if raw_port is None:
            raw_port = os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))
        self._port: int = _coerce_port(raw_port, DEFAULT_PORT)
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._model_name: str = self._resolve_model_name(
            extra.get("model_name", os.getenv("API_SERVER_MODEL_NAME", "")),
        )
        # model_routes: maps incoming ``model`` field values to specific
        # provider/model configs so one API server instance can serve
        # multiple clients on different backends.
        #
        # Config format (platforms.api_server.extra in the gateway config):
        #   model_routes:
        #     minimax-m2:          # alias the client sends as the "model" field
        #       model: "minimax/minimax-m1"
        #       provider: "openrouter"   # optional — resolved via the provider
        #                                # credential chain when set
        #       api_key: "sk-…"          # optional — per-route UPSTREAM provider
        #                                # key override (NOT caller auth; never logged)
        #       base_url: "https://…"    # optional — per-route base URL override
        self._model_routes: Dict[str, Dict[str, Any]] = self._parse_model_routes(
            extra.get("model_routes"),
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        # Active run streams: run_id -> asyncio.Queue of SSE event dicts
        self._run_streams: Dict[str, "asyncio.Queue[Optional[Dict]]"] = {}
        # Creation timestamps for orphaned-run TTL sweep
        self._run_streams_created: Dict[str, float] = {}
        # Runs with a connected SSE consumer; their queue is actively draining.
        self._run_stream_subscribers: set[str] = set()
        # Active run agent/task references for stop support
        self._active_run_agents: Dict[str, Any] = {}
        self._active_run_tasks: Dict[str, "asyncio.Task"] = {}
        # Stop is cooperative: the executor thread may outlive the HTTP request.
        self._stopping_run_ids: set[str] = set()
        # Pollable run status for dashboards and external control-plane UIs.
        self._run_statuses: Dict[str, Dict[str, Any]] = {}
        # Active approval session key for each run_id.  The approval core
        # resolves requests by session key, while API clients address the
        # in-flight run by run_id.
        self._run_approval_sessions: Dict[str, str] = {}
        self._session_db: Optional[Any] = None  # Lazy-init SessionDB for session continuity
        # Concurrency cap shared across all agent-serving endpoints
        # (/v1/chat/completions, /v1/responses, /v1/runs). Read from
        # config.yaml gateway.api_server.max_concurrent_runs; 0 disables
        # the cap. Bounds CPU / memory / upstream-LLM-quota exhaustion
        # from a request flood (#7483).
        self._max_concurrent_runs: int = self._resolve_max_concurrent_runs()
        # Number of in-flight runs on the non-streaming chat/responses paths
        # (the /v1/runs path tracks its own in-flight set via
        # _active_run_tasks).
        self._inflight_agent_runs: int = 0
        # Back-reference to the owning GatewayRunner (set by gateway/run.py)
        # so /api/platforms/{platform}/events can resolve sibling adapters.
        # BasePlatformAdapter declares the class-level default of None.
        self.gateway_runner: Optional[Any] = None
        # Requests admitted before their handler reaches agent bookkeeping.
        # Shutdown counts this reservation so the request cannot slip through
        # the drain between its first await and _run_agent()/task registration.
        self._pending_agent_requests: int = 0

    def active_agent_work_count(self) -> int:
        """Return all live agent work owned by this API adapter.

        ``/v1/runs`` registers an asyncio task before it constructs and stores
        its agent, so ``_active_run_agents`` has a real queued-before-agent gap.
        Reuse the task-based accounting used by the concurrent-run limit: it
        covers that gap and excludes completed tasks retained until cleanup.
        """
        try:
            return (
                int(getattr(self, "_pending_agent_requests", 0))
                + int(self._inflight_agent_runs)
                + sum(not task.done() for task in self._active_run_tasks.values())
            )
        except Exception:
            return 0

    @staticmethod
    def _gateway_is_draining() -> bool:
        """Whether the owning gateway currently refuses new agent turns."""
        try:
            from gateway.run import _gateway_runner_ref

            runner = _gateway_runner_ref()
            return bool(
                runner
                and (
                    getattr(runner, "_draining", False)
                    or getattr(runner, "_external_drain_active", False)
                )
            )
        except Exception:
            return False

    def _draining_response(self) -> Optional["web.Response"]:
        """Return a retryable response while the gateway drains existing work."""
        if not self._gateway_is_draining():
            return None
        return web.json_response(
            _openai_error(
                "Gateway is draining existing work; retry shortly.",
                code="gateway_draining",
            ),
            status=503,
            headers={"Retry-After": "1"},
        )

    def _activate_admitted_request(self) -> None:
        """Transfer this request's drain reservation to agent bookkeeping."""
        reservation = _api_agent_request_reservation.get()
        if reservation and reservation["active"]:
            reservation["active"] = False
            self._pending_agent_requests = max(0, self._pending_agent_requests - 1)

    def _readiness_work_counts(self) -> tuple[int, int, int]:
        """Return bounded work counts from each subsystem's public state."""
        active_api_runs = sum(
            1
            for status in self._run_statuses.values()
            if status.get("status") in {"queued", "running", "waiting_for_approval"}
        )
        process_depth = 0
        active_delegations = 0
        try:
            from tools.process_registry import process_registry

            process_depth = process_registry.completion_queue.qsize()
        except Exception:
            pass
        try:
            from tools.async_delegation import active_count

            active_delegations = active_count()
        except Exception:
            pass
        return active_api_runs, process_depth, active_delegations

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple."""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _resolve_max_concurrent_runs() -> int:
        """Read the concurrent-run cap from config.yaml (0 disables).

        gateway.api_server.max_concurrent_runs. Falls back to the historical
        default of 10 when unset or malformed. Negative values are clamped
        to 0 (disabled).
        """
        default = 10
        try:
            from hermes_cli.config import cfg_get, load_config

            raw = cfg_get(
                load_config(),
                "gateway",
                "api_server",
                "max_concurrent_runs",
                default=default,
            )
            value = int(raw)
        except Exception:
            return default
        return max(0, value)

    @staticmethod
    def _resolve_model_name(explicit: str) -> str:
        """Derive the advertised model name for /v1/models.

        Priority:
        1. Explicit override (config extra or API_SERVER_MODEL_NAME env var)
        2. Active profile name (so each profile advertises a distinct model)
        3. Fallback: "hermes-agent"
        """
        if explicit and explicit.strip():
            return explicit.strip()
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
            if profile and profile not in {"default", "custom"}:
                return profile
        except Exception:
            pass
        return "hermes-agent"

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin."""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    @staticmethod
    def _clean_log_value(value: Any, *, max_len: int = 200) -> str:
        """Sanitize request metadata before it reaches security logs."""
        if value is None:
            return ""
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        return text[:max_len]

    def _request_audit_context(self, request: "web.Request") -> Dict[str, str]:
        """Return non-secret source metadata for security/audit warnings."""
        peer_ip = ""
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (tuple, list)) and peer:
                peer_ip = str(peer[0])
        except Exception:
            peer_ip = ""

        return {
            "remote": self._clean_log_value(getattr(request, "remote", "") or peer_ip),
            "peer_ip": self._clean_log_value(peer_ip),
            "forwarded_for": self._clean_log_value(request.headers.get("X-Forwarded-For", "")),
            "real_ip": self._clean_log_value(request.headers.get("X-Real-IP", "")),
            "method": self._clean_log_value(request.method, max_len=16),
            "path": self._clean_log_value(request.path_qs, max_len=500),
            "user_agent": self._clean_log_value(request.headers.get("User-Agent", ""), max_len=300),
        }

    def _request_audit_log_suffix(self, request: "web.Request") -> str:
        ctx = self._request_audit_context(request)
        fields = [f"{key}={value!r}" for key, value in ctx.items() if value]
        return " ".join(fields) if fields else "source='unknown'"

    def _cron_origin_from_request(self, request: "web.Request") -> Dict[str, str]:
        """Persist safe API source metadata on cron jobs created over HTTP."""
        ctx = self._request_audit_context(request)
        origin = {
            "platform": "api_server",
            "chat_id": "api",
        }
        if ctx.get("remote"):
            origin["source_ip"] = ctx["remote"]
        if ctx.get("peer_ip"):
            origin["peer_ip"] = ctx["peer_ip"]
        if ctx.get("forwarded_for"):
            origin["forwarded_for"] = ctx["forwarded_for"]
        if ctx.get("real_ip"):
            origin["real_ip"] = ctx["real_ip"]
        if ctx.get("user_agent"):
            origin["user_agent"] = ctx["user_agent"]
        return origin

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        connect() refuses to start the API server without API_SERVER_KEY, so
        the no-key branch only exists for tests or unsupported manual wiring.
        """
        if not self._api_key:
            return None

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            # Compare as bytes: ``hmac.compare_digest`` raises TypeError on a
            # str containing non-ASCII characters, and ``token`` is the raw
            # client-supplied header. A stray non-ASCII byte in the key would
            # otherwise crash this handler (500) instead of returning a clean
            # 401. Encoding both sides keeps the timing-safe comparison and
            # matches web_server.py's dashboard-token check.
            if hmac.compare_digest(token.encode(), self._api_key.encode()):
                return None  # Auth OK

        logger.warning(
            "API server rejected invalid API key: %s",
            self._request_audit_log_suffix(request),
        )
        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    @staticmethod
    def _normalize_callback_platform(value: str) -> str:
        normalized = (value or "").strip().lower().replace("-", "_")
        if not re.fullmatch(r"[a-z0-9_]+", normalized):
            return ""
        return normalized

    def _get_platform_callback_adapter(
        self,
        request: "web.Request",
        platform_name: str,
    ) -> Optional[Any]:
        injected = request.app.get("platform_event_adapters")
        if isinstance(injected, dict):
            adapter = injected.get(platform_name)
            if adapter is not None:
                return adapter

        adapter = request.app.get(f"{platform_name}_adapter")
        if adapter is not None:
            return adapter

        runner = self.gateway_runner or request.app.get("gateway_runner")
        adapters = getattr(runner, "adapters", None)
        if not adapters:
            return None

        try:
            from gateway.config import Platform as _Platform
            return adapters.get(_Platform(platform_name))
        except Exception:
            for platform, candidate in adapters.items():
                if getattr(platform, "value", platform) == platform_name:
                    return candidate
        return None

    async def _handle_platform_event_callback(self, request: "web.Request") -> "web.Response":
        platform_name = self._normalize_callback_platform(
            request.match_info.get("platform", "")
        )
        if not platform_name:
            return web.json_response(
                _openai_error(
                    "Invalid platform name",
                    code="invalid_platform",
                ),
                status=400,
            )

        adapter = self._get_platform_callback_adapter(request, platform_name)
        if adapter is None:
            return web.json_response(
                _openai_error(
                    "Platform adapter is not connected",
                    code="platform_unavailable",
                ),
                status=503,
            )

        verifier = getattr(adapter, "verify_http_event_request", None)
        dispatcher = getattr(adapter, "dispatch_http_event", None)
        if verifier is None or dispatcher is None:
            return web.json_response(
                _openai_error(
                    "Platform adapter does not support HTTP events",
                    code="platform_http_events_unsupported",
                ),
                status=503,
            )

        auth_header = request.headers.get("Authorization", "")
        try:
            if asyncio.iscoroutinefunction(verifier):
                ok, code = await verifier(auth_header)
            else:
                # Platform verifiers may do blocking network I/O (e.g. Google
                # signing-cert fetches) — keep that off the event loop.
                ok, code = await asyncio.to_thread(verifier, auth_header)
        except Exception:
            # Fail closed: a crashing verifier must never admit the event.
            logger.exception(
                "Platform HTTP event verifier failed for %s", platform_name
            )
            ok, code = False, "platform_event_verifier_error"
        if not ok:
            return web.json_response(
                _openai_error(
                    "Invalid platform event authorization",
                    code=code or "invalid_platform_event_authorization",
                ),
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                _openai_error("Invalid JSON in platform event", code="invalid_json"),
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                _openai_error(
                    "Platform event must be a JSON object",
                    code="invalid_request",
                ),
                status=400,
            )

        try:
            result = await dispatcher(payload)
        except Exception:
            logger.exception("Platform HTTP event dispatch failed for %s", platform_name)
            return web.json_response(
                _openai_error(
                    "Platform event dispatch failed",
                    err_type="server_error",
                    code="platform_event_dispatch_failed",
                ),
                status=500,
            )

        return web.json_response(result if isinstance(result, dict) else {})

    # ------------------------------------------------------------------
    # Multi-profile multiplexing (/p/<profile>/…)
    # ------------------------------------------------------------------

    def _resolve_request_profile(self, request: "web.Request"):
        """Resolve + validate the /p/<profile>/ URL prefix on an API request.

        Returns:
          - ``None`` when no profile prefix is present, or multiplexing is off
            (the prefix is ignored; request handled as the default profile).
          - the profile name (str) when present, multiplexing is on, and the
            profile is one this gateway serves.
          - ``_PROFILE_REJECTED`` when a prefix is present but the profile is
            unknown/unconfigured (handler/middleware returns 404).
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = getattr(self, "gateway_runner", None)
        cfg = getattr(runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            # Prefix supplied but multiplexing is off — ignore it, behave as
            # the single-profile gateway (don't 404 a would-be valid route).
            return None
        try:
            from hermes_cli.profiles import profiles_to_serve

            served = {name for name, _ in profiles_to_serve(multiplex=True)}
        except Exception:
            return _PROFILE_REJECTED
        if profile not in served:
            return _PROFILE_REJECTED
        return profile

    @staticmethod
    def _profile_scope(profile: Optional[str]):
        """Enter the multiplex profile runtime scope, or a no-op when unset.

        When no ``/p/<profile>/`` prefix was given AND multiplexing is active,
        enter the DEFAULT profile's scope instead of a no-op: api_server is a
        port-binding platform that lives on the default profile, and with
        multiplex fail-closed ``get_secret`` active, an unscoped agent run
        raises ``UnscopedSecretError`` on its first credential read (#61276).
        Single-profile gateways keep the no-op — ``get_secret`` falls through
        to ``os.environ`` there, unchanged.
        """
        if not profile:
            try:
                from agent.secret_scope import is_multiplex_active

                if is_multiplex_active():
                    from gateway.run import _profile_runtime_scope
                    from hermes_constants import get_hermes_home

                    return _profile_runtime_scope(get_hermes_home())
            except Exception:
                pass
            return nullcontext()
        from gateway.run import _profile_runtime_scope
        from hermes_cli.profiles import get_profile_dir

        return _profile_runtime_scope(get_profile_dir(profile))

    def _make_profile_prefix_middleware(self):
        """Reject unknown /p/<profile>/ prefixes and scope the request home."""

        @web.middleware
        async def profile_prefix_middleware(request: "web.Request", handler):
            profile = self._resolve_request_profile(request)
            if profile is _PROFILE_REJECTED:
                return web.json_response(
                    {"error": "Unknown or unconfigured profile"},
                    status=404,
                )
            token = _api_request_profile.set(profile)
            try:
                with self._profile_scope(profile):
                    return await handler(request)
            finally:
                _api_request_profile.reset(token)

        return profile_prefix_middleware

    def _http_route_table(self) -> List[tuple]:
        """Return (method, path, handler) rows registered by ``connect()``.

        Kept as a method so multiplex tests can assert the /p/<profile>/
        mirrors without starting a real aiohttp listener.
        """
        routes: List[tuple] = [
            ("GET", "/health", self._handle_health),
            ("GET", "/health/detailed", self._handle_health_detailed),
            ("GET", "/v1/health", self._handle_health),
            ("GET", "/v1/models", self._handle_models),
            ("GET", "/v1/capabilities", self._handle_capabilities),
            ("GET", "/v1/skills", self._handle_skills),
            ("GET", "/v1/toolsets", self._handle_toolsets),
            ("GET", "/api/sessions", self._handle_list_sessions),
            ("POST", "/api/sessions", self._handle_create_session),
            ("GET", "/api/sessions/{session_id}", self._handle_get_session),
            ("PATCH", "/api/sessions/{session_id}", self._handle_patch_session),
            ("DELETE", "/api/sessions/{session_id}", self._handle_delete_session),
            ("GET", "/api/sessions/{session_id}/messages", self._handle_session_messages),
            ("POST", "/api/sessions/{session_id}/fork", self._handle_fork_session),
            ("POST", "/api/sessions/{session_id}/chat", self._handle_session_chat),
            ("POST", "/api/sessions/{session_id}/chat/stream", self._handle_session_chat_stream),
            ("POST", "/v1/chat/completions", self._handle_chat_completions),
            ("POST", "/v1/responses", self._handle_responses),
            ("GET", "/v1/responses/{response_id}", self._handle_get_response),
            ("DELETE", "/v1/responses/{response_id}", self._handle_delete_response),
            # Generic platform HTTP event callback ingress. Authenticated by
            # the target adapter's own verifier (platform-signed bearer), NOT
            # API_SERVER_KEY — external platforms hold no API server key.
            ("POST", "/api/platforms/{platform}/events", self._handle_platform_event_callback),
            ("GET", "/api/jobs", self._handle_list_jobs),
            ("POST", "/api/jobs", self._handle_create_job),
            ("GET", "/api/jobs/{job_id}", self._handle_get_job),
            ("PATCH", "/api/jobs/{job_id}", self._handle_update_job),
            ("DELETE", "/api/jobs/{job_id}", self._handle_delete_job),
            ("POST", "/api/jobs/{job_id}/pause", self._handle_pause_job),
            ("POST", "/api/jobs/{job_id}/resume", self._handle_resume_job),
            ("POST", "/api/jobs/{job_id}/run", self._handle_run_job),
            ("POST", "/v1/runs", self._handle_runs),
            ("GET", "/v1/runs/{run_id}", self._handle_get_run),
            ("GET", "/v1/runs/{run_id}/events", self._handle_run_events),
            ("POST", "/v1/runs/{run_id}/approval", self._handle_run_approval),
            ("POST", "/v1/runs/{run_id}/stop", self._handle_stop_run),
        ]
        if _CRON_AVAILABLE:
            # Chronos managed-cron fire webhook (NAS → agent). Authenticated
            # by a NAS-minted JWT (NOT API_SERVER_KEY).
            routes.append(("POST", "/api/cron/fire", self._handle_cron_fire))
        return routes

    # ------------------------------------------------------------------
    # Session header helpers
    # ------------------------------------------------------------------

    # Soft length cap for session identifiers.  Headers are bounded in
    # aggregate by aiohttp (``client_max_size`` / default 8 KiB per
    # header), but we impose a tighter limit on the session headers so a
    # caller can't burn memory by passing a multi-kilobyte "session key".
    # 256 chars is well above any realistic stable channel identifier
    # (e.g. ``agent:main:webui:dm:user-42``) while staying small enough
    # that the sanitized form is safe to pass into Honcho / state.db.
    _MAX_SESSION_HEADER_LEN = 256

    def _parse_session_key_header(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        """Extract and validate the ``X-Hermes-Session-Key`` header.

        The session key is a stable per-channel identifier that scopes
        long-term memory (e.g. Honcho sessions) across transcripts.  It
        is independent of ``X-Hermes-Session-Id``: callers may send
        either, both, or neither.

        Returns ``(session_key, None)`` on success (with an empty/absent
        header yielding ``None`` for the key), or ``(None, error_response)``
        on validation failure.

        Security: like session continuation, accepting a caller-supplied
        memory scope requires API-key authentication so that an
        unauthenticated client on a local-only server can't inject itself
        into another user's long-term memory scope by guessing a key.
        """
        raw = request.headers.get("X-Hermes-Session-Key", "").strip()
        if not raw:
            return None, None

        if not self._api_key:
            logger.warning(
                "X-Hermes-Session-Key rejected: no API key configured. "
                "Set API_SERVER_KEY to enable long-term memory scoping."
            )
            return None, web.json_response(
                _openai_error(
                    "X-Hermes-Session-Key requires API key authentication. "
                    "Configure API_SERVER_KEY to enable this feature."
                ),
                status=403,
            )

        # Reject control characters that could enable header injection on
        # the echo path.
        if re.search(r'[\r\n\x00]', raw):
            return None, web.json_response(
                {"error": {"message": "Invalid session key", "type": "invalid_request_error"}},
                status=400,
            )

        if len(raw) > self._MAX_SESSION_HEADER_LEN:
            return None, web.json_response(
                {"error": {"message": "Session key too long", "type": "invalid_request_error"}},
                status=400,
            )

        return raw, None

    # ------------------------------------------------------------------
    # Session DB helper
    # ------------------------------------------------------------------

    def _ensure_session_db(self):
        """Lazily initialise and return the SessionDB for the active profile home.

        Sessions are persisted to ``state.db`` so that ``hermes sessions list``
        shows API-server conversations alongside CLI and gateway ones.

        Under multiplex ``/p/<profile>/`` requests the profile runtime scope
        redirects ``get_hermes_home()``, so each profile gets its own DB —
        never the default profile's file.
        """
        # Explicit override (tests / manual wiring) wins. Production never sets
        # this externally, so the per-home cache below is the live path — and
        # we deliberately do NOT write back into ``self._session_db`` there, or
        # the first profile served would pin every later request to its DB.
        if self._session_db is not None:
            return self._session_db
        try:
            from hermes_constants import get_hermes_home
            from hermes_state import SessionDB

            home = get_hermes_home()
            cache = getattr(self, "_session_dbs", None)
            if cache is None:
                cache = {}
                self._session_dbs = cache
            key = str(home)
            db = cache.get(key)
            if db is None:
                db = SessionDB(db_path=home / "state.db")
                cache[key] = db
            return db
        except Exception as e:
            logger.debug("SessionDB unavailable for API server: %s", e)
            return None

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_model_routes(raw: Any) -> Dict[str, Dict[str, Any]]:
        """Validate and normalize the ``model_routes`` config block.

        Accepts a mapping of ``alias -> {model, provider?, api_key?, base_url?}``.
        Invalid shapes are dropped (never raised) so a config typo can't take
        the whole API server down.  Route values are coerced to strings.

        Security: per-route ``api_key`` values are UPSTREAM provider
        credentials (used to call the routed model's backend), not caller
        authentication — callers still authenticate with the global
        API_SERVER_KEY bearer token via ``_check_auth``.  Route api_keys must
        never be logged; only alias names and non-secret fields may appear in
        logs.
        """
        if not isinstance(raw, dict):
            if raw:
                logger.warning(
                    "api_server model_routes ignored: expected a mapping, got %s",
                    type(raw).__name__,
                )
            return {}

        allowed_keys = ("model", "provider", "api_key", "base_url")
        routes: Dict[str, Dict[str, Any]] = {}
        for alias, cfg in raw.items():
            alias_str = str(alias).strip()
            if not alias_str or not isinstance(cfg, dict):
                logger.warning(
                    "api_server model_routes: dropping invalid route entry %r", alias_str or alias
                )
                continue
            route = {
                key: str(cfg[key]).strip()
                for key in allowed_keys
                if cfg.get(key) is not None and str(cfg[key]).strip()
            }
            if not route.get("model"):
                logger.warning(
                    "api_server model_routes: route %r has no 'model'; dropping", alias_str
                )
                continue
            routes[alias_str] = route
        return routes

    def _resolve_route(self, model_alias: Any) -> Optional[Dict[str, Any]]:
        """Return the model_routes entry for *model_alias*, or None."""
        if not self._model_routes or not isinstance(model_alias, str):
            return None
        return self._model_routes.get(model_alias)

    def _session_model_override_for(self, session_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the gateway's session ``/model`` override for *session_key*, if any.

        The gateway tracks per-session ``/model`` switches in
        ``GatewayRunner._session_model_overrides``.  API-server requests that
        share such a session key must keep honouring the explicit session
        override even when the request's ``model`` field matches a configured
        route — a user-issued ``/model`` always wins over static per-client route config.
        """
        if not session_key:
            return None
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if runner is None:
                return None
            override = runner._session_model_overrides.get(session_key)
            return dict(override) if isinstance(override, dict) else None
        except Exception:
            return None

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        gateway_session_key: Optional[str] = None,
        reasoning_override: Optional[Dict[str, Any]] = None,
        model_override: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the hermes-api-server default.

        ``gateway_session_key`` is a stable per-channel identifier supplied
        by the client (via ``X-Hermes-Session-Key``).  Unlike ``session_id``
        which scopes the short-term transcript and rotates on /new, this
        key is meant to persist across transcripts so long-term memory
        providers (e.g. Honcho) can scope their per-chat state correctly
        — matching the semantics of the native gateway's ``session_key``.

        ``route`` is an optional ``model_routes`` entry (per-client model
        routing).  When set — and no session ``/model`` override exists for
        this session — its model/provider/api_key/base_url override the
        global defaults for this agent instance only.
        """
        from run_agent import AIAgent
        from gateway.run import (
            _current_max_iterations,
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            GatewayRunner,
        )
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        runtime_model = runtime_kwargs.pop("model", None)
        reasoning_config = reasoning_override if reasoning_override is not None else GatewayRunner._load_reasoning_config()
        model = _resolve_gateway_model()
        if runtime_model:
            logger.info(
                "Runtime provider supplied explicit model override: %s -> %s",
                model,
                runtime_model,
            )
            model = runtime_model
        if not model and runtime_kwargs.get("provider"):
            try:
                from hermes_cli.models import get_default_model_for_provider
                model = get_default_model_for_provider(runtime_kwargs["provider"])
                if model:
                    logger.info(
                        "No model configured - defaulting to %s for provider %s",
                        model,
                        runtime_kwargs["provider"],
                    )
            except Exception:
                pass
        if model_override:
            model = model_override
            self._apply_model_override_provider(runtime_kwargs, model_override)

        # When the primary provider's auth fails (expired token / 429 quota
        # cap), _resolve_runtime_agent_kwargs() falls through to the fallback
        # provider chain, whose runtime dict carries its own ``model`` key.
        # Pop it and let it override the config model, mirroring the native
        # gateway path (_resolve_session_agent_runtime in run.py). Otherwise
        # the explicit ``model=model`` below collides with the ``**runtime_kwargs``
        # spread → "got multiple values for keyword argument 'model'", 500ing
        # every /v1/chat/completions request while a fallback is active.
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            model = runtime_model

        # Per-client model routing (model_routes config).  The route was
        # resolved from the request's ``model`` field by the HTTP handler.
        # Precedence (highest first): session ``/model`` override → model_routes
        # route → global config — an explicit user-issued ``/model`` on the
        # session always beats static per-client route config.
        session_override = self._session_model_override_for(
            gateway_session_key or session_id
        )
        if route and not session_override:
            if route.get("provider"):
                # Resolve real credentials for the routed provider (mirrors
                # the channel_overrides path in gateway/run.py) so a route
                # without an explicit api_key/base_url still gets the right
                # provider auth instead of the default provider's key.
                try:
                    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
                    provider_kwargs = _resolve_runtime_agent_kwargs_for_provider(
                        route["provider"]
                    )
                    provider_kwargs.pop("model", None)
                    runtime_kwargs.update(provider_kwargs)
                except Exception:
                    # Fall back to just switching the provider name; explicit
                    # per-route api_key/base_url below can still complete auth.
                    runtime_kwargs["provider"] = route["provider"]
            if route.get("model"):
                model = route["model"]
            # Per-route secrets are upstream provider credentials. Never log
            # them (compare _check_auth: caller auth stays the global bearer
            # key checked with hmac.compare_digest).
            if route.get("api_key"):
                runtime_kwargs["api_key"] = route["api_key"]
            if route.get("base_url"):
                runtime_kwargs["base_url"] = route["base_url"]
            logger.debug(
                "api_server model route applied: model=%s provider=%s",
                model,
                runtime_kwargs.get("provider"),
            )
        elif route and session_override:
            logger.debug(
                "api_server model route skipped: session /model override wins for %s",
                gateway_session_key or session_id,
            )

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = _current_max_iterations()

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
            reasoning_config=reasoning_config,
            gateway_session_key=gateway_session_key,
        )
        return agent

    def _run_model_allowlist(self) -> set[str]:
        raw = os.getenv("HERMES_RUN_MODEL_ALLOWLIST", "")
        if not raw.strip():
            return set(_DEFAULT_RUN_MODEL_ALLOWLIST)
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _infer_provider_for_model_override(self, model: str) -> Optional[str]:
        lowered = model.strip().lower()
        if lowered.startswith("claude") or lowered.startswith("anthropic/claude"):
            return "anthropic"
        if lowered.startswith("gpt-"):
            return "openai-codex"
        return None

    def _apply_model_override_provider(self, runtime_kwargs: Dict[str, Any], model: str) -> None:
        provider = self._infer_provider_for_model_override(model)
        if not provider or runtime_kwargs.get("provider") == provider:
            return
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider
            runtime = resolve_runtime_provider(requested=provider)
        except Exception as exc:
            logger.warning("Could not resolve provider for run model override %s: %s", model, exc)
            return
        runtime_kwargs.update({
            "api_key": runtime.get("api_key"),
            "base_url": runtime.get("base_url"),
            "provider": runtime.get("provider"),
            "api_mode": runtime.get("api_mode"),
            "command": runtime.get("command"),
            "args": list(runtime.get("args") or []),
            "credential_pool": runtime.get("credential_pool"),
        })

    def _parse_run_reasoning_effort(self, body: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional["web.Response"]]:
        raw = body.get("reasoning_effort", body.get("agentEffort"))
        if raw is None or raw == "":
            return None, None
        if not isinstance(raw, str):
            return None, web.json_response(_openai_error("'reasoning_effort' must be a string"), status=400)
        from hermes_constants import parse_reasoning_effort
        parsed = parse_reasoning_effort(raw)
        if parsed is None:
            return None, web.json_response(_openai_error("Invalid reasoning_effort; expected none|minimal|low|medium|high|xhigh"), status=400)
        return parsed, None

    def _parse_run_model_override(self, body: Dict[str, Any]) -> tuple[Optional[str], Optional["web.Response"]]:
        raw = body.get("model")
        if raw is None or raw == "":
            return None, None
        if not isinstance(raw, str):
            return None, web.json_response(_openai_error("'model' must be a string"), status=400)
        model = raw.strip()
        if model not in self._run_model_allowlist():
            return None, web.json_response(_openai_error("Requested model is not allowlisted for per-run override"), status=400)
        return model, None

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response(
            {"status": "ok", "platform": "hermes-agent", "version": _hermes_version()}
        )

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed — rich status for cross-container dashboard probing.

        Returns gateway state, connected platforms, PID, and uptime so the
        dashboard can display full status without needing a shared PID file or
        /proc access.  Requires the same Bearer auth as other API routes.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        from gateway.status import (
            derive_gateway_busy,
            derive_gateway_drainable,
            parse_active_agents,
            read_runtime_status,
        )

        runtime = read_runtime_status() or {}
        gw_state = runtime.get("gateway_state")
        gw_active = parse_active_agents(runtime.get("active_agents", 0))
        # This endpoint is served BY the gateway process, so it is by definition
        # alive — gateway_running is True. Derive busy/drainable from the same
        # shared contract /api/status uses so the two surfaces never disagree.
        active_api_runs, process_depth, active_delegations = self._readiness_work_counts()
        from gateway.run import _resolve_gateway_model

        readiness = collect_runtime_readiness(
            configured_model=_resolve_gateway_model(),
            runtime_status=runtime,
            active_api_runs=active_api_runs,
            process_completion_queue_depth=process_depth,
            active_delegations=active_delegations,
        )
        return web.json_response({
            "status": readiness["status"],
            "readiness": readiness,
            "platform": "hermes-agent",
            "version": _hermes_version(),
            "gateway_state": gw_state,
            "platforms": runtime.get("platforms", {}),
            "active_agents": gw_active,
            "gateway_busy": derive_gateway_busy(
                gateway_running=True,
                gateway_state=gw_state,
                active_agents=gw_active,
            ),
            "gateway_drainable": derive_gateway_drainable(
                gateway_running=True,
                gateway_state=gw_state,
            ),
            "exit_reason": runtime.get("exit_reason"),
            "updated_at": runtime.get("updated_at"),
            "pid": os.getpid(),
        })

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — list hermes-agent and any configured model_routes aliases.

        Under ``/p/<profile>/v1/models`` (multiplex on) the advertised primary
        model id follows that profile's name/config, not the default adapter's
        cached ``_model_name``.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()
        
        now = int(time.time())

        # When multiplexing is active and a /p/<profile>/ prefix was given,
        # the primary model id must be that profile's, not the default.
        advertised_primary_model = self._model_name
        request_profile = _api_request_profile.get()
        if request_profile:
            try:
                from hermes_cli.profiles import get_profile_dir
                from gateway.run import _profile_runtime_scope

                with _profile_runtime_scope(get_profile_dir(request_profile)):
                    advertised_primary_model = self._resolve_model_name("")
            except Exception:
                pass

        data = [
            {
                "id": advertised_primary_model,
                "object": "model",
                "created": now,
                "owned_by": "nousresearch",
            }
        ]
        # Add any configured model_routes aliases
        for alias in sorted(self._model_routes.keys()):
            data.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": now,
                    "owned_by": "nousresearch",
                }
            )
        return web.json_response({"data": data, "object": "list"})

    async def _handle_capabilities(self, request: "web.Request") -> "web.Response":
        """GET /v1/capabilities — machine-readable capabilities for external UIs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()
        
        return web.json_response({
            "sessions_api": {
                "available": True,
                "forking": True,
            },
            "runs_api": {
                "available": True,
                "approval": True,
                "stop": True,
                "per_run_model_override": True,
                "per_run_reasoning_effort": True,
            },
            "cron_api": {
                "available": _CRON_AVAILABLE,
            },
        })

    async def _handle_skills(self, request: "web.Request") -> "web.Response":
        """GET /v1/skills — list available skills for the active profile."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()
        
        try:
            from tools.registry import registry
            skills = {
                skill.name: {
                    "description": skill.description or "",
                    "functions": {
                        f.name: {
                            "description": f.description or "",
                            "parameters": f.parameters or {},
                        } for f in skill.functions
                    }
                } for skill in registry.get_all_skills()
            }
            return web.json_response(skills)
        except Exception as e:
            return web.json_response(
                _openai_error("Could not load skills", code="skills_unavailable", err_type="server_error"),
                status=500,
            )

    async def _handle_toolsets(self, request: "web.Request") -> "web.Response":
        """GET /v1/toolsets — list toolsets enabled on the API server platform."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()
        
        try:
            from gateway.run import _load_gateway_config
            from hermes_cli.tools_config import _get_platform_tools

            user_config = _load_gateway_config()
            toolsets = sorted(_get_platform_tools(user_config, "api_server"))
            return web.json_response({"toolsets": toolsets})
        except Exception as e:
            return web.json_response(
                _openai_error("Could not load toolsets", code="toolsets_unavailable", err_type="server_error"),
                status=500,
            )

    # ------------------------------------------------------------------
    # /api/sessions/* handlers
    # ------------------------------------------------------------------
    
    @_admit_api_agent_request
    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        db = self._ensure_session_db()
        if db is None:
            return web.json_response({"data": [], "object": "list"})
        
        limit = 50
        offset = 0
        if request.query.get("limit"):
            try:
                limit = max(1, min(100, int(request.query["limit"])))
            except ValueError:
                pass
        if request.query.get("offset"):
            try:
                offset = max(0, int(request.query["offset"]))
            except ValueError:
                pass
        
        sessions = await db.list_sessions_summary(limit=limit, offset=offset)
        return web.json_response({
            "data": sessions,
            "object": "list",
            "has_more": len(sessions) == limit,
        })

    @_admit_api_agent_request
    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
        
        try:
            body = await request.json() if request.content_type == "application/json" else {}
        except Exception:
            body = {}
            
        session_id = await db.create_session(
            title=body.get("title"),
            metadata=body.get("metadata"),
            forked_from=body.get("forked_from"),
        )
        return web.json_response({"id": session_id, "object": "session"}, status=201)

    @_admit_api_agent_request
    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
        
        session_id = request.match_info.get("session_id")
        if not session_id:
            return web.json_response(_openai_error("Missing session ID", code="missing_session_id"), status=400)
            
        session = await db.get_session(session_id)
        if session is None:
            return web.json_response(_openai_error("Session not found", code="session_not_found"), status=404)
            
        return web.json_response(session)

    @_admit_api_agent_request
    async def _handle_patch_session(self, request: "web.Request") -> "web.Response":
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
        
        session_id = request.match_info.get("session_id")
        if not session_id:
            return web.json_response(_openai_error("Missing session ID", code="missing_session_id"), status=400)
        
        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON", code="invalid_json"), status=400)
            
        updated = await db.update_session(
            session_id,
            title=body.get("title"),
            metadata=body.get("metadata"),
        )
        if not updated:
            return web.json_response(_openai_error("Session not found", code="session_not_found"), status=404)
            
        return web.json_response(updated)

    @_admit_api_agent_request
    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
            
        session_id = request.match_info.get("session_id")
        if not session_id:
            return web.json_response(_openai_error("Missing session ID", code="missing_session_id"), status=400)
            
        deleted = await db.delete_session(session_id)
        return web.json_response({"id": session_id, "object": "session.deleted", "deleted": deleted})

    @_admit_api_agent_request
    async def _handle_session_messages(self, request: "web.Request") -> "web.Response":
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
            
        session_id = request.match_info.get("session_id")
        if not session_id:
            return web.json_response(_openai_error("Missing session ID", code="missing_session_id"), status=400)
        
        limit = 100
        if request.query.get("limit"):
            try:
                limit = max(1, min(500, int(request.query["limit"])))
            except ValueError:
                pass
        
        messages = await db.get_session_messages(session_id, limit=limit)
        return web.json_response({
            "data": messages,
            "object": "list",
            "has_more": len(messages) == limit,
        })
    
    @_admit_api_agent_request
    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
        
        parent_id = request.match_info.get("session_id")
        if not parent_id:
            return web.json_response(_openai_error("Missing parent session ID", code="missing_parent_session_id"), status=400)
            
        try:
            body = await request.json() if request.content_type == "application/json" else {}
        except Exception:
            body = {}
            
        child_id = await db.fork_session(
            parent_id,
            new_title=body.get("title"),
            new_metadata=body.get("metadata"),
            message_index=body.get("message_index"),
        )
        if child_id is None:
            return web.json_response(_openai_error("Parent session not found", code="session_not_found"), status=404)
        
        return web.json_response({"id": child_id, "object": "session"}, status=201)

    @_admit_api_agent_request
    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        session_id = request.match_info.get("session_id")
        if not session_id:
            return web.json_response(_openai_error("Missing session ID", code="missing_session_id"), status=400)
        
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
            
        session = await db.get_session(session_id)
        if session is None:
            return web.json_response(_openai_error("Session not found", code="session_not_found"), status=404)

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)
        
        user_message, err = _session_chat_user_message(body)
        if err:
            return err
        
        reservation = _api_agent_request_reservation.get()
        return await self._run_agent_on_session(
            session_id=session_id,
            user_message=user_message,
            reservation=reservation,
            request=request,
            ephemeral_prompt=body.get("ephemeral_prompt") or body.get("system_prompt"),
            model_override=body.get("model"),
            stream=False,
        )

    @_admit_api_agent_request
    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.Response":
        session_id = request.match_info.get("session_id")
        if not session_id:
            return web.json_response(_openai_error("Missing session ID", code="missing_session_id"), status=400)
        
        db = self._ensure_session_db()
        if db is None:
            return web.json_response(
                _openai_error("Session persistence is not available", code="sessions_unavailable"),
                status=503,
            )
            
        session = await db.get_session(session_id)
        if session is None:
            return web.json_response(_openai_error("Session not found", code="session_not_found"), status=404)

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)
        
        user_message, err = _session_chat_user_message(body)
        if err:
            return err

        reservation = _api_agent_request_reservation.get()
        return await self._run_agent_on_session(
            session_id=session_id,
            user_message=user_message,
            reservation=reservation,
            request=request,
            ephemeral_prompt=body.get("ephemeral_prompt") or body.get("system_prompt"),
            model_override=body.get("model"),
            stream=True,
        )

    # ------------------------------------------------------------------
    # /v1/chat/completions handler
    # ------------------------------------------------------------------

    @_admit_api_agent_request
    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """
        POST /v1/chat/completions — OpenAI-compatible chat completions endpoint.

        Accepts stateless requests with full conversation history and optional
        streaming.  Supports session continuity via the non-standard
        ``X-Hermes-Session-Id`` header.
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(_openai_error("Invalid JSON"), status=400)
        except Exception as e:
            return web.json_response(_openai_error(f"Could not parse request: {e}"), status=400)
        
        stream = _coerce_request_bool(body.get("stream"), default=False)
        
        # Extract messages and normalize content
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list):
            return web.json_response(_openai_error("'messages' must be a list", param="messages"), status=400)

        # Session continuity via headers
        session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
        session_key, key_err = self._parse_session_key_header(request)
        if key_err:
            return key_err
        
        # Extract and validate content from messages. If there are no user messages,
        # derive a session ID from the first message.
        messages: List[Dict[str, Any]] = []
        user_message_content = None
        system_prompt = None
        first_user_message = ""
        
        for i, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                return web.json_response(_openai_error(f"Message at index {i} must be a dictionary", param=f"messages[{i}]"), status=400)
            
            role = msg.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                return web.json_response(_openai_error(f"Invalid role '{role}' for message at index {i}", param=f"messages[{i}].role"), status=400)
            
            try:
                content = _normalize_multimodal_content(msg.get("content"))
            except ValueError as e:
                return _multimodal_validation_error(e, param=f"messages[{i}].content")

            if role == "system":
                if not isinstance(content, str):
                    return web.json_response(_openai_error("System message content must be a string", param=f"messages[{i}].content"), status=400)
                system_prompt = content
            elif role == "user":
                if not first_user_message:
                    # Capture the string-normalized form
                    first_user_message = _normalize_chat_content(content)
                user_message_content = content
            
            messages.append({**msg, "content": content})

        if not user_message_content:
            return web.json_response(_openai_error("No user message found", param="messages"), status=400)

        # Derive a stable session ID if none was provided. This keeps the
        # agent's sandbox (terminal, browser) consistent across turns of the
        # same conversation for stateless OpenAI-compatible clients.
        if not session_id and self._ensure_session_db() is not None:
            session_id = _derive_chat_session_id(system_prompt, first_user_message)
        
        reservation = _api_agent_request_reservation.get()
        return await self._run_agent_on_session(
            session_id=session_id,
            user_message=user_message_content,
            reservation=reservation,
            request=request,
            ephemeral_prompt=system_prompt,
            conversation_history=messages[:-1],  # All but the last user message
            model_override=body.get("model"),
            stream=stream,
            session_key=session_key,
        )

    # ------------------------------------------------------------------
    # /v1/responses/* handlers (legacy, stateful)
    # ------------------------------------------------------------------

    @_admit_api_agent_request
    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — stateful chat using previous_response_id."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        user_message, err = _session_chat_user_message(body, param="prompt")
        if err:
            return err

        prev_id = body.get("previous_response_id")
        history = []
        if prev_id:
            stored = self._response_store.get(prev_id)
            if stored is None:
                return web.json_response(_openai_error("Previous response not found", code="previous_response_not_found"), status=404)
            history = stored.get("messages", [])

        conversation_name = body.get("conversation_name")
        if conversation_name:
            latest_id = self._response_store.get_conversation(conversation_name)
            if latest_id and latest_id != prev_id:
                return web.json_response(
                    _openai_error(
                        "conversation_name is out of date; use the latest response_id",
                        code="stale_conversation",
                    ),
                    status=409,
                )
        
        reservation = _api_agent_request_reservation.get()
        result = await self._run_agent_on_session(
            session_id=body.get("session_id"),  # Allow explicit session_id
            user_message=user_message,
            reservation=reservation,
            request=request,
            ephemeral_prompt=body.get("ephemeral_prompt") or body.get("system_prompt"),
            conversation_history=history,
            stream=False,
        )
        
        # Store and return the OpenAI-format response
        response_id = f"res_{uuid.uuid4().hex}"
        openai_response = {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.get("model", self._model_name),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": result.get("final_response"),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": result.get("last_prompt_tokens", 0),
                "completion_tokens": result.get("output_tokens", 0),
                "total_tokens": result.get("last_prompt_tokens", 0) + result.get("output_tokens", 0),
            },
            # Non-standard extensions
            "hermes_session_id": result.get("session_id"),
        }
        self._response_store.put(response_id, {
            **openai_response,
            "messages": result.get("messages", []),
        })
        if conversation_name:
            self._response_store.set_conversation(conversation_name, response_id)
        
        return web.json_response(openai_response)

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()

        response_id = request.match_info.get("response_id")
        if not response_id:
            return web.json_response(_openai_error("Missing response ID"), status=400)
            
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error("Response not found"), status=404)
        
        # Don't leak internal conversation history through the public GET endpoint
        stored.pop("messages", None)
        return web.json_response(stored)

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()

        response_id = request.match_info.get("response_id")
        if not response_id:
            return web.json_response(_openai_error("Missing response ID"), status=400)
        
        deleted = self._response_store.delete(response_id)
        return web.json_response({"id": response_id, "deleted": deleted})

    # ------------------------------------------------------------------
    # /v1/runs/* handlers (stateful, streaming)
    # ------------------------------------------------------------------

    @_admit_api_agent_request
    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start a run, return 202 with run_id."""
        
        # Concurrency cap
        active = sum(not task.done() for task in self._active_run_tasks.values())
        if self._max_concurrent_runs > 0 and active >= self._max_concurrent_runs:
            return web.json_response(
                _openai_error("Too many concurrent runs", code="too_many_runs"),
                status=429,
                headers={"Retry-After": "1"},
            )
            
        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        user_message, err = _session_chat_user_message(body)
        if err:
            return err
            
        run_id = f"run_{uuid.uuid4().hex}"
        q = asyncio.Queue()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = time.time()

        # Capture the reservation so the detached task can release it.
        reservation = _api_agent_request_reservation.get()
        if reservation:
            reservation["detached"] = True
        
        # Run agent in background, stream events to queue
        task = asyncio.create_task(
            self._run_agent_and_stream_events(
                run_id=run_id,
                q=q,
                user_message=user_message,
                reservation=reservation,
                request=request,
                body=body,
            )
        )
        self._active_run_tasks[run_id] = task
        
        def _cleanup_task(done_task):
            self._active_run_tasks.pop(run_id, None)
            self._stopping_run_ids.discard(run_id)

        task.add_done_callback(_cleanup_task)

        return web.json_response({"id": run_id, "object": "run"}, status=202)

    async def _run_agent_and_stream_events(
        self,
        run_id: str,
        q: "asyncio.Queue",
        user_message: Any,
        reservation: Optional[Dict[str, bool]],
        request: "web.Request",
        body: Dict[str, Any],
    ) -> None:
        """Run agent and push lifecycle events to the SSE queue."""
        created_at = time.time()
        session_id = body.get("session_id")
        ephemeral_system_prompt = body.get("ephemeral_prompt") or body.get("system_prompt")
        conversation_history = body.get("conversation_history", [])
        working_directory = body.get("working_directory")

        # Allow /v1/runs to specify a gateway session key (honcho scoping).
        # When absent, use the run_id as a transient key — keeps approval
        # state isolated per run without needing a durable session.
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err:
            # Can't return a Response here; just log and proceed without a key.
            logger.warning(
                "Invalid X-Hermes-Session-Key on /v1/runs: %s",
                (str(key_err.text)[:200] if hasattr(key_err, "text") else "..."),
            )
            gateway_session_key = None
        
        # An API client can explicitly bind a run to a gateway session key, a
        # durable session ID, or both. When neither is given, generate a
        # transient key for this run so approval state stays isolated.
        approval_session_key = gateway_session_key or session_id or f"run_{run_id}"
        self._run_approval_sessions[run_id] = approval_session_key

        reasoning_override, err = self._parse_run_reasoning_effort(body)
        if err:
            self._set_run_status(run_id, "failed", error=err.text)
            q.put_nowait({"event": "run.failed", "run_id": run_id, "timestamp": time.time(), "error": err.text})
            q.put_nowait(None)
            if reservation:
                _release_pending_api_work(self, reservation)
            return

        model_override, err = self._parse_run_model_override(body)
        if err:
            self._set_run_status(run_id, "failed", error=err.text)
            q.put_nowait({"event": "run.failed", "run_id": run_id, "timestamp": time.time(), "error": err.text})
            q.put_nowait(None)
            if reservation:
                _release_pending_api_work(self, reservation)
            return
            
        loop = asyncio.get_running_loop()

        def event_cb(event_type: str, tool_name: str = None, preview: str = None, args: dict = None, **kwargs):
            if run_id not in self._run_streams:
                return
            event: Dict[str, Any] = {
                "event": event_type,
                "run_id": run_id,
                "timestamp": time.time(),
            }
            if tool_name:
                event["tool_name"] = tool_name
            if preview:
                event["preview"] = preview
            if args:
                event["args"] = args
            if kwargs:
                event.update(kwargs)
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass
        
        def _put_event_if_active(event: dict) -> None:
            """Enqueue only while this run still owns live transport state."""
            if self._run_streams.get(run_id) is q:
                q.put_nowait(event)

        # Also wire stream_delta_callback so message.delta events flow through.
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            if run_id not in self._run_streams:
                return
            try:
                loop.call_soon_threadsafe(_put_event_if_active, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        self._set_run_status(
            run_id,
            "queued",
            created_at=created_at,
            session_id=session_id,
            model=model_override or self._model_name,
            reasoning_effort=body.get("reasoning_effort", body.get("agentEffort")),
        )

        # Per-client model routing for /v1/runs (see model_routes).
        route = self._resolve_route(body.get("model"))
        # Background task outlives the HTTP response (and thus the middleware
        # profile scope). Capture now and re-enter inside the task/executor.
        request_profile = _api_request_profile.get()

        async def _run_and_close():
            try:
                self._set_run_status(run_id, "running")
                if run_id in self._stopping_run_ids:
                    _put_event_if_active({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                    self._set_run_status(
                        run_id,
                        "cancelled",
                        last_event="run.cancelled",
                    )
                    return
                with self._profile_scope(request_profile):
                    agent = self._create_agent(
                        ephemeral_system_prompt=ephemeral_system_prompt,
                        session_id=session_id,
                        stream_delta_callback=_text_cb,
                        tool_progress_callback=event_cb,
                        gateway_session_key=gateway_session_key,
                        reasoning_override=reasoning_override,
                        model_override=model_override,
                        route=route,
                    )
                self._active_run_agents[run_id] = agent

                def _approval_notify(approval_data: Dict[str, Any]) -> None:
                    event = dict(approval_data or {})
                    # Redact credentials from the command before it enters the
                    # SSE/API event stream — same egress bug as #48456, second
                    # transport: API/desktop clients would otherwise receive the
                    # raw command Tirith flagged. Reuse the gateway seam.
                    if "command" in event:
                        from gateway.run import _redact_approval_command

                        event["command"] = _redact_approval_command(event.get("command"))
                    event.update({
                        "event": "approval.request",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "choices": _approval_event_choices(
                            smart_denied=bool(event.get("smart_denied")),
                            allow_permanent=event.get("allow_permanent") is not False,
                        ),
                    })
                    self._set_run_status(
                        run_id,
                        "waiting_for_approval",
                        last_event="approval.request",
                    )
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, event)
                    except Exception:
                        pass

                def _run_sync():
                    from gateway.session_context import clear_session_vars
                    from tools.terminal_tool import (
                        clear_task_env_overrides,
                        register_task_env_overrides,
                    )
                    from tools.approval import (
                        register_gateway_notify,
                        reset_current_session_key,
                        set_current_session_key,
                        unregister_gateway_notify,
                    )

                    effective_task_id = session_id or run_id
                    approval_token = None
                    session_tokens = []
                    cwd_task_ids = []
                    with self._profile_scope(request_profile):
                        try:
                            # Bind approval/session identity for this API run via
                            # contextvars so concurrent runs do not share process
                            # environment state.
                            approval_token = set_current_session_key(approval_session_key)
                            session_tokens = self._bind_api_server_session(
                                chat_id=session_id or run_id,
                                session_key=approval_session_key,
                                session_id=session_id or run_id,
                                cwd=working_directory,
                            )
                            if working_directory:
                                cwd_task_ids = list(dict.fromkeys((effective_task_id, approval_session_key)))
                                for cwd_task_id in cwd_task_ids:
                                    register_task_env_overrides(cwd_task_id, {"cwd": working_directory})
                            register_gateway_notify(approval_session_key, _approval_notify)
                            r = agent.run_conversation(
                                user_message=user_message,
                                conversation_history=conversation_history,
                                task_id=effective_task_id,
                            )
                        finally:
                            if approval_token is not None:
                                try:
                                    reset_current_session_key(approval_token)
                                except Exception:
                                    pass
                            if session_tokens:
                                try:
                                    clear_session_vars(session_tokens)
                                except Exception:
                                    pass
                            for cwd_task_id in cwd_task_ids:
                                clear_task_env_overrides(cwd_task_id)
                            unregister_gateway_notify(approval_session_key)
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                if run_id in self._stopping_run_ids:
                    _put_event_if_active({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                    self._set_run_status(
                        run_id,
                        "cancelled",
                        last_event="run.cancelled",
                    )
                # Check for structured failure (non-retryable client errors like
                # 401/400 return failed=True instead of raising, so the except
                # block below never fires — issue #15561).
                elif isinstance(result, dict) and result.get("failed"):
                    error_msg = _redact_api_error_text(result.get("error") or "agent run failed")
                    _put_event_if_active({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": error_msg,
                    })
                    self._set_run_status(
                        run_id,
                        "failed",
                        error=error_msg,
                        last_event="run.failed",
                    )
                else:
                    final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                    _put_event_if_active({
                        "event": "run.completed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "final_response": final_response,
                        "usage": usage,
                    })
                    self._set_run_status(
                        run_id,
                        "completed",
                        final_response=final_response,
                        usage=usage,
                        last_event="run.completed",
                    )
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                logger.exception("Agent run %s failed", run_id)
                _put_event_if_active({
                    "event": "run.failed",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "error": error_msg,
                })
                self._set_run_status(
                    run_id,
                    "failed",
                    error=error_msg,
                    last_event="run.failed",
                )
            finally:
                self._active_run_agents.pop(run_id, None)
                _put_event_if_active(None)  # Sentinel to close stream
                # Detached reservation was passed in; we own its release.
                if reservation:
                    _release_pending_api_work(self, reservation)

        # Keep the reservation visible until the background task registers its
        # own bookkeeping, then release it so shutdown doesn't miss the handoff.
        # Use a done callback so we release even if the task never reaches its
        # own finally block (e.g. cancelled before start).
        run_task = asyncio.create_task(_run_and_close())
        if reservation:
            run_task.add_done_callback(
                lambda _fut: _release_pending_api_work(self, reservation)
            )

    async def _handle_get_run(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()

        run_id = request.match_info.get("run_id")
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(_openai_error("Run not found"), status=404)
        return web.json_response(status)

    async def _handle_run_events(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()

        run_id = request.match_info.get("run_id")
        if run_id not in self._run_streams:
            return web.json_response(_openai_error("Run not found or already completed"), status=404)

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)
        self._run_stream_subscribers.add(run_id)

        try:
            q = self._run_streams.get(run_id)
            if q is None:
                return resp
            
            while True:
                event = await q.get()
                if event is None:
                    break
                
                await resp.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
        finally:
            self._run_stream_subscribers.discard(run_id)
            # If this was the last subscriber and the run is complete, clean up.
            if not any(run_id in s for s in (self._run_stream_subscribers, self._active_run_tasks)):
                self._run_streams.pop(run_id, None)
                self._run_streams_created.pop(run_id, None)

        return resp

    @_admit_api_agent_request
    async def _handle_run_approval(self, request: "web.Request") -> "web.Response":
        run_id = request.match_info.get("run_id")
        if not run_id:
            return web.json_response(_openai_error("Missing run ID"), status=400)
            
        status = self._run_statuses.get(run_id)
        if status is None or status.get("status") != "waiting_for_approval":
            return web.json_response(_openai_error("No approval pending for this run"), status=400)

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)
            
        decision = (body.get("decision") or "").strip().lower()
        if not decision:
            return web.json_response(_openai_error("Missing 'decision' field"), status=400)

        session_key = self._run_approval_sessions.get(run_id)
        if not session_key:
            return web.json_response(_openai_error("Approval session not found for this run"), status=500)
            
        try:
            from tools.approval import resolve_pending_command
            
            if decision == "approve_once":
                resolve_pending_command(session_key, "once")
            elif decision == "approve_session":
                resolve_pending_command(session_key, "session")
            elif decision == "approve_always":
                resolve_pending_command(session_key, "always")
            elif decision == "deny":
                resolve_pending_command(session_key, "deny")
            else:
                return web.json_response(_openai_error("Invalid decision"), status=400)
        except Exception as e:
            return web.json_response(_openai_error(f"Could not resolve approval: {e}"), status=500)

        self._set_run_status(run_id, "running", last_event=f"approval.{decision}")
        return web.json_response({"status": "ok"})
        
    async def _handle_stop_run(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/stop — interrupt a running agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if self._draining_response():
            return self._draining_response()

        run_id = request.match_info.get("run_id")
        if not run_id:
            return web.json_response(_openai_error("Missing run ID"), status=400)
            
        if run_id not in self._active_run_tasks and run_id not in self._active_run_agents:
            return web.json_response(_openai_error("Run not found or not running"), status=404)
        
        self._stopping_run_ids.add(run_id)
        
        agent = self._active_run_agents.get(run_id)
        if agent:
            agent.interrupt("API stop request")
        
        task = self._active_run_tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            
        self._set_run_status(run_id, "cancelling", last_event="run.stop_requested")
        return web.json_response({"status": "stopping"})

    # ------------------------------------------------------------------
    # Run status helpers
    # ------------------------------------------------------------------

    def _set_run_status(self, run_id: str, status: str, **kwargs):
        """Update and store the status of a run."""
        if run_id not in self._run_statuses:
            self._run_statuses[run_id] = {"id": run_id, "object": "run"}
            
        self._run_statuses[run_id].update({
            "status": status,
            "updated_at": time.time(),
            **kwargs,
        })
        
    async def _sweep_stale_runs(self):
        """Periodically clean up old completed runs and orphaned streams."""
        while True:
            await asyncio.sleep(300)
            now = time.time()
            stale_run_ids = [
                rid for rid, status in self._run_statuses.items()
                if status.get("status") in {"completed", "failed", "cancelled"}
                and now - status.get("updated_at", 0) > 3600  # 1 hour
            ]
            for rid in stale_run_ids:
                self._run_statuses.pop(rid, None)

            orphaned_stream_ids = [
                rid for rid, created in self._run_streams_created.items()
                if rid not in self._run_stream_subscribers
                and rid not in self._active_run_tasks
                and now - created > 3600
            ]
            for rid in orphaned_stream_ids:
                self._run_streams.pop(rid, None)
                self._run_streams_created.pop(rid, None)

    # ------------------------------------------------------------------
    # Main connect/disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the aiohttp server."""
        if not AIOHTTP_AVAILABLE:
            logger.error("API server requires 'aiohttp' — install with: pip install 'hermes-agent[api_server]'")
            return False
            
        if not self._api_key:
            logger.error(
                "API server requires an API key. Set API_SERVER_KEY in your environment "
                "or platforms.api_server.extra.key in config.yaml"
            )
            return False

        middlewares = [body_limit_middleware, security_headers_middleware, self._make_profile_prefix_middleware(), cors_middleware]
        self._app = web.Application(middlewares=middlewares)
        self._app["api_server_adapter"] = self
        
        for method, path, handler in self._http_route_table():
            self._app.router.add_route(method, path, handler)
            # Add mirrored /p/<profile>/... routes for multiplexing
            self._app.router.add_route(method, f"/p/{{profile}}{path}", handler)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        
        try:
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                logger.error(f"API server address {self._host}:{self._port} is already in use.")
            else:
                logger.error(f"Could not start API server: {e}")
            await self._runner.cleanup()
            return False
            
        logger.info(f"API server listening on http://{self._host}:{self._port}")
        logger.info("Connect with any OpenAI-compatible client.")
        if self._cors_origins:
            logger.info(f"CORS allowed origins: {', '.join(self._cors_origins)}")
        
        asyncio.create_task(self._sweep_stale_runs())
        
        return True

    async def disconnect(self):
        """Stop the aiohttp server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._response_store.close()

    # ------------------------------------------------------------------
    # No-op stubs for BasePlatformAdapter
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, content: str, **kwargs) -> SendResult:
        """API server is stateless; send is a no-op."""
        return SendResult(success=True)

    async def edit_message(self, chat_id: str, message_id: str, content: str, **kwargs) -> SendResult:
        """API server is stateless; edit is a no-op."""
        return SendResult(success=False, error="not_supported")

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> SendResult:
        """API server is stateless; delete is a no-op."""
        return SendResult(success=False, error="not_supported")
