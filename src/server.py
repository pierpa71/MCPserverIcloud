"""
iCloud Mail MCP server
======================

MCP server that gives Claude access to an iCloud Mail mailbox via IMAP/SMTP.

Transports:
  - http  (default): Streamable HTTP, for Docker / remote use as a custom connector
  - stdio:           for local use (Claude Desktop, Claude Code)

All configuration comes from environment variables (see .env.example).
"""

from __future__ import annotations

import base64
import contextlib
import email
import hmac
import imaplib
import io
import logging
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import date, datetime
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parsedate_to_datetime, formatdate
from html.parser import HTMLParser
from typing import Any, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

import imap_utf7

log = logging.getLogger("icloud-mail-mcp")

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on", "si", "sì")


def _read_secret(name: str) -> str:
    """Read NAME, or the file pointed to by NAME_FILE (Docker secrets)."""
    path = os.getenv(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    return (os.getenv(name) or "").strip()


ICLOUD_EMAIL = os.getenv("ICLOUD_EMAIL", "").strip()
ICLOUD_APP_PASSWORD = _read_secret("ICLOUD_APP_PASSWORD")
FROM_ADDRESS = os.getenv("ICLOUD_FROM_ADDRESS", "").strip() or ICLOUD_EMAIL
FROM_NAME = os.getenv("ICLOUD_FROM_NAME", "").strip()

IMAP_HOST = os.getenv("ICLOUD_IMAP_HOST", "imap.mail.me.com")
IMAP_PORT = int(os.getenv("ICLOUD_IMAP_PORT", "993"))
SMTP_HOST = os.getenv("ICLOUD_SMTP_HOST", "smtp.mail.me.com")
SMTP_PORT = int(os.getenv("ICLOUD_SMTP_PORT", "587"))
NET_TIMEOUT = int(os.getenv("ICLOUD_TIMEOUT", "30"))

FOLDER_DRAFTS = os.getenv("ICLOUD_DRAFTS_FOLDER", "Drafts")
FOLDER_SENT = os.getenv("ICLOUD_SENT_FOLDER", "Sent Messages")
FOLDER_TRASH = os.getenv("ICLOUD_TRASH_FOLDER", "Deleted Messages")

READ_ONLY = _env_bool("ICLOUD_READ_ONLY", False)
ALLOW_SEND = _env_bool("ICLOUD_ALLOW_SEND", False)
SAVE_SENT = _env_bool("ICLOUD_SAVE_SENT", True)

MAX_BODY_CHARS = int(os.getenv("ICLOUD_MAX_BODY_CHARS", "30000"))
MAX_ATTACHMENT_BYTES = int(os.getenv("ICLOUD_MAX_ATTACHMENT_BYTES", str(8 * 1024 * 1024)))
MAX_SEARCH_RESULTS = 100

TRANSPORT = os.getenv("MCP_TRANSPORT", "http").strip().lower()
HTTP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("MCP_PORT", "8000"))
BEARER_TOKEN = _read_secret("MCP_BEARER_TOKEN")
PATH_SECRET = _read_secret("MCP_PATH_SECRET").strip("/")
ALLOW_NO_AUTH = _env_bool("MCP_ALLOW_NO_AUTH", False)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

MCP_PATH = f"/{PATH_SECRET}/mcp" if PATH_SECRET else "/mcp"

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _q(s: str) -> str:
    """IMAP quoted string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mbox(folder: str) -> str:
    return _q(imap_utf7.encode(folder))


def _check(typ: str, data: Any, what: str) -> Any:
    if typ != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace") if data else ""
        raise ToolError(f"IMAP {what} failed: {detail or typ}")
    return data


@contextlib.contextmanager
def imap_session(folder: Optional[str] = None, readonly: bool = True):
    if not ICLOUD_EMAIL or not ICLOUD_APP_PASSWORD:
        raise ToolError("ICLOUD_EMAIL / ICLOUD_APP_PASSWORD are not configured on the server.")
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=NET_TIMEOUT,
                                 ssl_context=ssl.create_default_context())
    except OSError as e:
        raise ToolError(f"Cannot connect to {IMAP_HOST}:{IMAP_PORT}: {e}") from e
    try:
        try:
            conn.login(ICLOUD_EMAIL, ICLOUD_APP_PASSWORD)
        except imaplib.IMAP4.error as e:
            raise ToolError(
                "iCloud login failed. Check ICLOUD_EMAIL and that ICLOUD_APP_PASSWORD is an "
                f"app-specific password (appleid.apple.com). Server said: {e}"
            ) from e
        if folder:
            typ, data = conn.select(_mbox(folder), readonly=readonly)
            if typ != "OK":
                raise ToolError(f"Folder not found or not selectable: {folder!r}. Use list_folders.")
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


def _imap_date(d: str) -> str:
    try:
        dt = date.fromisoformat(d)
    except ValueError as e:
        raise ToolError(f"Invalid date {d!r}; use YYYY-MM-DD.") from e
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{dt.day:02d}-{months[dt.month - 1]}-{dt.year}"


def _iso_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except Exception:
        return str(raw)


def _addr_list(value: Any) -> list[str]:
    if not value:
        return []
    return [formataddr((n, a)) if n else a for n, a in getaddresses([str(value)]) if a]


def _uid_set(uids: list[int]) -> str:
    if not uids:
        raise ToolError("No UIDs given.")
    return ",".join(str(int(u)) for u in uids)


_META_RE = {
    "uid": re.compile(rb"UID (\d+)"),
    "flags": re.compile(rb"FLAGS \(([^)]*)\)"),
    "size": re.compile(rb"RFC822\.SIZE (\d+)"),
}


def _fetch_parts(data: list) -> list[tuple[bytes, bytes]]:
    """Return (meta, literal) pairs from an imaplib FETCH response."""
    return [(item[0], item[1]) for item in data if isinstance(item, tuple) and len(item) == 2]


def _meta(meta: bytes) -> dict:
    uid = _META_RE["uid"].search(meta)
    flags = _META_RE["flags"].search(meta)
    size = _META_RE["size"].search(meta)
    flag_list = flags.group(1).decode().split() if flags else []
    return {
        "uid": int(uid.group(1)) if uid else None,
        "flags": flag_list,
        "unread": "\\Seen" not in flag_list,
        "flagged": "\\Flagged" in flag_list,
        "size_bytes": int(size.group(1)) if size else None,
    }


class _HTMLToText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "td":
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)

    def text(self) -> str:
        t = "".join(self.out).replace("\xa0", " ")
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


def html_to_text(html: str) -> str:
    p = _HTMLToText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return p.text()


def _body_text(msg: EmailMessage) -> tuple[str, str]:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", "none"
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    content = content.replace("\r\n", "\n")
    if part.get_content_subtype() == "html":
        return html_to_text(content), "html"
    return content.strip(), "plain"


def _attachment_parts(msg: EmailMessage) -> list[EmailMessage]:
    body = msg.get_body(preferencelist=("plain", "html"))
    parts = []
    for part in msg.walk():
        if part.is_multipart() or part is body:
            continue
        disp = part.get_content_disposition()
        if disp == "attachment" or part.get_filename():
            parts.append(part)
    return parts


def _fetch_message(conn: imaplib.IMAP4, uid: int) -> tuple[EmailMessage, dict]:
    typ, data = conn.uid("FETCH", str(int(uid)), "(UID FLAGS RFC822.SIZE BODY.PEEK[])")
    _check(typ, data, "FETCH")
    parts = _fetch_parts(data)
    if not parts:
        raise ToolError(f"Message UID {uid} not found in this folder.")
    meta, raw = parts[0]
    msg = email.message_from_bytes(raw, policy=policy.default)
    info = _meta(meta)
    info["uid"] = info["uid"] or int(uid)
    return msg, info


# --------------------------------------------------------------------------------------
# Blocking implementations (run in worker threads)
# --------------------------------------------------------------------------------------

_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')


def _list_folders(with_counts: bool) -> list[dict]:
    with imap_session() as conn:
        typ, data = conn.list()
        _check(typ, data, "LIST")
        folders = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            m = _LIST_RE.match(line.decode(errors="replace"))
            if not m:
                continue
            raw = m.group("name").strip()
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            flags = m.group("flags").split()
            entry: dict[str, Any] = {"name": imap_utf7.decode(raw), "flags": flags}
            if with_counts and "\\Noselect" not in flags:
                typ2, st = conn.status(_q(raw), "(MESSAGES UNSEEN)")
                if typ2 == "OK" and st and st[0]:
                    s = st[0].decode(errors="replace")
                    mm = re.search(r"MESSAGES (\d+)", s)
                    mu = re.search(r"UNSEEN (\d+)", s)
                    entry["messages"] = int(mm.group(1)) if mm else None
                    entry["unread"] = int(mu.group(1)) if mu else None
            folders.append(entry)
        return folders


def _search(folder, text, from_addr, to_addr, subject, since, before, unread_only, flagged_only, limit, offset):
    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    offset = max(0, int(offset))

    criteria: list[str] = []
    if since:
        criteria += ["SINCE", _imap_date(since)]
    if before:
        criteria += ["BEFORE", _imap_date(before)]
    if unread_only:
        criteria.append("UNSEEN")
    if flagged_only:
        criteria.append("FLAGGED")

    # ASCII terms go to the server as quoted strings. imaplib supports a single literal per
    # command (always sent last), so the first non-ASCII term uses it and any further
    # non-ASCII header terms are filtered client-side. TEXT comes first so it gets the slot.
    literal: Optional[bytes] = None
    literal_key: Optional[str] = None
    client_filters: list[tuple[str, str]] = []
    for key, value, header in (("TEXT", text, None), ("FROM", from_addr, "from"),
                               ("TO", to_addr, "to"), ("SUBJECT", subject, "subject")):
        if not value:
            continue
        if value.isascii():
            criteria += [key, _q(value)]
        elif literal is None:
            literal, literal_key = value.encode("utf-8"), key
        elif header:
            client_filters.append((header, value.lower()))
        else:
            raise ToolError("Only one non-ASCII search term with full text is supported.")
    if literal_key:
        criteria.append(literal_key)

    with imap_session(folder) as conn:
        if literal is not None:
            conn.literal = literal
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria)
        else:
            typ, data = conn.uid("SEARCH", *(criteria or ["ALL"]))
        _check(typ, data, "SEARCH")
        uids = sorted(int(x) for x in (data[0] or b"").split())
        total = len(uids)
        uids.reverse()  # newest first

        results: list[dict] = []
        cursor = offset
        # Fetch in pages so client-side filters still fill `limit` when possible.
        while len(results) < limit and cursor < len(uids):
            page = uids[cursor: cursor + max(limit, 25)]
            cursor += len(page)
            typ, data = conn.uid(
                "FETCH", ",".join(map(str, page)),
                "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID CONTENT-TYPE)])",
            )
            _check(typ, data, "FETCH")
            by_uid = {}
            for meta, hdr in _fetch_parts(data):
                info = _meta(meta)
                h = email.message_from_bytes(hdr, policy=policy.default)
                ctype = (h.get("Content-Type") or "").lower()
                info.update({
                    "date": _iso_date(h.get("Date")),
                    "from": str(h.get("From") or ""),
                    "to": _addr_list(h.get("To")),
                    "cc": _addr_list(h.get("Cc")),
                    "subject": str(h.get("Subject") or ""),
                    "probably_has_attachments": "multipart/mixed" in ctype,
                })
                if all(needle in str(info.get(hdr_name) or "").lower() for hdr_name, needle in client_filters):
                    by_uid[info["uid"]] = info
            for u in page:
                if u in by_uid and len(results) < limit:
                    results.append(by_uid[u])

        return {
            "folder": folder,
            "total_matches": total if not client_filters else None,
            "returned": len(results),
            "offset": offset,
            "next_offset": cursor if cursor < len(uids) else None,
            "messages": results,
        }


def _read(folder: str, uid: int, mark_as_read: bool, max_chars: int) -> dict:
    with imap_session(folder, readonly=not mark_as_read) as conn:
        msg, info = _fetch_message(conn, uid)
        if mark_as_read and not READ_ONLY:
            conn.uid("STORE", str(int(uid)), "+FLAGS", "(\\Seen)")
    body, fmt = _body_text(msg)
    max_chars = max(500, min(int(max_chars), 200_000))
    truncated = len(body) > max_chars
    attachments = []
    for i, part in enumerate(_attachment_parts(msg)):
        payload = part.get_payload(decode=True) or b""
        attachments.append({
            "index": i,
            "filename": part.get_filename() or f"attachment-{i}",
            "content_type": part.get_content_type(),
            "size_bytes": len(payload),
            "inline": part.get_content_disposition() == "inline",
        })
    return {
        **info,
        "folder": folder,
        "message_id": str(msg.get("Message-ID") or ""),
        "date": _iso_date(msg.get("Date")),
        "from": str(msg.get("From") or ""),
        "reply_to": _addr_list(msg.get("Reply-To")),
        "to": _addr_list(msg.get("To")),
        "cc": _addr_list(msg.get("Cc")),
        "subject": str(msg.get("Subject") or ""),
        "body_format": fmt,
        "body": body[:max_chars],
        "body_truncated": truncated,
        "attachments": attachments,
    }


def _get_attachment(folder: str, uid: int, index: int):
    with imap_session(folder) as conn:
        msg, _ = _fetch_message(conn, uid)
    parts = _attachment_parts(msg)
    if not 0 <= index < len(parts):
        raise ToolError(f"Attachment index {index} out of range (message has {len(parts)}).")
    part = parts[index]
    data = part.get_payload(decode=True) or b""
    name = part.get_filename() or f"attachment-{index}"
    ctype = part.get_content_type()
    header = {"filename": name, "content_type": ctype, "size_bytes": len(data)}

    if ctype.startswith("image/") and len(data) <= MAX_ATTACHMENT_BYTES:
        fmt = ctype.split("/", 1)[1].replace("jpg", "jpeg")
        if fmt in ("png", "jpeg", "gif", "webp"):
            return Image(data=data, format=fmt)

    textual = ctype.startswith("text/") or ctype in (
        "application/json", "application/xml", "application/csv", "application/x-yaml",
    ) or name.lower().endswith((".txt", ".csv", ".json", ".xml", ".md", ".log", ".ics", ".eml", ".yaml", ".yml"))
    if textual:
        charset = part.get_content_charset() or "utf-8"
        text = data.decode(charset, errors="replace")
        if ctype == "text/html":
            text = html_to_text(text)
        return {**header, "text": text[:MAX_BODY_CHARS * 3], "truncated": len(text) > MAX_BODY_CHARS * 3}

    if ctype == "application/pdf" or name.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages]
            text = "\n\n".join(f"--- page {i + 1} ---\n{t}" for i, t in enumerate(pages))
            if text.strip():
                limit = MAX_BODY_CHARS * 3
                return {**header, "pages": len(pages), "text": text[:limit], "truncated": len(text) > limit}
        except Exception as e:  # fall through to base64
            log.warning("PDF text extraction failed: %s", e)

    if ctype == "message/rfc822":
        inner = part.get_payload(0) if part.is_multipart() else email.message_from_bytes(data, policy=policy.default)
        body, _ = _body_text(inner)
        return {**header, "subject": str(inner.get("Subject") or ""), "from": str(inner.get("From") or ""),
                "date": _iso_date(inner.get("Date")), "text": body[:MAX_BODY_CHARS]}

    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ToolError(f"Attachment is {len(data)} bytes, above ICLOUD_MAX_ATTACHMENT_BYTES ({MAX_ATTACHMENT_BYTES}).")
    return {**header, "encoding": "base64", "data": base64.b64encode(data).decode()}


def _build_message(to, subject, body, cc, bcc, html, reply_to_uid, reply_folder, quote_original):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = formataddr((FROM_NAME, FROM_ADDRESS)) if FROM_NAME else FROM_ADDRESS
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(FROM_ADDRESS.split("@")[-1] or None))

    quoted = ""
    if reply_to_uid is not None:
        with imap_session(reply_folder) as conn:
            orig, _ = _fetch_message(conn, reply_to_uid)
        orig_id = str(orig.get("Message-ID") or "").strip()
        refs = str(orig.get("References") or "").strip()
        if orig_id:
            msg["In-Reply-To"] = orig_id
            msg["References"] = f"{refs} {orig_id}".strip()
        if not subject:
            s = str(orig.get("Subject") or "")
            subject = s if re.match(r"^\s*(re|r)\s*:", s, re.I) else f"Re: {s}"
        if not to:
            to = _addr_list(orig.get("Reply-To")) or _addr_list(orig.get("From"))
        if quote_original:
            obody, _ = _body_text(orig)
            when = str(orig.get("Date") or "")
            who = str(orig.get("From") or "")
            quoted = f"\n\n{when}, {who} ha scritto:\n" + "\n".join("> " + ln for ln in obody.splitlines())

    if not to:
        raise ToolError("At least one recipient ('to') is required.")
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject or ""

    if html:
        plain = html_to_text(body) + quoted
        msg.set_content(plain)
        quoted_html = ""
        if quoted:
            esc = quoted.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            quoted_html = f"<pre style='white-space:pre-wrap'>{esc}</pre>"
        msg.add_alternative(f"<html><body>{body}{quoted_html}</body></html>", subtype="html")
    else:
        msg.set_content(body + quoted)
    return msg


def _append(folder: str, msg: EmailMessage, flags: str) -> None:
    with imap_session() as conn:
        typ, data = conn.append(_mbox(folder), flags, imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        _check(typ, data, f"APPEND to {folder}")


def _create_draft(**kw) -> dict:
    msg = _build_message(**kw)
    _append(FOLDER_DRAFTS, msg, "(\\Draft \\Seen)")
    return {"status": "draft_saved", "folder": FOLDER_DRAFTS, "message_id": msg["Message-ID"],
            "to": msg["To"], "subject": msg["Subject"]}


def _send(**kw) -> dict:
    msg = _build_message(**kw)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=NET_TIMEOUT) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(ICLOUD_EMAIL, ICLOUD_APP_PASSWORD)
            s.send_message(msg)  # strips Bcc from the transmitted copy
    except smtplib.SMTPException as e:
        raise ToolError(f"SMTP send failed: {e}") from e
    saved = False
    if SAVE_SENT:
        try:
            _append(FOLDER_SENT, msg, "(\\Seen)")
            saved = True
        except Exception as e:
            log.warning("Sent OK but could not save copy to %s: %s", FOLDER_SENT, e)
    return {"status": "sent", "message_id": msg["Message-ID"], "to": msg["To"],
            "subject": msg["Subject"], "saved_to_sent": saved}


def _move(folder: str, uids: list[int], destination: str) -> dict:
    uidset = _uid_set(uids)
    with imap_session(folder, readonly=False) as conn:
        caps = {c.upper() for c in conn.capabilities}
        if "MOVE" in caps:
            typ, data = conn.uid("MOVE", uidset, _mbox(destination))
            _check(typ, data, "MOVE")
        else:
            typ, data = conn.uid("COPY", uidset, _mbox(destination))
            _check(typ, data, "COPY")
            conn.uid("STORE", uidset, "+FLAGS.SILENT", "(\\Deleted)")
            if "UIDPLUS" in caps:
                conn.uid("EXPUNGE", uidset)
            else:
                conn.expunge()
    return {"status": "moved", "uids": uids, "from": folder, "to": destination}


def _set_flags(folder: str, uids: list[int], seen: Optional[bool], flagged: Optional[bool]) -> dict:
    uidset = _uid_set(uids)
    changes = []
    with imap_session(folder, readonly=False) as conn:
        for flag, value in (("\\Seen", seen), ("\\Flagged", flagged)):
            if value is None:
                continue
            typ, data = conn.uid("STORE", uidset, "+FLAGS.SILENT" if value else "-FLAGS.SILENT", f"({flag})")
            _check(typ, data, "STORE")
            changes.append(f"{'+' if value else '-'}{flag}")
    return {"status": "ok", "uids": uids, "folder": folder, "changes": changes}


# --------------------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------------------

mcp = FastMCP(
    name="icloud-mail",
    instructions=(
        "Access to the user's iCloud Mail mailbox. Typical flow: list_folders -> search_emails "
        "(newest first) -> read_email by UID -> get_attachment by index. UIDs are per-folder, so "
        "always pass the same folder you searched. Standard iCloud folders: INBOX, Drafts, "
        "'Sent Messages', Archive, Junk, 'Deleted Messages'. Prefer create_draft over send_email "
        "unless the user explicitly asked to send."
    ),
    host=HTTP_HOST,
    port=HTTP_PORT,
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    json_response=True,
    log_level=LOG_LEVEL if LOG_LEVEL in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO",
    # Runs behind Docker / reverse proxies with arbitrary Host headers; auth is enforced below.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def list_folders(with_counts: bool = True) -> dict:
    """List all mailbox folders, optionally with total and unread message counts."""
    return {"folders": await anyio.to_thread.run_sync(_list_folders, with_counts)}


@mcp.tool()
async def search_emails(
    folder: str = "INBOX",
    text: Optional[str] = None,
    from_addr: Optional[str] = None,
    to_addr: Optional[str] = None,
    subject: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search messages in a folder, newest first. Returns headers only (uid, date, from, to,
    subject, flags). All filters are optional and combined with AND.

    Args:
        folder: Folder name (e.g. INBOX, "Sent Messages", Archive).
        text: Free text searched in headers and body.
        from_addr / to_addr / subject: Substring match on those headers.
        since: Only messages on/after this date (YYYY-MM-DD).
        before: Only messages before this date (YYYY-MM-DD).
        unread_only / flagged_only: Restrict to unread / flagged messages.
        limit: Max results (1-100). offset: Skip this many (use next_offset to page).
    """
    return await anyio.to_thread.run_sync(
        _search, folder, text, from_addr, to_addr, subject, since, before,
        unread_only, flagged_only, limit, offset,
    )


@mcp.tool()
async def read_email(uid: int, folder: str = "INBOX", mark_as_read: bool = False,
                     max_chars: int = MAX_BODY_CHARS) -> dict:
    """Read a full message by UID: headers, body as text (HTML is converted) and the list of
    attachments (use their index with get_attachment). Does not mark as read unless asked."""
    return await anyio.to_thread.run_sync(_read, folder, uid, mark_as_read, max_chars)


@mcp.tool()
async def get_attachment(uid: int, index: int, folder: str = "INBOX"):
    """Get one attachment of a message. Text, CSV, HTML and PDF attachments are returned as
    extracted text; images are returned as images; other files as base64."""
    return await anyio.to_thread.run_sync(_get_attachment, folder, uid, index)


if not READ_ONLY:

    @mcp.tool()
    async def create_draft(
        to: Optional[list[str]] = None,
        subject: str = "",
        body: str = "",
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html: bool = False,
        reply_to_uid: Optional[int] = None,
        reply_folder: str = "INBOX",
        quote_original: bool = True,
    ) -> dict:
        """Save a draft in the Drafts folder (it appears in Mail on all the user's devices, ready
        to review and send). For a reply, pass reply_to_uid (+ reply_folder): threading headers
        are set, and 'to'/'subject' default to the original sender and 'Re: <subject>'.
        Set html=true if body is HTML."""
        return await anyio.to_thread.run_sync(lambda: _create_draft(
            to=to or [], subject=subject, body=body, cc=cc or [], bcc=bcc or [], html=html,
            reply_to_uid=reply_to_uid, reply_folder=reply_folder, quote_original=quote_original))

    @mcp.tool()
    async def move_email(uids: list[int], destination: str, folder: str = "INBOX") -> dict:
        """Move one or more messages (by UID) from `folder` to `destination` (e.g. Archive)."""
        return await anyio.to_thread.run_sync(_move, folder, uids, destination)

    @mcp.tool()
    async def trash_email(uids: list[int], folder: str = "INBOX") -> dict:
        """Move messages to the iCloud trash ('Deleted Messages'). Recoverable; no permanent delete."""
        return await anyio.to_thread.run_sync(_move, folder, uids, FOLDER_TRASH)

    @mcp.tool()
    async def set_flags(uids: list[int], folder: str = "INBOX", seen: Optional[bool] = None,
                        flagged: Optional[bool] = None) -> dict:
        """Mark messages read/unread (seen) and/or flagged/unflagged. Omit a flag to leave it unchanged."""
        return await anyio.to_thread.run_sync(_set_flags, folder, uids, seen, flagged)

    if ALLOW_SEND:

        @mcp.tool()
        async def send_email(
            to: Optional[list[str]] = None,
            subject: str = "",
            body: str = "",
            cc: Optional[list[str]] = None,
            bcc: Optional[list[str]] = None,
            html: bool = False,
            reply_to_uid: Optional[int] = None,
            reply_folder: str = "INBOX",
            quote_original: bool = True,
        ) -> dict:
            """SEND an email immediately via iCloud SMTP. Only use when the user explicitly asked
            to send (otherwise use create_draft). Same parameters as create_draft."""
            return await anyio.to_thread.run_sync(lambda: _send(
                to=to or [], subject=subject, body=body, cc=cc or [], bcc=bcc or [], html=html,
                reply_to_uid=reply_to_uid, reply_folder=reply_folder, quote_original=quote_original))


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "read_only": READ_ONLY, "send_enabled": ALLOW_SEND and not READ_ONLY})


class BearerAuthMiddleware:
    """Requires 'Authorization: Bearer <MCP_BEARER_TOKEN>' on every HTTP request except /health."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    logging.basicConfig(level=LOG_LEVEL, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not ICLOUD_EMAIL or not ICLOUD_APP_PASSWORD:
        log.error("ICLOUD_EMAIL and ICLOUD_APP_PASSWORD must be set.")
        sys.exit(2)

    mode = "read-only" if READ_ONLY else ("read/write + send" if ALLOW_SEND else "read/write (drafts, no send)")
    log.info("iCloud Mail MCP for %s — %s", ICLOUD_EMAIL, mode)

    if TRANSPORT == "stdio":
        mcp.run("stdio")
        return

    if not (BEARER_TOKEN or PATH_SECRET or ALLOW_NO_AUTH):
        log.error("Refusing to start HTTP without auth: set MCP_PATH_SECRET and/or MCP_BEARER_TOKEN "
                  "(or MCP_ALLOW_NO_AUTH=true for a trusted local network only).")
        sys.exit(2)

    import uvicorn

    app = mcp.streamable_http_app()
    if BEARER_TOKEN:
        app = BearerAuthMiddleware(app, BEARER_TOKEN)
    log.info("Listening on http://%s:%d%s (bearer auth: %s)", HTTP_HOST, HTTP_PORT,
             "/<secret>/mcp" if PATH_SECRET else "/mcp", "on" if BEARER_TOKEN else "off")
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, proxy_headers=True,
                forwarded_allow_ips="*", log_level=LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
