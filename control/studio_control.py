"""Scoped, localhost-only Studio tools; no server, deployment, or import side effects.

Mutation idempotence is local and durable: a request ID is never automatically
replayed, including after a crash. An input echo acknowledges admission only;
only an official history read can verify persistence. Receipt files contain the
original messages and must inherit the private control directory's permissions.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from websockets.asyncio.client import connect

API_BASE = "http://127.0.0.1:8001"
# Historical fixture identities only; never authorization rules or tool defaults.
PNG_CONVERSATION = "4060a541-e41a-42e5-9035-f74240489417"
PNG_SAVED_NAME = "general_7dfc4fab"
PNG_GOAL = "goal-4zcrum"
MAX_BYTES = 1048576
SESSION_RESOLUTION_TIMEOUT = 30.0
MAX_HISTORY_PAGES = 10
REQUEST_PATTERN = r"[A-Za-z0-9_-]{8,128}"
USER_EVENT_TYPES = {"user_input", "user_message", "user_input_injected"}
IDENTITY_FIELDS = (
    "drive_id", "kind", "schema_version", "scope_type", "scope_id", "owner",
    "owner_scope", "created_by", "created_at", "assignee_creature_id", "assignment_state",
)
GOAL_STATE_FIELDS = ("status", "lifecycle_epoch", "status_reason", "dependency_ids")
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "retired"}
SECRET_KEYS = {
    "password", "passwd", "api_key", "apikey", "access_token", "refresh_token",
    "authorization", "cookie", "credentials", "secret", "client_secret",
    "config_snapshot", "config", "meta",
}


class ControlError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.data = {"code": code, "message": message, **details}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact(value):
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()
                if k.lower().replace("-", "_") not in SECRET_KEYS}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _segment(value: str) -> str:
    # The saved-history route unquotes again; reject percent escapes as well.
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(c in value for c in "/\\%?#")
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ControlError("invalid_identity", "Unsafe native identity")
    return quote(value, safe="")


def _chunk(text: str, offset: int, max_chars: int) -> dict:
    end = min(len(text), offset + max_chars)
    return {"content": text[offset:end], "offset": offset,
            "next_offset": end if end < len(text) else None,
            "full_length": len(text), "utf8_bytes": len(text.encode("utf-8")),
            "sha256": _sha(text)}


def _content(record: dict, field="auto") -> tuple[str, str, str | None]:
    if field != "auto":
        if not isinstance(record.get(field), str):
            raise ControlError("content_field_unavailable", "Selected record field is not text", field=field)
        return record[field], "text", field
    keys = ("output", "content", "text") if record.get("type") in ("tool_result", "activity") else (
        "content", "output", "text")
    for key in keys:
        if isinstance(record.get(key), str):
            return record[key], "text", key
    return _json(_redact(record)), "json", None


def _string(**kw):
    return {"type": "string", "minLength": 1, **kw}


def _integer(minimum=0, maximum=None, **kw):
    return {"type": "integer", "minimum": minimum,
            **({"maximum": maximum} if maximum is not None else {}), **kw}


SESSION = {"session_id": _string(), "creature_id": _string()}
CHUNK = {"offset": _integer(default=0),
         "max_chars": _integer(1, 262144, default=32000)}
REQUEST = _string(pattern="^" + REQUEST_PATTERN + "$")
BOOL = {"type": "boolean", "default": False}
GOAL = {"session_id": _string(), "goal_id": _string()}
NULLABLE_TEXT = {"type": ["string", "null"]}
PATCH_SCHEMA = {
    "type": "object", "minProperties": 1, "additionalProperties": False,
    "properties": {
        "title": _string(), "spec": {"type": "object"},
        "presentation": {"type": ["object", "null"]},
        "metadata": {"type": ["object", "null"]}, "priority": {"type": "integer"},
        "status_reason": NULLABLE_TEXT, "not_before": NULLABLE_TEXT,
        "expires_at": NULLABLE_TEXT,
        "dependency_ids": {"type": ["array", "null"], "items": _string()},
        "policy_options": {"type": ["object", "null"]},
    },
}
TOOL_SPECS = {
    "sessions_list": (
        "Search minimal summaries of existing sessions. With include_closed, query "
        "uses native saved search (name, preview, config path, agents, pwd). Each call "
        "reads at most one saved page; follow next_offset for older matches. Offsets "
        "count matching open rows then native search hits, including deduplicated mirrors.",
        {"query": {"type": "string"}, "include_closed": BOOL,
         "limit": _integer(1, 100, default=50), "offset": _integer(default=0)}, []),
    "session_read": (
        "Read one member-scoped, bounded native history page. Cursors are opaque. "
        "Preserve history_page.stream and history_id when paging a snapshot fallback.",
        {**SESSION, "limit": _integer(1, 100, default=30),
         "stream": _string(enum=["events", "snapshot"], default="events"),
         "history_id": _string(), "before": _string(), "after": _string()}, ["session_id"]),
    "session_history_detail": (
        "Read a full native history record as a text chunk with UTF-8 hash. Auto "
        "prefers full output for tool_result/activity and retains its content summary "
        "in metadata; field can explicitly select content, output or text. "
        "history_id, stream and ref must belong to the selected existing member.",
        {**SESSION, "history_id": _string(), "ref": _string(), **CHUNK,
         "stream": _string(enum=["events", "snapshot"], default="events"),
         "field": _string(enum=["auto", "content", "output", "text"], default="auto")},
        ["session_id", "history_id", "ref"]),
    "session_send": (
        "Send exact plain text once to an existing member. Leading slash commands "
        "are forbidden. Resume requires explicit resume_if_paused=true. The same "
        "request_id never sends again, even if uncertain; echoes are not persistence.",
        {**SESSION, "message": _string(), "request_id": REQUEST,
         "resume_if_paused": BOOL, "dry_run": BOOL},
        ["session_id", "message", "request_id"]),
    "session_message_read": (
        "Read the retained submission separately from bounded official persistence "
        "readback. Never polls, resumes, or resends; missing history is inconclusive.",
        {"request_id": REQUEST, **CHUNK}, ["request_id"]),
    "goals_list": (
        "Discover existing goals in a chosen conversation. Live lists retain native "
        "redaction and allowed_actions; saved viewer rows are persistent, redacted, "
        "read-only snapshots without live authorization. Never resumes.",
        {"session_id": _string(), "include_terminal": {"type": "boolean", "default": True}},
        ["session_id"]),
    "goal_get": (
        "Read a chosen existing goal on its canonical graph or, without resuming, "
        "as a redacted saved viewer snapshot with no live mutation authority.",
        GOAL, ["session_id", "goal_id"]),
    "goal_update": (
        "PATCH editable fields of a chosen existing goal with exact CAS. Spec merges "
        "at top level, with nested budgets merged; omitted policy and limits survive. "
        "Explicit policy edits are allowed; native policy controls terminal approval. "
        "No lifecycle/identity changes. Resume is explicit; dry runs never resume. "
        "Same request_id never mutates twice, including uncertain outcomes.",
        {**GOAL, "expected_revision": _integer(1), "request_id": REQUEST,
         "patch": PATCH_SCHEMA, "dry_run": BOOL, "resume_if_paused": BOOL},
        ["session_id", "goal_id", "expected_revision", "request_id", "patch"]),
    "goal_transition": (
        "Explicitly activate, wait, pause, block or cancel a chosen existing goal "
        "using native CAS, ACL and legal-transition policy. Never separately wakes, "
        "completes, fails, creates or transfers ownership. Resume is explicit; "
        "dry runs never resume. Same request_id never mutates twice.",
        {**GOAL, "expected_revision": _integer(1), "request_id": REQUEST,
         "target_status": _string(enum=["active", "waiting", "paused", "blocked", "cancelled"]),
         "status_reason": NULLABLE_TEXT, "dry_run": BOOL, "resume_if_paused": BOOL},
        ["session_id", "goal_id", "expected_revision", "request_id", "target_status"]),
}


def _validate(value, schema: dict, path="arguments"):
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "string": isinstance(value, str),
             "integer": type(value) is int, "boolean": type(value) is bool,
             "array": isinstance(value, list), "null": value is None}
    kinds = kind if isinstance(kind, list) else [kind]
    if kind is not None and not any(valid.get(k, False) for k in kinds):
        raise ControlError("invalid_arguments", f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ControlError("invalid_arguments", f"{path} has an unsupported value")
    if isinstance(value, dict) and "object" in kinds:
        props = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                raise ControlError("invalid_arguments", f"{path}.{required} is required")
        if len(value) < schema.get("minProperties", 0):
            raise ControlError("invalid_arguments", f"{path} must not be empty")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ControlError("invalid_arguments", "Object keys must be strings")
            if key not in props and schema.get("additionalProperties") is False:
                raise ControlError("invalid_arguments", f"{path}.{key} is forbidden")
            if key in props:
                _validate(item, props[key], f"{path}.{key}")
    elif isinstance(value, str) and "string" in kinds:
        if len(value) < schema.get("minLength", 0):
            raise ControlError("invalid_arguments", f"{path} must not be empty")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ControlError("invalid_arguments", f"{path} has invalid format")
    elif type(value) is int and "integer" in kinds:
        if ("minimum" in schema and value < schema["minimum"]
                or "maximum" in schema and value > schema["maximum"]):
            raise ControlError("invalid_arguments", f"{path} is out of range")
    elif isinstance(value, list) and "array" in kinds:
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{index}]")


class StudioControl:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate configuration key")
                    result[key] = value
                return result
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"),
                             object_pairs_hook=unique)
            keys = {"version", "api_base", "receipts_db", "allowed_send",
                    "writable_goals", "max_message_bytes"}
            if not isinstance(cfg, dict) or set(cfg) != keys:
                raise ValueError("Unexpected configuration keys")
            if type(cfg["version"]) is not int or cfg["version"] != 1:
                raise ValueError("Unsupported configuration version")
            if cfg["api_base"] != API_BASE or cfg["allowed_send"] != "all_existing":
                raise ValueError("Only the trusted localhost base and existing sessions are allowed")
            if (type(cfg["max_message_bytes"]) is not int
                    or not 1 <= cfg["max_message_bytes"] <= MAX_BYTES):
                raise ValueError("Invalid message bound")
            if not isinstance(cfg["receipts_db"], str):
                raise ValueError("receipts_db must be an absolute path")
            db = Path(cfg["receipts_db"])
            if (not db.is_absolute() or db.name != "receipts.sqlite3"
                    or db.resolve().parent != self.config_path.parent):
                raise ValueError("Receipts must be the private control/receipts.sqlite3")
            if cfg["writable_goals"] != "all_existing":
                raise ValueError("writable_goals must be all_existing")
        except (OSError, TypeError, KeyError, ValueError) as exc:
            raise ValueError("Invalid private Studio control configuration") from exc
        self.db_path = db.resolve()
        self.max_message_bytes = cfg["max_message_bytes"]
        self.writable_goals = cfg["writable_goals"]

    @property
    def tool_names(self) -> set[str]:
        return set(TOOL_SPECS)

    def tools(self) -> list[Tool]:
        result = []
        for name, (description, properties, required) in TOOL_SPECS.items():
            mutation = name in ("session_send", "goal_update", "goal_transition")
            result.append(Tool(
                name=name, description=description,
                inputSchema={"type": "object", "properties": copy.deepcopy(properties),
                             "required": required[:], "additionalProperties": False},
                annotations=ToolAnnotations(
                    readOnlyHint=not mutation,
                    destructiveHint=name in ("goal_update", "goal_transition"),
                    idempotentHint=mutation, openWorldHint=False),
            ))
        return result

    async def call(self, name: str, arguments: dict) -> CallToolResult:
        try:
            if name not in self.tool_names:
                raise ControlError("unknown_tool", "Unknown scoped Studio tool")
            _, props, required = TOOL_SPECS[name]
            _validate(arguments, {"type": "object", "properties": props,
                                  "required": required, "additionalProperties": False})
            _json(arguments).encode("utf-8")  # Reject non-JSON values and invalid Unicode.
            args = copy.deepcopy(arguments)
            for key, schema in props.items():
                if key not in args and "default" in schema:
                    args[key] = schema["default"]
            if name == "session_message_read":
                data = await self._session_message_read(args)
            else:
                async with self._client() as client:
                    data = await getattr(self, "_" + name)(client, args)
            return self._result(data)
        except ControlError as exc:
            return self._result({"error": exc.data}, error=True)
        except (ValueError, TypeError, UnicodeError):
            return self._result({"error": {"code": "invalid_arguments",
                                           "message": "Arguments must be valid UTF-8 JSON"}}, True)
        except (httpx.HTTPError, OSError, sqlite3.Error):
            return self._result({"error": {"code": "service_unavailable",
                                           "message": "Native service or private receipts unavailable"}}, True)

    @staticmethod
    def _result(data: dict, error=False) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=_json(data))],
                              structuredContent=data, isError=error)

    def _client(self):
        return httpx.AsyncClient(base_url=API_BASE, trust_env=False,
                                 follow_redirects=False, timeout=15.0)

    def _connect(self, url: str):
        return connect(url, proxy=None, max_size=16 * 1024 * 1024,
                       open_timeout=10, close_timeout=2)

    async def _http(self, client, method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        if not 200 <= response.status_code < 300:
            details = {}
            try:
                body = response.json()
                # Native auth/CAS codes and human-readable detail, not raw headers/body.
                for key in ("code", "error", "detail", "message", "current_revision",
                            "expected_revision"):
                    if isinstance(body, dict) and key in body:
                        value = _redact(body[key])
                        text = _json(value)
                        text = re.sub(r'(?i)(bearer\s+|(?:token|password|api_key)[=:]\s*)[^\s",]+',
                                      r'\1[redacted]', text)
                        details[key] = json.loads(text) if len(text) <= 2048 else "[omitted]"
            except (ValueError, TypeError):
                pass
            raise ControlError("native_http_error", "Native Studio request denied or failed",
                               http_status=response.status_code, details=details)
        try:
            result = response.json()
        except ValueError as exc:
            raise ControlError("native_protocol_error", "Invalid native JSON response") from exc
        if not isinstance(result, (dict, list)):
            raise ControlError("native_protocol_error", "Unexpected native response")
        return result

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS receipts (
                request_id TEXT PRIMARY KEY, action TEXT NOT NULL, payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL, context TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _receipt(self, request_id):
        if not self.db_path.exists():
            return None
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM receipts WHERE request_id=?", (request_id,)).fetchone()
            return self._decode_receipt(row) if row else None
        finally:
            db.close()

    @staticmethod
    def _decode_receipt(row):
        return {**dict(row), **{k: json.loads(row[k]) for k in ("payload", "context", "result")}}

    def _reserve(self, request_id, payload, context):
        fingerprint = _sha(_json(payload))
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM receipts WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row["payload_hash"] != fingerprint:
                    raise ControlError("request_conflict", "request_id already binds a different payload")
                db.commit()
                return self._decode_receipt(row), False
            db.execute("""INSERT INTO receipts
                (request_id,action,payload_hash,payload,context,state,result)
                VALUES (?,?,?,?,?,'reserved','{}')""",
                       (request_id, payload["action"], fingerprint, _json(payload), _json(context)))
            db.commit()  # Durable reservation precedes resume, websocket send, PATCH or transition.
        finally:
            db.close()
        return self._receipt(request_id), True

    def _check_replay(self, request_id, payload):
        row = self._receipt(request_id)
        if row and row["payload_hash"] != _sha(_json(payload)):
            raise ControlError("request_conflict", "request_id already binds a different payload")
        return row

    def _record(self, request_id, state, *, context=None, result=None):
        db = self._db()
        try:
            fields, values = ["state=?", "updated_at=CURRENT_TIMESTAMP"], [state]
            for key, value in (("context", context), ("result", result)):
                if value is not None:
                    fields.append(key + "=?")
                    values.append(_json(value))
            db.execute("UPDATE receipts SET " + ",".join(fields) + " WHERE request_id=?",
                       (*values, request_id))
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _summary(row, saved=False):
        creatures = row.get("creatures") or row.get("agents") or []
        members = []
        for c in creatures:
            if isinstance(c, str):
                members.append({"creature_id": None, "name": c, "is_privileged": False})
            elif isinstance(c, dict):
                members.append({"creature_id": c.get("creature_id") or c.get("id"),
                                "name": c.get("name"),
                                "is_privileged": c.get("is_privileged") is True})
        live = row.get("is_live") is True and bool(row.get("runtime_id"))
        return {"conversation_id": row.get("conversation_id"),
                "saved_name": row.get("saved_name") or (row.get("name") if saved else None),
                "runtime_id": row.get("runtime_id") if live else None,
                "name": row.get("config_name") or row.get("terrarium_name")
                        or (members[0]["name"] if members else row.get("name")),
                "status": row.get("status") or ("running" if live else "paused"),
                "is_live": live, "creatures": members}

    @staticmethod
    def _rows(payload):
        rows = payload if isinstance(payload, list) else payload.get("sessions", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ControlError("native_protocol_error", "Invalid native session listing")
        return rows

    async def _saved_page(self, client, *, limit, offset, search=""):
        payload = await self._http(client, "GET", "/api/sessions",
                                   params={"limit": limit, "offset": offset, "search": search})
        raw = self._rows(payload)
        if (not isinstance(payload, dict) or type(payload.get("total")) is not int
                or payload["total"] < 0 or payload.get("offset", offset) != offset
                or len(raw) > limit or (not raw and offset < payload["total"])):
            raise ControlError("native_protocol_error", "Invalid native saved-session page")
        return raw, payload["total"]

    async def _sessions_list(self, client, args):
        native_open = self._rows(await self._http(client, "GET", "/api/sessions/open"))
        query = args.get("query", "")
        open_rows, open_by_id, open_by_name = [], {}, {}
        for native in native_open:
            row = self._summary(native)
            if row["conversation_id"]:
                open_by_id[row["conversation_id"]] = row
            if row["saved_name"]:
                open_by_name[row["saved_name"]] = row
            searchable = {**row, **{k: native.get(k) for k in ("preview", "config_path", "pwd")}}
            if not query or query.casefold() in _json(searchable).casefold():
                open_rows.append(row)
        start, limit = args["offset"], args["limit"]
        rows = open_rows[start:start + limit]
        scanned, next_offset = 0, None
        if start + len(rows) < len(open_rows):
            next_offset = start + len(rows)
        elif args["include_closed"]:
            saved_offset = max(0, start - len(open_rows))
            remaining = limit - len(rows)
            # A full open page probes total without consuming the first saved hit.
            raw, total = await self._saved_page(client, limit=max(1, remaining),
                                                 offset=saved_offset, search=query)
            if remaining:
                known_ids = {r["conversation_id"] for r in open_rows if r["conversation_id"]}
                known_names = {r["saved_name"] for r in open_rows if r["saved_name"]}
                for native in raw:
                    row = self._summary(native, saved=True)
                    # Saved FTS can match preview/config fields absent from the open rail.
                    row = open_by_id.get(row["conversation_id"]) or open_by_name.get(row["saved_name"]) or row
                    if row["conversation_id"] in known_ids or row["saved_name"] in known_names:
                        continue
                    rows.append(row)
                    if row["conversation_id"]:
                        known_ids.add(row["conversation_id"])
                    if row["saved_name"]:
                        known_names.add(row["saved_name"])
                scanned = len(raw)
            if saved_offset + scanned < total:
                next_offset = len(open_rows) + saved_offset + scanned
        minimal = [{**row, "creatures": [{"creature_id": c["creature_id"], "name": c["name"]}
                                         for c in row["creatures"]]} for row in rows]
        return {"sessions": minimal, "offset": start, "next_offset": next_offset,
                "saved_rows_scanned": scanned, "scan_truncated": False}

    async def _saved_index(self, client, saved_name):
        index = await self._http(client, "GET", f"/api/sessions/{_segment(saved_name)}/history")
        if (not isinstance(index, dict) or not isinstance(index.get("session_name"), str)
                or not index["session_name"] or not isinstance(index.get("meta"), dict)
                or not isinstance(index["meta"].get("conversation_id"), str)
                or not index["meta"]["conversation_id"]
                or not isinstance(index.get("targets"), list)
                or any(not isinstance(t, str) or not t for t in index["targets"])):
            raise ControlError("native_protocol_error", "Invalid saved history index identity or targets")
        return index

    @staticmethod
    def _resolved(found):
        if (len(found) != 1 or not isinstance(found[0]["conversation_id"], str)
                or not found[0]["conversation_id"] or not isinstance(found[0]["saved_name"], str)
                or not found[0]["saved_name"]):
            raise ControlError("ambiguous_identity", "A unique canonical saved conversation is required")
        row = found[0]
        _segment(row["saved_name"])
        if row["runtime_id"]:
            _segment(row["runtime_id"])
        return row

    async def _resolve(self, client, session_id):
        _segment(session_id)
        scanned = 0
        try:
            async with asyncio.timeout(SESSION_RESOLUTION_TIMEOUT):
                rows = [self._summary(r) for r in self._rows(
                    await self._http(client, "GET", "/api/sessions/open"))]
                def matches(row):
                    return session_id in (row["conversation_id"], row["runtime_id"], row["saved_name"])
                found = [r for r in rows if matches(r)]
                if found:
                    return self._resolved(found)
                try:
                    index = await self._saved_index(client, session_id)
                except ControlError as exc:
                    if exc.data.get("http_status") != 404:
                        raise
                else:
                    # Native path lookup can be fuzzy; an alias is not exact identity proof.
                    if index["session_name"] == session_id:
                        native_id = index["meta"]["conversation_id"]
                        if any(r["conversation_id"] == native_id for r in rows):
                            raise ControlError("ambiguous_identity", "Saved name does not match the open canonical conversation")
                        return self._resolved([self._summary({
                            "name": session_id, "conversation_id": native_id,
                            "status": index["meta"].get("status"),
                            "terrarium_name": index["meta"].get("terrarium_name"),
                            "agents": [t for t in index["targets"] if not t.startswith("ch:")],
                        }, saved=True)])
                # UUIDs are not native FTS fields. Map actual indexed identities, not search hits.
                offset = 0
                while True:
                    raw, total = await self._saved_page(client, limit=100, offset=offset)
                    scanned += len(raw)
                    found.extend(row for row in (self._summary(r, saved=True) for r in raw) if matches(row))
                    offset += len(raw)
                    if offset >= total:
                        break
                if not found:
                    raise ControlError("session_not_found", "Existing exact session not found after index exhaustion",
                                       scan_truncated=False)
                return self._resolved(found)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ControlError("resolution_incomplete", "Exact session resolution timed out; absence is not established",
                               saved_rows_scanned=scanned, scan_truncated=True) from exc

    async def _target(self, client, row, creature_id=None):
        members = row["creatures"]
        if not row["is_live"]:
            index = await self._saved_index(client, row["saved_name"])
            if (index["session_name"] != row["saved_name"]
                    or index["meta"]["conversation_id"] != row["conversation_id"]):
                raise ControlError("identity_changed", "Saved history canonical identity changed")
            members = [{"creature_id": None, "name": t, "is_privileged": False}
                       for t in index["targets"] if not t.startswith("ch:")]
        if creature_id is not None:
            selected = [c for c in members if creature_id in (c["creature_id"], c["name"])]
        else:
            selected = [c for c in members if c["is_privileged"]]
            if len(selected) != 1:
                selected = [c for c in members if c["name"] == row["name"]]
            if len(selected) != 1 and len(members) == 1:
                selected = members[:]
        if len(selected) != 1:
            raise ControlError("invalid_target", "Select a unique existing member of this conversation")
        target = selected[0]
        if (not target["name"] or (row["is_live"] and not target["creature_id"])
                or sum(c["name"] == target["name"] for c in members) != 1):
            raise ControlError("invalid_target", "Native member identity is incomplete or ambiguous")
        _segment(target["name"])
        if target["creature_id"]:
            _segment(target["creature_id"])
        return target

    @staticmethod
    def _identity(row, target=None):
        result = {k: row[k] for k in ("conversation_id", "saved_name", "runtime_id", "status", "is_live")}
        if target:
            result["target"] = {"creature_id": target["creature_id"], "name": target["name"]}
        return result

    @staticmethod
    def _history_path(row, target):
        base = f"/api/sessions/{_segment(row['runtime_id'] if row['is_live'] else row['saved_name'])}"
        if row["is_live"]:
            return base + f"/creatures/{_segment(target['creature_id'])}/history"
        return base + f"/history/{_segment(target['name'])}"

    async def _history(self, client, row, target, **params):
        payload = await self._http(client, "GET", self._history_path(row, target),
                                   params={"paged": "true", "stream": "events", **params})
        if not isinstance(payload, dict) or not isinstance(payload.get("history_page"), dict):
            raise ControlError("native_protocol_error", "Native history page is missing")
        return payload

    async def _detail(self, client, row, target, history_id, ref, stream="events"):
        # Installed session.history_records.history_detail returns this envelope;
        # history_paging previews carry a string token, not a nested detail object.
        if not isinstance(ref, str) or not ref or not isinstance(history_id, str) or not history_id:
            raise ControlError("native_protocol_error", "Native detail token or history identity is missing")
        payload = await self._http(client, "GET", self._history_path(row, target) + "/detail",
                                   params={"stream": stream, "history_id": history_id, "ref": ref})
        if (not isinstance(payload, dict) or not isinstance(payload.get("record"), dict)
                or not isinstance(payload.get("history_page"), dict)):
            raise ControlError("native_protocol_error", "Native detail envelope is missing")
        hp = payload["history_page"]
        if hp.get("stream") != stream or hp.get("history_id") != history_id:
            raise ControlError("identity_changed", "Native detail history identity does not match")
        return payload

    async def _session_read(self, client, args):
        if "before" in args and "after" in args:
            raise ControlError("invalid_arguments", "before and after are mutually exclusive")
        row = await self._resolve(client, args["session_id"])
        target = await self._target(client, row, args.get("creature_id"))
        params = {k: args[k] for k in ("limit", "stream", "history_id", "before", "after") if k in args}
        history = await self._history(client, row, target, **params)
        return {**self._identity(row, target), "history_page": _redact(history["history_page"]),
                "is_processing": bool(history.get("is_processing", False)),
                "live_job_ids": history.get("live_job_ids", []),
                "events": _redact(history.get("events", [])),
                "messages": _redact(history.get("messages", []))}

    async def _session_history_detail(self, client, args):
        row = await self._resolve(client, args["session_id"])
        target = await self._target(client, row, args.get("creature_id"))
        detail = await self._detail(client, row, target, args["history_id"], args["ref"], args["stream"])
        text, fmt, field = _content(detail["record"], args["field"])
        metadata = {k: v for k, v in detail["record"].items() if k not in ("content", "output", "text")}
        if field == "output" and isinstance(detail["record"].get("content"), str):
            metadata["content"] = detail["record"]["content"]
        return {**self._identity(row, target), **_chunk(text, args["offset"], args["max_chars"]),
                "format": fmt, "content_field": field, "metadata": _redact(metadata),
                "history_page": _redact(detail["history_page"])}

    async def _session_send(self, client, args):
        message, rid = args["message"], args["request_id"]
        if message.lstrip().startswith("/"):
            raise ControlError("forbidden_command", "Leading slash commands are not plain-text chat")
        if len(message.encode("utf-8")) > self.max_message_bytes:
            raise ControlError("message_too_large", "Message exceeds configured UTF-8 byte limit")
        row = await self._resolve(client, args["session_id"])
        target = await self._target(client, row, args.get("creature_id"))
        payload = {"action": "session_send", "conversation_id": row["conversation_id"],
                   "target": target["name"], "message": message}
        preview = {**self._identity(row, target), "request_id": rid,
                   "utf8_bytes": len(message.encode("utf-8")), "full_length": len(message),
                   "sha256": _sha(message), "needs_resume": not row["is_live"]}
        if args["dry_run"]:
            return {**preview, "dry_run": True, "would_resume": not row["is_live"] and args["resume_if_paused"]}
        prior = self._check_replay(rid, payload)
        if prior:
            return await self._message_result(client, prior, 0, 32000, replayed=True)
        if not row["is_live"] and not args["resume_if_paused"]:
            return {**preview, "state": "needs_resume", "resumed": False}
        context = {"identity": self._identity(row, target), "baseline": None, "resumed": False}
        receipt, owned = self._reserve(rid, payload, context)
        if not owned:
            return await self._message_result(client, receipt, 0, 32000, replayed=True)
        attempted, resume_attempted, ack = False, False, None
        try:
            if not row["is_live"]:
                self._record(rid, "sending", context=context)
                resume_attempted = True
                await self._http(client, "POST", f"/api/sessions/{_segment(row['saved_name'])}/resume", json={})
                current = await self._resolve(client, row["conversation_id"])
                if current["saved_name"] != row["saved_name"] or not current["is_live"]:
                    raise ControlError("identity_changed", "Resume did not yield the same saved conversation")
                row = current
                target = await self._target(client, row, target["name"])
                context.update(identity=self._identity(row, target), resumed=True)
            url = f"ws://127.0.0.1:8001/ws/sessions/{_segment(row['runtime_id'])}/creatures/{_segment(target['creature_id'])}/chat"
            async with self._connect(url) as ws:
                # Capture the baseline after connection, immediately before this input.
                history = await self._history(client, row, target, limit=1)
                hp = history["history_page"]
                context["baseline"] = {"history_id": hp.get("history_id"),
                                       "after": hp.get("after") if hp.get("stream") == "events" else None}
                self._record(rid, "sending", context=context)
                async with asyncio.timeout(10):
                    attempted = True  # Even a send() exception can have written the frame.
                    await ws.send(_json({"type": "input", "event_id": "mcp_" + rid, "content": message}))
                    for _ in range(1000):
                        frame = json.loads(await ws.recv())
                        if not isinstance(frame, dict) or frame.get("event_id") != "mcp_" + rid:
                            continue
                        if frame.get("type") == "input_queued":
                            ack = "queued"
                            break
                        if frame.get("type") == "user_input" and frame.get("content") == message:
                            ack = "accepted"
                            break
                if ack is None:
                    raise ControlError("ack_unknown", "No matching native admission acknowledgement")
                self._record(rid, ack, result={"ack_type": "input_queued" if ack == "queued" else "user_input",
                                             "persistence_verified": False, "execution_complete": False})
        except asyncio.CancelledError:
            if ack is None:
                self._record(rid, "uncertain" if attempted or resume_attempted else "rejected")
            raise
        except Exception as exc:
            # A close failure must not erase a received acknowledgement.
            if ack is None:
                state = "uncertain" if attempted or resume_attempted else "rejected"
                error = exc.data if isinstance(exc, ControlError) else {
                    "code": "ack_unknown" if attempted else "connect_failed",
                    "message": "Delivery uncertain; never retry this request_id" if attempted else "Input was not sent"}
                self._record(rid, state, result={"error": error, "persistence_verified": False})
        receipt = self._receipt(rid)
        if receipt["state"] == "uncertain":
            return await self._message_result(client, receipt, 0, 32000)
        return {**self._receipt_summary(receipt), "replayed": False,
                "readback_status": "not_checked", "execution_complete": False}

    @staticmethod
    def _receipt_summary(receipt):
        payload = receipt["payload"]
        summary = {"request_id": receipt["request_id"], "state": receipt["state"],
                   "action": receipt["action"], "payload_hash": receipt["payload_hash"],
                   "conversation_id": payload["conversation_id"],
                   "submitted_target": payload.get("target"),
                   "submitted_identity": receipt["context"].get("identity"),
                   "resumed": receipt["context"].get("resumed", False),
                   "result": receipt["result"]}
        if receipt["action"] == "session_send":
            message = payload["message"]
            summary.update(full_length=len(message), utf8_bytes=len(message.encode("utf-8")),
                           sha256=_sha(message))
        return summary

    async def _session_message_read(self, args):
        receipt = self._receipt(args["request_id"])
        if not receipt or receipt["action"] != "session_send":
            raise ControlError("receipt_not_found", "No retained chat receipt for this request_id")
        async with self._client() as client:
            return await self._message_result(client, receipt, args["offset"], args["max_chars"])

    async def _message_result(self, client, receipt, offset, max_chars, replayed=False):
        result = {**self._receipt_summary(receipt), "replayed": replayed,
                  "submitted": _chunk(receipt["payload"]["message"], offset, max_chars),
                  "canonical_persisted": None, "readback_status": "inconclusive",
                  "execution_complete": False, "pages_read": 0}
        try:
            row = await self._resolve(client, receipt["payload"]["conversation_id"])
            if row["saved_name"] != receipt["context"]["identity"]["saved_name"]:
                raise ControlError("identity_changed", "Canonical saved conversation changed")
            target = await self._target(client, row, receipt["payload"]["target"])
            result["current_identity"] = self._identity(row, target)
            baseline = receipt["context"].get("baseline") or {}
            params = {"limit": 100}
            forward = bool(baseline.get("after"))
            if forward:
                params.update(after=baseline["after"], history_id=baseline["history_id"])
            seen = set()
            pending_id = "mcp_" + receipt["request_id"]
            for _ in range(MAX_HISTORY_PAGES):
                try:
                    page = await self._history(client, row, target, **params)
                except ControlError as exc:
                    if forward and exc.data.get("http_status") in (400, 409):
                        params, forward = {"limit": 100}, False
                        result["cursor_reset"] = True
                        continue
                    raise
                result["pages_read"] += 1
                hp = page["history_page"]
                if hp.get("reset_required"):
                    if not forward:
                        break
                    params, forward = {"limit": 100}, False
                    result["cursor_reset"] = True
                    continue
                for event in page.get("events", []):
                    if not isinstance(event, dict):
                        continue
                    # A native preview may omit type/pending_id; expand before filtering.
                    if event.get("_history_truncated") or event.get("_history_unavailable"):
                        event = (await self._detail(client, row, target, hp.get("history_id"),
                                                    event.get("_history_detail"), hp.get("stream")))["record"]
                    if event.get("type") not in USER_EVENT_TYPES or event.get("pending_id") != pending_id:
                        continue
                    content = event.get("content")
                    if not isinstance(content, str):
                        result["readback_status"] = "mismatch"
                        return result
                    chunk = _chunk(content, offset, max_chars)
                    result["canonical_persisted"] = {
                        **chunk, "event_id": event.get("event_id"),
                        "event_key": event.get("_history_key"), "type": event["type"],
                        "pending_id": event["pending_id"], "history_id": hp.get("history_id")}
                    submitted = receipt["payload"]["message"]
                    result["readback_status"] = ("verified" if _sha(content) == _sha(submitted)
                                                 and len(content.encode("utf-8")) == len(submitted.encode("utf-8"))
                                                 else "mismatch")
                    return result
                cursor = hp.get("after" if forward else "before")
                if not hp.get("has_newer" if forward else "has_older") or not cursor or cursor in seen:
                    break
                seen.add(cursor)
                params = {"limit": 100, "stream": hp["stream"], "history_id": hp["history_id"],
                          "after" if forward else "before": cursor}
        except (ControlError, httpx.HTTPError, ValueError, KeyError) as exc:
            result["readback_status"] = "error"
            result["readback_error"] = exc.data if isinstance(exc, ControlError) else {
                "code": "readback_unavailable", "message": "Official bounded readback unavailable"}
        return result

    async def _goal_target(self, client, args):
        _segment(args["goal_id"])
        return await self._resolve(client, args["session_id"])

    @staticmethod
    def _goal_source(row):
        return {"source": "live_runtime" if row["is_live"] else "saved_viewer",
                "read_only_snapshot": not row["is_live"],
                "acl_basis": "native_allowed_actions" if row["is_live"] else "saved_viewer_read_only",
                "access_note": ("Native graph-scoped ACL and durability apply." if row["is_live"] else
                                "Persistent saved rows omit spec/presentation/metadata/evidence and expose "
                                "no live allowed actions; they do not authorize a mutation.")}

    async def _goal_rows(self, client, row, include_terminal=True):
        if row["is_live"]:
            payload = await self._http(client, "GET", f"/api/sessions/{_segment(row['runtime_id'])}/drives",
                                       params={"kind": "goal", "include_terminal": str(include_terminal).lower()})
        else:
            payload = await self._http(client, "GET", f"/api/persistence/viewer/{_segment(row['saved_name'])}/drives")
        if (not isinstance(payload, dict) or not isinstance(payload.get("drives"), list)
                or any(not isinstance(d, dict) for d in payload["drives"])):
            raise ControlError("native_protocol_error", "Invalid native Drive listing")
        return [d for d in payload["drives"] if d.get("kind") == "goal"
                and (include_terminal or d.get("status") not in TERMINAL_STATUSES)]

    async def _goals_list(self, client, args):
        row = await self._resolve(client, args["session_id"])
        goals = await self._goal_rows(client, row, args["include_terminal"])
        return {**self._identity(row), **self._goal_source(row), "goals": _redact(goals),
                "include_terminal": args["include_terminal"], "resumed": False}

    @staticmethod
    def _goal_proof(detail, goal_id):
        if (not isinstance(detail, dict) or detail.get("drive_id") != goal_id
                or detail.get("kind") != "goal"):
            raise ControlError("identity_changed", "Native existing goal identity or kind does not match")
        return detail

    async def _goal_detail(self, client, row, goal_id, action="read"):
        if not row["is_live"]:
            if action != "read":
                raise ControlError("needs_resume", "Saved viewer rows do not authorize live mutations")
            found = [d for d in await self._goal_rows(client, row) if d.get("drive_id") == goal_id]
            if not found:
                raise ControlError("goal_not_found", "Existing goal not found in saved viewer")
            if len(found) != 1:
                raise ControlError("ambiguous_identity", "Saved viewer goal identity is ambiguous")
            return self._goal_proof(found[0], goal_id)
        detail = await self._http(client, "GET", f"/api/sessions/{_segment(row['runtime_id'])}/drives/{_segment(goal_id)}")
        self._goal_proof(detail, goal_id)
        actions = detail.get("allowed_actions", [])
        if not isinstance(actions, list) or "read" not in actions or action not in actions:
            raise ControlError("native_action_denied", "Native allowed_actions does not permit requested goal action")
        return detail

    async def _goal_get(self, client, args):
        row = await self._goal_target(client, args)
        return {**self._identity(row), **self._goal_source(row),
                "goal": _redact(await self._goal_detail(client, row, args["goal_id"])), "resumed": False}

    @staticmethod
    def _effective_patch(current, patch):
        effective = copy.deepcopy(patch)
        # Native build_patch normalizes nullable collections to empty collections.
        for key in ("presentation", "metadata", "policy_options"):
            if key in effective and effective[key] is None:
                effective[key] = {}
        if "dependency_ids" in effective and effective["dependency_ids"] is None:
            effective["dependency_ids"] = []
        if "spec" in patch:
            if "objective" in patch["spec"] and (not isinstance(patch["spec"]["objective"], str)
                                                   or not patch["spec"]["objective"].strip()):
                raise ControlError("invalid_patch", "Goal objective must be nonempty plain text")
            existing = current.get("spec")
            if not isinstance(existing, dict):
                raise ControlError("native_protocol_error", "Native goal spec is missing or redacted")
            effective["spec"] = {**copy.deepcopy(existing), **copy.deepcopy(patch["spec"])}
            if "budgets" in patch["spec"]:
                budgets = patch["spec"]["budgets"]
                if not isinstance(budgets, dict):
                    raise ControlError("invalid_patch", "Goal budgets must be an object")
                existing_budgets = existing.get("budgets", {})
                if not isinstance(existing_budgets, dict):
                    raise ControlError("native_protocol_error", "Native goal budgets are not an object")
                effective["spec"]["budgets"] = {**copy.deepcopy(existing_budgets), **copy.deepcopy(budgets)}
        return effective

    @staticmethod
    def _goal_revision(current, expected):
        if type(current.get("revision")) is not int or current["revision"] != expected:
            raise ControlError("revision_conflict", "Expected revision does not match native revision",
                               expected_revision=expected, current_revision=current.get("revision"))

    def _goal_request(self, current, args, action):
        body = {"expected_revision": args["expected_revision"], "idempotency_key": "mcp_" + args["request_id"]}
        expected = {k: copy.deepcopy(current[k]) for k in GOAL_STATE_FIELDS if k in current}
        if action == "goal_update":
            effective = self._effective_patch(current, args["patch"])
            body.update(effective)
            expected.update(effective)
        else:
            body["target_status"] = args["target_status"]
            if "status_reason" in args:
                body["status_reason"] = args["status_reason"]
            expected.update(status=args["target_status"], status_reason=args.get("status_reason"))
            # The runtime determines epoch changes. Capture its returned epoch below.
            expected.pop("lifecycle_epoch", None)
        return body, expected

    async def _goal_update(self, client, args):
        return await self._goal_mutate(client, args, "goal_update")

    async def _goal_transition(self, client, args):
        return await self._goal_mutate(client, args, "goal_transition")

    async def _goal_mutate(self, client, args, action):
        row = await self._goal_target(client, args)
        rid, goal_id = args["request_id"], args["goal_id"]
        payload = {"action": action, "conversation_id": row["conversation_id"],
                   "goal_id": goal_id, "expected_revision": args["expected_revision"]}
        if action == "goal_update":
            payload["patch"] = args["patch"]
        else:
            payload.update(target_status=args["target_status"], status_reason=args.get("status_reason"))
        if not args["dry_run"]:
            prior = self._check_replay(rid, payload)
            if prior:
                return await self._goal_readback(client, prior, replayed=True)
        native_action = "update" if action == "goal_update" else "transition"
        current = await self._goal_detail(client, row, goal_id, native_action if row["is_live"] else "read")
        self._goal_revision(current, args["expected_revision"])
        preview = {**self._identity(row), "goal_id": goal_id, "request_id": rid,
                   "needs_resume": not row["is_live"], "resumed": False,
                   "would_resume": not row["is_live"] and args["resume_if_paused"]}
        body, expected = self._goal_request(current, args, action) if row["is_live"] else (None, None)
        if args["dry_run"]:
            return {**preview, "dry_run": True, "effective_request": body,
                    "planned_mutation": payload, "live_validation_required": not row["is_live"]}
        if not row["is_live"] and not args["resume_if_paused"]:
            return {**preview, "state": "needs_resume", "accepted": False, "mutation_verified": False}
        context = {"identity": self._identity(row), "resumed": False,
                   "original_identity": {k: copy.deepcopy(current[k]) for k in IDENTITY_FIELDS if k in current},
                   "expected_fields": expected, "effective_request": body}
        receipt, owned = self._reserve(rid, payload, context)
        if not owned:
            return await self._goal_readback(client, receipt, replayed=True)
        attempted, resume_attempted = False, False
        try:
            if not row["is_live"]:
                self._record(rid, "resuming", context=context)
                resume_attempted = True
                await self._http(client, "POST", f"/api/sessions/{_segment(row['saved_name'])}/resume", json={})
                resumed = await self._resolve(client, row["conversation_id"])
                if (resumed["conversation_id"] != row["conversation_id"]
                        or resumed["saved_name"] != row["saved_name"] or not resumed["is_live"]):
                    raise ControlError("identity_changed", "Resume did not yield the same saved conversation")
                row = resumed
                context.update(identity=self._identity(row), resumed=True)
                self._record(rid, "resuming", context=context)
                current = await self._goal_detail(client, row, goal_id, native_action)
                changed = [k for k, v in context["original_identity"].items() if current.get(k) != v]
                if changed:
                    raise ControlError("identity_changed", "Resume changed the existing goal identity",
                                       changed_identity_fields=changed)
                self._goal_revision(current, args["expected_revision"])
                body, expected = self._goal_request(current, args, action)
                context.update(expected_fields=expected, effective_request=body)
            self._record(rid, "sending", context=context)
            path = f"/api/sessions/{_segment(row['runtime_id'])}/drives/{_segment(goal_id)}"
            if action == "goal_transition":
                path += "/transition"
            attempted = True
            response = await self._http(client, "PATCH" if action == "goal_update" else "POST", path, json=body)
            self._goal_proof(response, goal_id)
            if action == "goal_transition" and "lifecycle_epoch" in response:
                context["expected_fields"]["lifecycle_epoch"] = response["lifecycle_epoch"]
            # Scheduling fields can be normalized, but this native version omits them
            # (and policy_options) from DriveView. Missing readback is never verification.
            for key in ("not_before", "expires_at"):
                if key in context["expected_fields"] and key in response:
                    context["expected_fields"][key] = response[key]
            self._record(rid, "accepted", context=context, result={"native_response": _redact(response)})
        except asyncio.CancelledError:
            self._record(rid, "uncertain" if attempted or resume_attempted else "rejected", context=context)
            raise
        except Exception as exc:
            status = exc.data.get("http_status", 0) if isinstance(exc, ControlError) else 0
            denied = 400 <= status < 500 and status != 408
            # A proven failure before the Drive mutation cannot verify it. A lost
            # resume/mutation response remains uncertain and is never automatically retried.
            state = "rejected" if denied or (not attempted and isinstance(exc, ControlError)) else "uncertain"
            self._record(rid, state, context=context, result={"error": exc.data if isinstance(exc, ControlError) else {
                "code": "mutation_uncertain", "message": "Mutation outcome unknown; this request_id will never be replayed"}})
        return await self._goal_readback(client, self._receipt(rid))

    async def _goal_readback(self, client, receipt, replayed=False):
        result = {**self._receipt_summary(receipt), "replayed": replayed,
                  "goal_id": receipt["payload"]["goal_id"], "accepted": receipt["state"] == "accepted",
                  "mutation_verified": False, "readback_status": "inconclusive"}
        try:
            row = await self._goal_target(client, {"session_id": receipt["payload"]["conversation_id"],
                                                  "goal_id": receipt["payload"]["goal_id"]})
            if row["saved_name"] != receipt["context"]["identity"]["saved_name"]:
                raise ControlError("identity_changed", "Canonical saved conversation changed")
            goal = await self._goal_detail(client, row, receipt["payload"]["goal_id"])
            context = receipt["context"]
            # Preserve old ledger readback while no longer treating editable/state
            # fields as immutable identity.
            expected = context.get("expected_fields")
            if expected is None:
                expected = context.get("effective_patch", {})
            missing = [k for k in expected if k not in goal]
            changed = [k for k, v in expected.items() if k in goal and goal[k] != v]
            identity_changed = [k for k, v in context.get("original_identity", {}).items()
                                if k in IDENTITY_FIELDS and (k not in goal or goal[k] != v)]
            result.update(current_identity=self._identity(row), **self._goal_source(row), goal=_redact(goal),
                          revision=goal.get("revision"), patched_fields_match=not changed and not missing,
                          original_identity_preserved=not identity_changed, mismatched_fields=changed,
                          unobservable_fields=missing, changed_identity_fields=identity_changed)
            revision = goal.get("revision")
            target_revision = receipt["payload"]["expected_revision"] + 1
            if identity_changed:
                result["readback_status"] = "mismatch"
            elif result["accepted"]:
                if type(revision) is int and revision > target_revision:
                    result["readback_status"] = "superseded"
                elif changed or type(revision) is not int or revision != target_revision:
                    result["readback_status"] = "mismatch"
                elif not missing and expected:
                    result["readback_status"] = "verified"
                    result["mutation_verified"] = True
            # Rejected, reserved and uncertain receipts never become verified solely
            # because another actor happened to produce matching fields/revision.
        except (ControlError, httpx.HTTPError) as exc:
            result["readback_status"] = "error"
            result["readback_error"] = exc.data if isinstance(exc, ControlError) else {
                "code": "readback_unavailable", "message": "Official goal readback unavailable"}
        return result
