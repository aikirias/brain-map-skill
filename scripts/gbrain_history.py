#!/usr/bin/env python3
"""
gbrain_history — optional, read-only GBrain adapter for brain-map.

Markdown exports carry `created` and (sometimes) an update-ish frontmatter
field, but not GBrain's own semantic `updated_at` and not its revision
history. A vault that was re-exported, re-cloned or rsynced yesterday looks
uniformly *fresh* if you trust file mtimes, and uniformly *stale* if the
export never wrote an update field. This adapter reads the real numbers back
out of the brain — read-only, batched, and entirely optional.

Transport
---------
One `gbrain serve` (MCP stdio) child process for the whole build, spoken to
over JSON-RPC 2.0. That is the documented machine-readable surface
(`gbrain --help`: "serve  MCP server (stdio)"; tool schemas are discoverable
with `gbrain --tools-json`), and it is what lets us issue N reads without N
subprocesses.

Only two tools are ever called, both `scope: 'read'`:

  list_pages   {limit, sort, updated_after, source_id} -> [{slug, source_id,
               type, title, updated_at}]                  (batched, paged)
  get_versions {slug} -> [{id, page_id, compiled_truth, frontmatter,
               snapshot_at}]                              (one page per call)

`get_versions` has no `source_id` parameter: it takes a bare slug and answers
with the union of every source that holds it. History is therefore only ever
read for slugs that are provably unique brain-wide. A federated read proves
that from the page index it already has; a concrete `--gbrain-source` cannot —
the other sources' rows were filtered out of `list_pages` before they arrived —
so one extra read-only `list_pages source_id='__all__'` walk is made purely to
count collisions, and its rows enrich nothing. A collision check that could not
cover the whole brain proves nothing about any slug, and then no history is
read at all.

Nothing else is invoked. `put_page`, `revert_version`, `delete_page` and the
rest of the write surface are never sent, and when history is not requested
the child is started with `--surface starter`, a catalog that does not even
contain a revert op.

MCP stdio frames one JSON-RPC message per line and offers no chunked or
streaming read, so a `get_versions` reply — which carries the body of every
past revision of a page — is necessarily decoded whole before anything can be
done with it. It is then projected to timestamps immediately after decoding
(`summarize_versions`), and only counts and stamps outlive the call. To keep
"whole" bounded, the reader caps a single reply at `MAX_REPLY_CHARS` and
throws away anything larger without ever buffering it, so one runaway page
degrades to "history unavailable for this page" instead of to a heap the size
of the brain.

What the timestamps mean
------------------------
`pages.updated_at` is bumped by every content write, but *also* by a few
non-content touches (the contextual-retrieval re-embed stamp, a slug rename,
a revert). Reads, recalls, searches, `gbrain export` and a git checkout never
touch it, and `gbrain sync` skips files whose `content_hash` is unchanged —
so a re-export or a fresh clone does not make a page look updated.

`page_versions` rows are only ever inserted by a write to an *existing* page
(`createVersion` fires just before the upsert), so each row is exactly one
semantic revision. That makes the two signals complementary:

  * versions present, `updated_at` == last `snapshot_at`  -> the last touch
    was a real content write; `updated_at` is semantic and *verified*.
  * versions present, `updated_at` >  last `snapshot_at`  -> something
    non-semantic touched the row afterwards; the last real revision is the
    snapshot, and that is what freshness should use.
  * versions present, `updated_at` <  last `snapshot_at`  -> impossible in the
    write path; the two numbers contradict each other and neither confirms
    the other, so nothing is called verified.
  * no versions -> the page has been written exactly once; `updated_at` is
    that single write, but a later non-semantic bump cannot be ruled out.

See docs/gbrain-history.md for the full diff/replay semantics.

Everything here is stdlib-only and deterministic: same brain state in, same
bytes out. Failures raise `GBrainUnavailable`; the caller decides whether to
fail open (the default) or hard-fail.
"""
import collections
import datetime
import json
import queue
import re
import subprocess
import threading
import time

DEFAULT_COMMAND = "gbrain"
DEFAULT_TIMEOUT = 20.0

# MCP protocol revision advertised in `initialize`. gbrain 0.48 answers with
# the same string; a server that negotiates a different one still works — we
# only use `tools/call`, which is stable across every revision that has it.
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "brain-map", "version": "1"}

# `list_pages` caps non-local callers at 100 rows per call, and an MCP client
# is such a caller. Paging is the documented workaround (see the tool's own
# description: sort=updated_asc + updated_after=<last row's updated_at>).
LIST_PAGE_SIZE = 100
MAX_LIST_CALLS = 2000          # 200k pages, then we stop asking

# The scope token that means "every source". Anything else in --gbrain-source
# is one concrete source and is checked against the rows that come back.
FEDERATED_SOURCE = "__all__"

# Ceiling on a single JSON-RPC reply, in characters. MCP gives us no way to
# stream one, so this is the honest bound: read at most this much of a line,
# discard the rest unread, and fail that one call. 16 MiB is far above any real
# page's history (a 100 KB page revised 50 times is ~5 MB) and far below a heap
# that matters.
MAX_REPLY_CHARS = 16 * 1024 * 1024

# `get_versions` lives in the full tool catalog; the ~20-op "starter" surface
# does not carry it. Ask for the smallest surface that answers the question.
SURFACE_PAGES_ONLY = "starter"
SURFACE_WITH_HISTORY = "full"

# `createVersion` and the page upsert are separate statements and can land in
# separate transactions, so a single logical write leaves `snapshot_at` a few
# hundred milliseconds before `updated_at`. Anything inside this window is the
# same write; anything past it is a later, non-semantic touch.
SEMANTIC_SKEW_SECONDS = 5.0

# Freshness provenance keys. build_map renders these in the inspector.
SOURCE_HISTORY = "gbrain_history"     # updated_at confirmed by a version snapshot
SOURCE_REVISION = "gbrain_revision"   # updated_at was non-semantic; snapshot used
SOURCE_UPDATED = "gbrain_updated"     # backend updated_at, unconfirmed by history

MONTH_RE = re.compile(r"^\d{4}-\d{2}")


class GBrainUnavailable(Exception):
    """The adapter as a whole cannot be used (spawn, handshake, transport)."""


class GBrainToolError(Exception):
    """One read failed or came back malformed; other reads may still work."""


# ── timestamps ───────────────────────────────────────────────────────────────
def parse_stamp(value):
    """Parse a backend timestamp into an aware UTC datetime, or None.

    GBrain hands back JSON `Date`s as ISO-8601 with a `Z` suffix and
    milliseconds ("2026-09-07T01:32:33.566Z"); tolerate offsets and bare
    dates too, and refuse anything else rather than guessing.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def normalize_stamp(value):
    """Backend timestamp -> the ISO-8601 UTC spelling build_map stores, or ''."""
    parsed = parse_stamp(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""


# ── slug matching ────────────────────────────────────────────────────────────
def normalize_slug(value):
    """Normalize a slug the way `gbrain export` writes it: posix, no .md."""
    if not isinstance(value, str):
        return ""
    slug = value.strip().replace("\\", "/").strip("/")
    while slug.startswith("./"):
        slug = slug[2:]
    if slug.lower().endswith(".md"):
        slug = slug[:-3]
    return slug


def slug_candidates(relpath, fm_slug="", prefix=""):
    """Slugs a Markdown file may correspond to, most authoritative first.

    `gbrain export` writes every page to `<dir>/<slug>.md`, so the vault-
    relative path *is* the slug. When the stored slug is not a fixed point of
    the exporter's own path slugifier (legacy hand-keyed slugs with case or
    accents), export additionally stamps `slug:` into the frontmatter — that
    spelling wins. `prefix` re-attaches the slug prefix when the map is built
    from a subdirectory of an export.
    """
    out = []
    for candidate in (normalize_slug(fm_slug), normalize_slug(relpath)):
        if candidate and candidate not in out:
            out.append(candidate)
    head = normalize_slug(prefix)
    if head:
        for candidate in list(out):
            joined = head + "/" + candidate
            if joined not in out:
                out.append(joined)
    return out


# ── MCP stdio session ────────────────────────────────────────────────────────
class _Oversized:
    """Queued in place of a reply that ran past `MAX_REPLY_CHARS`.

    The line it stands for was read in capped slices and dropped, never
    assembled, so all that survives is how long it was.
    """

    def __init__(self, chars):
        self.chars = chars


class McpStdioSession:
    """A single `gbrain serve` child, spoken to over JSON-RPC on stdio.

    One process for the whole build: pages page-by-page, then whatever
    history was asked for, then EOF. Requests are strictly sequential, so a
    plain id counter is enough to match replies.
    """

    def __init__(self, command=DEFAULT_COMMAND, surface=None, timeout=DEFAULT_TIMEOUT,
                 extra_args=()):
        self.command = command
        self.surface = surface
        self.timeout = float(timeout)
        self.extra_args = tuple(extra_args)
        self.server_info = {}
        self._proc = None
        self._lines = queue.Queue()
        self._next_id = 0

    # -- lifecycle --
    def argv(self):
        argv = [self.command, "serve"]
        if self.surface:
            argv += ["--surface", self.surface]
        return argv + list(self.extra_args)

    def open(self):
        try:
            # The environment is inherited untouched: GBRAIN_HOME, the engine
            # config and the PATH the user set are exactly what `gbrain` reads
            # to decide which brain it is talking to.
            self._proc = subprocess.Popen(
                self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            raise GBrainUnavailable("cannot run %r: %s" % (self.command, exc))
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._handshake()
        return self

    def _pump_stdout(self):
        # `readline(n)` stops at the newline *or* at n characters, so a reply
        # that never ends cannot grow the process: it is read in capped slices
        # and discarded as it goes.
        try:
            stream = self._proc.stdout
            while True:
                line = stream.readline(MAX_REPLY_CHARS)
                if not line:
                    break                       # EOF
                if line.endswith("\n") or len(line) < MAX_REPLY_CHARS:
                    self._lines.put(line)       # a whole message (or a final, unterminated one)
                    continue
                self._lines.put(_Oversized(self._drop_rest_of_line(stream, len(line))))
        except (ValueError, OSError):
            pass
        finally:
            self._lines.put(None)   # EOF sentinel

    @staticmethod
    def _drop_rest_of_line(stream, seen):
        """Read the tail of an over-long line in slices, keeping none of it."""
        while True:
            slice_ = stream.readline(MAX_REPLY_CHARS)
            if not slice_:
                return seen
            seen += len(slice_)
            if slice_.endswith("\n"):
                return seen

    def _pump_stderr(self):
        # Drain the pipe so the child cannot block, but retain nothing: backend
        # logs may contain private page identifiers or content.
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for _line in proc.stderr:
                pass
        except (ValueError, OSError):
            pass

    def close(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except (ValueError, OSError):
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()
        return False

    # -- wire --
    def _write(self, message):
        if self._proc is None:
            raise GBrainUnavailable("session is closed")
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            # Child stderr may contain private slugs or content. Never copy it
            # into an exception that can reach a shareable HTML payload.
            raise GBrainUnavailable("gbrain serve stopped reading") from exc

    def _await(self, message_id):
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise GBrainUnavailable("gbrain serve did not answer within %gs"
                                        % self.timeout)
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                self.close()
                raise GBrainUnavailable("gbrain serve exited before replying")
            if isinstance(line, _Oversized):
                # Dropped, not buffered. Requests are sequential, so this was
                # almost certainly our reply; if it was not, the real one is
                # ignored later by its id and the session stays usable.
                raise GBrainToolError(
                    "gbrain serve sent a %d-character reply, past the %d-character "
                    "cap — dropped unread" % (line.chars, MAX_REPLY_CHARS))
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue        # banner or log line on stdout — not our reply
            if isinstance(message, dict) and message.get("id") == message_id:
                return message

    def _request(self, method, params):
        self._next_id += 1
        message_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        return self._await(message_id)

    def _handshake(self):
        try:
            reply = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            })
        except GBrainToolError as exc:      # an over-long handshake reply
            self.close()
            raise GBrainUnavailable("gbrain serve returned an invalid MCP handshake") from exc
        if "error" in reply or not isinstance(reply.get("result"), dict):
            self.close()
            raise GBrainUnavailable("gbrain serve refused the MCP handshake")
        server_info = reply["result"].get("serverInfo")
        if not isinstance(server_info, dict):
            self.close()
            raise GBrainUnavailable("gbrain serve returned malformed handshake metadata")
        self.server_info = server_info
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, tool, arguments):
        """Invoke one read tool and return its decoded JSON payload."""
        reply = self._request("tools/call", {"name": tool, "arguments": arguments})
        if "error" in reply:
            raise GBrainToolError("%s failed: %s" % (tool, _error_text(reply)))
        result = reply.get("result")
        if not isinstance(result, dict):
            raise GBrainToolError("%s returned no result" % tool)
        text = _first_text(result.get("content"))
        if text is None:
            raise GBrainToolError("%s returned no text content" % tool)
        try:
            payload = json.loads(text)
        except ValueError:
            raise GBrainToolError("%s returned non-JSON content" % tool)
        if result.get("isError"):
            detail = payload.get("message") if isinstance(payload, dict) else None
            raise GBrainToolError("%s failed: %s" % (tool, detail or text[:120]))
        return payload


def _first_text(content):
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            return item["text"]
    return None


def _error_text(reply):
    error = reply.get("error") if isinstance(reply, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return "malformed reply"


# ── page index ───────────────────────────────────────────────────────────────
def index_pages(rows, source_id=None):
    """Fold `list_pages` rows into {slug: facts} plus the rejection tallies.

    A slug is unique per *source*, not brain-wide, and nothing else in play
    carries a source: neither the Markdown file on disk nor `get_versions`,
    which takes a bare slug and answers with the union across sources. So when
    a federated read (`--gbrain-source __all__`, or no scope at all) returns
    one slug from two sources, there is no evidence that could tie the file to
    one of them. Equal `updated_at` values do not help — two pages that happen
    to have been written at the same instant are still two pages with two
    histories, and picking either is a coin flip dressed up as a fact. Such
    slugs go to `ambiguous`, are never asked about again, and keep their
    Markdown timestamps.

    With a concrete `source_id` the server is asked to filter, and the rows are
    checked against that request rather than trusted: anything tagged with a
    different source — or with none at all, which cannot be shown to match — is
    counted in `foreign` and dropped. A scoped read therefore enriches only
    rows that provably came from the source that was asked for — but it also
    cannot see a slug the *other* sources hold, which is why `history_scope`
    audits that separately before any history is read.
    """
    scoped = bool(source_id) and source_id != FEDERATED_SOURCE
    buckets, malformed, foreign = {}, 0, 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        slug = normalize_slug(row.get("slug"))
        updated = normalize_stamp(row.get("updated_at"))
        if not slug or not updated:
            malformed += 1
            continue
        row_source = row.get("source_id") if isinstance(row.get("source_id"), str) else ""
        if scoped and row_source != source_id:
            foreign += 1
            continue
        buckets.setdefault(slug, []).append({
            "slug": slug, "sourceId": row_source, "updated": updated,
            "title": row.get("title") if isinstance(row.get("title"), str) else "",
            "type": row.get("type") if isinstance(row.get("type"), str) else ""})
    pages, ambiguous = {}, set()
    for slug, group in buckets.items():
        if (len({facts["sourceId"] for facts in group}) > 1 or
                len({facts["updated"] for facts in group}) > 1):
            ambiguous.add(slug)
            continue
        # What is left is one page, seen once or repeated verbatim by the
        # inclusive-cursor quirk in fetch_pages.
        pages[slug] = group[0]
    return {"pages": pages, "ambiguous": ambiguous, "malformed": malformed,
            "foreign": foreign}


def fetch_pages(session, source_id=None, page_size=LIST_PAGE_SIZE, max_calls=MAX_LIST_CALLS):
    """Page through `list_pages` and return the folded index.

    Uses the paging recipe the tool documents for itself: sort ascending by
    `updated_at` and carry the last row's timestamp as an exclusive cursor.

    `source_id` is both sent to the server and used to check what comes back
    (see index_pages): a scoped read that answers with another source's rows
    does not get to enrich anything with them.

    Termination is on an empty batch or a cursor that stops advancing, never
    on "fewer rows than I asked for" — the server caps non-local callers at
    100 rows and may cap lower, and a short batch must not be mistaken for the
    end of the brain.

    `updated_after` is documented as a strict `>`, but gbrain 0.48.3.0 answers
    inclusively: every batch after the first repeats the boundary row. Both
    readings are handled — the cursor advances on the greatest timestamp in the
    batch, a batch that only repeats the boundary ends the walk, and duplicate
    rows fold away in the index. `truncated` is reported only when the walk
    really did stop early: a full batch that could not advance (more pages tie
    on one timestamp than fit in a batch) or the call budget running out.
    """
    rows, calls, cursor = [], 0, None
    truncated = False
    while calls < max_calls:
        arguments = {"limit": page_size, "sort": "updated_asc"}
        if source_id:
            arguments["source_id"] = source_id
        if cursor:
            arguments["updated_after"] = cursor
        batch = session.call("list_pages", arguments)
        calls += 1
        if not isinstance(batch, list):
            raise GBrainToolError("list_pages did not return a list")
        rows.extend(batch)
        if not batch:
            break
        following = _max_stamp(batch)
        if not following or not _advances(following, cursor):
            truncated = len(batch) >= page_size
            break
        cursor = following
    else:
        truncated = True
    index = index_pages(rows, source_id)
    index["calls"] = calls
    index["rows"] = len(rows)
    index["truncated"] = truncated
    return index


def _max_stamp(batch):
    """The greatest `updated_at` in a batch, in the server's own spelling.

    Taking the maximum rather than the last row keeps the cursor monotonic even
    if a build ever answers unsorted, which is what makes the walk provably
    terminate.
    """
    best_raw, best_value = None, None
    for row in batch:
        if not isinstance(row, dict):
            continue
        raw = row.get("updated_at")
        value = parse_stamp(raw)
        if value is not None and (best_value is None or value > best_value):
            best_raw, best_value = raw, value
    return best_raw


def _advances(following, cursor):
    """True when `following` is strictly newer than the cursor we sent."""
    if cursor is None:
        return True
    ahead, behind = parse_stamp(following), parse_stamp(cursor)
    if ahead is None or behind is None:
        return following != cursor
    return ahead > behind


# ── version history ──────────────────────────────────────────────────────────
def summarize_versions(rows):
    """Fold `get_versions` rows into revision counts and dates.

    MCP hands over one reply at a time and cannot stream it, so the rows —
    `compiled_truth` and `frontmatter`, the actual private prose of every past
    revision — do exist as decoded Python objects for the length of this call.
    They are projected to timestamps immediately after decoding and nothing
    else is retained: the return value holds counts and stamps only, so no
    revision body can reach a caller, a payload or the generated HTML.
    `MAX_REPLY_CHARS` bounds how large that transient decode can get.
    """
    stamps, malformed = [], 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        stamp = normalize_stamp(row.get("snapshot_at"))
        if not stamp:
            malformed += 1
            continue
        stamps.append(stamp)
    stamps.sort()
    months = collections.Counter(stamp[:7] for stamp in stamps if MONTH_RE.match(stamp))
    return {"revisions": len(stamps), "first": stamps[0] if stamps else "",
            "last": stamps[-1] if stamps else "", "months": dict(months),
            "malformed": malformed, "error": ""}


def unreadable_history(reason):
    """The summary shape used when a page's history could not be read."""
    return {"revisions": 0, "first": "", "last": "", "months": {},
            "malformed": 0, "error": reason}


def fetch_history(session, slugs, limit=0, order=None):
    """Read version history for `slugs`, one `get_versions` per page.

    All of them go down the single already-open stdio session, so this costs N
    round-trips but exactly one process. GBrain exposes no batched history op
    (see docs/gbrain-history.md, "External limitation"), which is why `limit`
    exists: with a budget, the slugs are visited in `order` — freshest first,
    ties broken by slug — so a truncated run is still deterministic.

    Every way one page can fail — a tool error, a reply past `MAX_REPLY_CHARS`,
    a payload that is not a list of rows, rows that cannot be read — leaves
    that page with an `error` and no counts, and is tallied so the caller can
    report the build as partial. A page whose rows were *partly* unreadable is
    in the same position as one that failed outright: how many revisions it
    really has is unknowable, and a maybe-complete count is not a count.
    """
    wanted = sorted(set(slugs))
    if order:
        rank = {slug: position for position, slug in enumerate(order)}
        wanted.sort(key=lambda slug: (rank.get(slug, len(rank)), slug))
    if limit and limit > 0:
        wanted = wanted[:limit]
    history, failures, incomplete = {}, 0, 0
    for slug in wanted:
        try:
            rows = session.call("get_versions", {"slug": slug})
        except GBrainToolError as exc:
            history[slug] = unreadable_history(str(exc))
            failures += 1
            continue
        if not isinstance(rows, list):
            history[slug] = unreadable_history("get_versions did not return a list")
            failures += 1
            continue
        summary = summarize_versions(rows)
        if summary["malformed"]:
            summary["error"] = ("%d version row(s) were unreadable, so the revision "
                                "count for this page is unknown" % summary["malformed"])
            incomplete += 1
        history[slug] = summary
    return {"history": history, "asked": len(wanted), "failures": failures,
            "incomplete": incomplete}


# ── precedence ───────────────────────────────────────────────────────────────
def classify(updated_at, history=None, skew_seconds=SEMANTIC_SKEW_SECONDS):
    """Decide the semantic update stamp for one page.

    Returns {updated, source, status, revisions, lastRevision}. `status` is
    one of:

      verified      history confirms the last touch was a content write
      superseded    a non-semantic touch came later; the snapshot is used
      single-write  no versions: written once, never revised
      inconsistent  the last snapshot is *newer* than `updated_at`, which the
                    write path cannot produce; nothing is verified
      unverified    history was not read (adapter ran pages-only)
      error         history was requested for this page and failed
    """
    backend = normalize_stamp(updated_at)
    if not backend:
        return {"updated": "", "source": "", "status": "missing",
                "revisions": None, "lastRevision": ""}
    if history is None:
        return {"updated": backend, "source": SOURCE_UPDATED, "status": "unverified",
                "revisions": None, "lastRevision": ""}
    if history.get("error"):
        return {"updated": backend, "source": SOURCE_UPDATED, "status": "error",
                "revisions": None, "lastRevision": ""}
    revisions = int(history.get("revisions") or 0)
    last = normalize_stamp(history.get("last"))
    if revisions == 0 or not last:
        return {"updated": backend, "source": SOURCE_UPDATED, "status": "single-write",
                "revisions": revisions, "lastRevision": ""}
    gap = (parse_stamp(backend) - parse_stamp(last)).total_seconds()
    if gap > skew_seconds:
        # updated_at moved after the last real revision: a re-embed stamp, a
        # rename or a revert. Freshness follows the content, not the touch.
        return {"updated": last, "source": SOURCE_REVISION, "status": "superseded",
                "revisions": revisions, "lastRevision": last}
    if gap < -skew_seconds:
        # A snapshot newer than the page's own updated_at is impossible in the
        # write path (createVersion fires just *before* the upsert), so one of
        # the two numbers is wrong — a clock jump, a restored row, a backend
        # change. Neither confirms the other: keep the page's own stamp, and
        # say the history disagreed instead of calling it verified.
        return {"updated": backend, "source": SOURCE_UPDATED, "status": "inconsistent",
                "revisions": revisions, "lastRevision": last}
    return {"updated": backend, "source": SOURCE_HISTORY, "status": "verified",
            "revisions": revisions, "lastRevision": last}


# ── façade ───────────────────────────────────────────────────────────────────
class GBrainReader:
    """Two-phase read: index every page, then history for the matched slugs."""

    def __init__(self, session, source_id=None, page_size=LIST_PAGE_SIZE):
        self.session = session
        self.source_id = source_id
        self.page_size = page_size
        # A concrete scope filters `list_pages` server-side; `__all__` and no
        # scope at all are both federated, and see every source's rows.
        self.concrete_scope = bool(source_id) and source_id != FEDERATED_SOURCE
        self._index = None
        # `scoped` rather than the id itself: a generated map is shared, and
        # the source's name is no more its business than a page's slug is.
        self.diagnostics = {"transport": "mcp-stdio", "calls": 0,
                            "scoped": bool(source_id)}

    def pages(self):
        index = fetch_pages(self.session, self.source_id, self.page_size)
        self._index = index
        self.diagnostics.update({
            "calls": self.diagnostics["calls"] + index["calls"],
            "rows": index["rows"], "indexed": len(index["pages"]),
            "ambiguous": len(index["ambiguous"]), "malformed": index["malformed"],
            "foreign": index["foreign"], "truncated": index["truncated"],
        })
        return index["pages"]

    def history_scope(self):
        """Which slugs may be asked for history at all, and what bounds that.

        `get_versions` takes a bare slug and answers with the union of every
        source that holds it, so a slug that exists twice brain-wide has no
        history that can be attributed to one page — and a concrete
        `--gbrain-source` is no evidence to the contrary, because it filtered
        the other source's rows out before they could be counted. A federated
        page index shows those collisions directly; a scoped one is therefore
        followed by one extra read-only `list_pages source_id='__all__'` walk
        made purely to count them. The audit's rows enrich nothing.

        Returns {"unique": set, "proven": bool, "reason": str}. Proof is
        all-or-nothing by necessity: a walk that stopped early or carried a row
        it could not read might have hidden any slug, so nothing it saw once
        can be called unique. `unique` is empty then, and no history is read.
        """
        if self._index is None:
            raise GBrainToolError("the page index has to be read before its history")
        if not self.concrete_scope:
            audit, origin = self._index, "index"
        else:
            try:
                audit = fetch_pages(self.session, FEDERATED_SOURCE, self.page_size)
            except GBrainToolError:
                self.diagnostics["historyProof"] = "unproven"
                return {"unique": set(), "proven": False,
                        "reason": "the brain-wide collision audit failed"}
            origin = "audit"
            self.diagnostics.update({
                "calls": self.diagnostics["calls"] + audit["calls"],
                "auditRows": audit["rows"], "auditTruncated": audit["truncated"],
                "auditMalformed": audit["malformed"],
                "auditCollisions": len(audit["ambiguous"])})
        short = []
        if audit["truncated"]:
            short.append("it stopped early")
        if audit["malformed"]:
            short.append("%d row(s) were unreadable" % audit["malformed"])
        if short:
            self.diagnostics["historyProof"] = "unproven"
            return {"unique": set(), "proven": False,
                    "reason": "the brain-wide collision %s was incomplete (%s)"
                              % ("audit" if origin == "audit" else "check",
                                 " and ".join(short))}
        self.diagnostics["historyProof"] = origin
        return {"unique": set(audit["pages"]), "proven": True, "reason": ""}

    def history(self, slugs, limit=0, order=None):
        result = fetch_history(self.session, slugs, limit=limit, order=order)
        self.diagnostics.update({
            "calls": self.diagnostics["calls"] + result["asked"],
            "historyAsked": result["asked"], "historyFailures": result["failures"],
            "historyIncomplete": result["incomplete"],
        })
        return result["history"]

    def close(self):
        self.session.close()


def open_reader(command=DEFAULT_COMMAND, source_id=None, want_history=False,
                timeout=DEFAULT_TIMEOUT):
    """Start a session and wrap it. Raises GBrainUnavailable on any failure."""
    surface = SURFACE_WITH_HISTORY if want_history else SURFACE_PAGES_ONLY
    session = McpStdioSession(command=command, surface=surface, timeout=timeout)
    session.open()
    return GBrainReader(session, source_id=source_id)
