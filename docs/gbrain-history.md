# GBrain history in brain-map

How the optional `--gbrain` adapter reads real timestamps out of a brain, what
those timestamps actually mean, and where the backend cannot answer the
question. Everything below was checked against **gbrain 0.48.3.0**.

Markdown-only remains the default. Without `--gbrain` nothing in this document
runs, and the map's freshness comes from frontmatter and file timestamps
exactly as before.

---

## 1. Why the export is not enough

A `gbrain export` writes `created` and whatever update-ish frontmatter the page
already carried. It does **not** write the brain's own `updated_at`, and it
writes no revision history at all. So a vault that was exported, cloned or
rsynced this morning has:

* file mtimes that all say "today" — freshness collapses to a single band;
* frontmatter that may say nothing about updates — freshness falls back to
  `created`, and a page rewritten last week looks two years old.

Neither is wrong on purpose; the information simply is not in the file. The
adapter goes and gets it.

## 2. The surface it reads

One `gbrain serve` child process per build, spoken to over MCP (JSON-RPC 2.0 on
stdio). This is the documented machine-readable surface — `gbrain --help` lists
`serve  MCP server (stdio)`, and every tool's schema is discoverable with
`gbrain --tools-json`.

Exactly two tools are ever called, both declared `scope: 'read'`:

| Tool | Arguments used | Returns |
|------|----------------|---------|
| `list_pages` | `limit`, `sort`, `updated_after`, `source_id` | `[{slug, source_id, type, title, updated_at}]` |
| `get_versions` | `slug` | `[{id, page_id, compiled_truth, frontmatter, snapshot_at}]` |

Their CLI equivalents, if you want to see the same rows by hand:

```bash
gbrain list --limit 5 --sort updated_desc      # list_pages, tab-separated
gbrain history <slug> --json                   # get_versions, JSON
gbrain call list_pages '{"limit":5,"sort":"updated_asc"}'
gbrain --tools-json | python3 -m json.tool | less
```

**Batching.** `list_pages` caps non-local callers at 100 rows, and an MCP client
is such a caller, so the page index is paged with the recipe the tool documents
for itself: `sort=updated_asc` plus `updated_after=<the greatest updated_at in
the batch>` as a cursor. A 5 000-page brain costs ~51 round-trips inside
**one** process — not one process per page.

> **Observed quirk.** The schema documents `updated_after` as a strict `>`, but
> gbrain 0.48.3.0 filters **inclusively**: every batch after the first repeats
> the boundary row. The adapter is written for both readings — the cursor
> advances only on a strictly greater timestamp, a batch that merely repeats
> the boundary ends the walk, and duplicate rows fold away when the index is
> built. A 540-page brain walks in 7 calls with no rows lost and no spin.

**Least privilege.** With `--gbrain` alone the child is started as
`gbrain serve --surface starter`, a ~27-tool catalog that does not contain
`revert_version` at all. `--gbrain-history` needs `get_versions`, which only
exists in the full catalog, so that mode starts `gbrain serve --surface full`
and the client-side allow-list of two read tools becomes the binding
constraint.

## 3. Read-only guarantees

The adapter is read-only by construction, at three levels:

* Only `list_pages` and `get_versions` are ever sent. No `put_page`,
  `delete_page`, `add_link`, `add_tag`, `remember`, `revert_version`, or any
  other mutating op appears anywhere in `scripts/gbrain_history.py` — grep it.
  `tests/test_gbrain_history.py::test_only_read_tools_are_ever_called` records
  every call the adapter makes against a scripted server and asserts the set is
  exactly `{list_pages, get_versions}`.
* The adapter never writes to the vault either; it only overwrites in-memory
  node fields before the HTML is serialized.
* Version bodies are projected to timestamps immediately after decoding. MCP
  stdio frames one JSON-RPC message per line and offers no chunked or streaming
  read, so a `get_versions` reply — which carries `compiled_truth` and
  `frontmatter` for every past revision — necessarily exists as a decoded
  object for the length of that one call; there is no way to avoid that short
  of a backend change. `summarize_versions()` then keeps counts and stamps and
  nothing else, so no revision body outlives the call, reaches a payload or
  reaches the generated file. There is a test that greps the payload *and* the
  rendered HTML for fixture secrets.
* That transient decode is bounded. The reader takes a reply in capped slices
  (`MAX_REPLY_CHARS`, 16 MiB) and, past the cap, discards the rest of the line
  unread rather than assembling it: one runaway page becomes "history
  unavailable for this page" instead of a heap the size of the brain. The cap
  is per message, so peak memory is one reply, not one brain.
* The map's own node data carries no brain identifiers. Slugs and source ids
  are matching inputs — they are used to look pages up, are never displayed,
  and are dropped before the payload is serialized. What a note carries is
  timestamps (`updated`, `brainUpdated`, `lastRevision`), a revision count and
  a `historyStatus`.
* No brain data is committed to this repository. Every fixture under
  `tests/fixtures/` is invented, and the bundled demo is built from the
  fictional corpus with the adapter switched off.

## 4. What `updated_at` means

`pages.updated_at` is set to `now()` by:

* every content write (`put_page` / `import` upsert, and the narrow body-only
  update used by redirect repair);
* `updatePageContextualRetrievalState` — the re-embed / contextual-retrieval
  tier stamp, which changes **no content**;
* a slug rename;
* `revert_version`.

It is **not** touched by: reads, `recall`, `search` / `query`, `synthesize`,
`gbrain export`, a git checkout, a file copy, or an `import`/`sync` of a file
whose `content_hash` is unchanged (that path short-circuits before the upsert).
This is the property that makes the adapter worth having: re-exporting or
re-cloning a vault moves every file mtime and moves no `updated_at`.

So `updated_at` is *nearly* semantic — the exception is the re-embed stamp and
the rename. That is what history is for.

## 5. What `page_versions` means — exact semantics

Schema (`page_versions`): `id`, `page_id`, `compiled_truth`, `frontmatter`,
`snapshot_at`. `get_versions` returns rows for one slug ordered by
`snapshot_at` descending.

Rows are inserted by exactly one function, `createVersion`, and it is called
from exactly one place in the write path: immediately **before** the page
upsert, and only when a row for that slug already exists
(`if (existing) await tx.createVersion(slug, txOpts)`). Nothing else in the
codebase writes the table, and it is never written on create.

Take a page with content writes `w₁ … wₙ` at times `t₁ … tₙ`:

```
w₁ (create)   -> no version row.            page = C₁
w₂ at t₂      -> version row: content C₁, snapshot_at = t₂.   page = C₂
w₃ at t₃      -> version row: content C₂, snapshot_at = t₃.   page = C₃
…
wₙ at tₙ      -> version row: content Cₙ₋₁, snapshot_at = tₙ.  page = Cₙ,
                                                        updated_at = tₙ
```

Three consequences, all of which the adapter relies on:

1. **Row count = writes − 1.** `len(get_versions(slug))` is the number of
   *revisions after creation*. Zero rows means "written once, never revised",
   not "no data".
2. **`snapshot_at` is a death certificate, not a birth certificate.** A row
   stamped `t₃` holds the content that was live *until* `t₃`, produced at `t₂`.
   Reading it as "this text was written at `t₃`" is off by one write.
3. **`max(snapshot_at) == updated_at`** after any content write to a page that
   already existed, because the snapshot and the upsert belong to the same
   logical write (a never-revised page has no rows at all). They can differ by
   a few hundred milliseconds when they land in separate transactions, which is
   why the adapter allows a 5-second window (`SEMANTIC_SKEW_SECONDS`).

### Replay

To replay a page's content timeline, order the rows ascending by `snapshot_at`
as `v₁ … vₙ₋₁` and append the live page body:

| Content | Where it lives | In force from | Until |
|---------|----------------|---------------|-------|
| `C₁` | `v₁.compiled_truth` | `created` (history cannot tell you `t₁`) | `v₁.snapshot_at` |
| `Cᵢ` (1 < i < n) | `vᵢ.compiled_truth` | `vᵢ₋₁.snapshot_at` | `vᵢ.snapshot_at` |
| `Cₙ` | the live page (`get_page`) | `vₙ₋₁.snapshot_at` = `updated_at` | now |

A replay that stops at the last version row is missing the current text; the
live page is the final frame.

### Diff

`diff(vᵢ.compiled_truth, vᵢ₊₁.compiled_truth)` is the change made by write
`wᵢ₊₁`, and it happened at `vᵢ₊₁.snapshot_at`. The final change —
`diff(vₙ₋₁.compiled_truth, page.compiled_truth)` — happened at `updated_at`.
Pairing a diff with the *earlier* row's timestamp dates every change one write
too early.

brain-map itself renders no diffs and stores no bodies. It uses only
`snapshot_at`: how many revisions a page has had, when the last one was, and
which month each one fell in.

## 6. Precedence and provenance labels

Highest wins. Every note is decided independently; a note the brain does not
know keeps its Markdown answer.

| Rank | Source | Inspector label | When |
|------|--------|-----------------|------|
| 1 | GBrain `updated_at`, verified | `gbrain_history` | history read, `updated_at ≈ max(snapshot_at)` |
| 1 | GBrain last revision | `gbrain_revision` | history read, `updated_at > max(snapshot_at) + 5s` → the later touch was a re-embed/rename/revert, so the map uses the last real revision |
| 1 | GBrain `updated_at`, unconfirmed | `gbrain_updated` | no versions (single write); history not requested; history not readable for that page (a collision, an audit that fell short, or a budget that stopped short); history failed for that page; or history contradicted `updated_at` |
| 2 | frontmatter `last_updated` / `updated` / `modified` | `frontmatter …` | note not matched in the brain |
| 3 | frontmatter `created` | `frontmatter created` | no update field |
| 4 | file timestamp | `file timestamp` | no dates at all |

`historyStatus` on each node spells out which case fired: `verified`,
`superseded`, `single-write`, `inconsistent`, `unverified`, `error`. The
inspector prints it next to the revision count, and shows the raw backend touch
when it differs from the timestamp freshness used.

`inconsistent` is the case the write path cannot produce: `createVersion` fires
just *before* the upsert, so the last `snapshot_at` can never legitimately be
newer than `updated_at` by more than the skew window. When it is — a clock
jump, a restored row, a backend that changed — the two numbers disagree and
neither confirms the other. The map keeps the page's own `updated_at`, labels
the provenance unconfirmed, and says in the inspector that history disagreed.
It is never called `verified`.

A note whose history read *failed* is in the same position with even less
information: the count is unknown (`revisions` is `null`), so the inspector
keys the revision row off the status rather than the count and still prints
"page history unavailable for this note". A note whose history was never
*asked for* — because the slug collides with another source, because the
collision audit could not cover the brain, or because `--gbrain-history-limit`
stopped short of it — is `unverified` with a `null` count: the backend
timestamp it kept is real and says so, and no revision number is invented to
go with it.

## 7. Creation growth vs revisions on the timeline

The stacked areas are still *creations* per month, from `created`. Revisions are
a separate overlay line built from version `snapshot_at` months, and the
playhead readout and tooltip report both. They are different events: a month
can add no notes and still be the month the brain was most heavily rewritten.

So the axis is the **sorted union of creation months and revision months**. A
month that created nothing and revised a dozen pages gets its own slot, with
every theme count at zero and a revision count of twelve — it is a month the
brain moved in, and dropping it would both lose the twelve revisions and
compress the time axis into a lie. `sum(payload["revisions"]["series"])` is
therefore always equal to `payload["revisions"]["events"]`: nothing falls off
the chart, and there is no leftover count to report.

## 8. Determinism

Same brain state in, same bytes out.

* Pages are walked in a fixed order with a deterministic cursor.
* Notes are matched to slugs in sorted title order, and each note takes the
  first candidate that exists (frontmatter `slug`, then path, then prefixed
  path).
* When a history budget (`--gbrain-history-limit`, default 500) truncates the
  run, the pages read are the most recently updated ones, ties broken by slug
  ascending — the same set every time, and the same set whether or not the
  budget was the thing that stopped it. A budget that did not reach every
  eligible page makes the build `partial` (§9.1), never a quiet subset.
* The brain-wide collision audit (§9.2) is a plain `list_pages` walk with the
  same cursor rule, so which slugs are eligible for history is a function of
  brain state, not of timing.
* A slug that comes back from more than one source is *not* enriched at all,
  and equal `updated_at` values do not change that. Two rows are two pages with
  two histories; nothing the adapter can see — not the file on disk, not
  `get_versions`, which takes a bare slug — ties the file to one of them, so
  there is no non-arbitrary answer and none is invented. The slug falls back to
  Markdown, its history is never requested, and it is counted in
  `diagnostics.ambiguous`. Use `--gbrain-source <id>` to disambiguate.
* With a concrete `--gbrain-source <id>` the rows are *checked*, not trusted:
  a row tagged with any other source — or with none, which cannot be shown to
  match — is dropped and counted in `diagnostics.foreign`. A server that
  ignores the scope therefore cannot smuggle another source's timestamps into
  the map. `--gbrain-source __all__` is a federated scope, not a concrete one,
  so it gets the ambiguity rule instead.

## 9. Limitations

These are properties of the backend surface, not of the adapter.

1. **No batched history.** `get_versions` takes a single `slug` and there is no
   bulk or since-cursor equivalent anywhere in the tool catalog, so full
   history costs **one call per page**. They all travel down one already-open
   stdio session, so the cost is round-trips, not processes — but it is still
   O(pages). `--gbrain-history-limit` bounds it (default: the 500 most recently
   updated eligible pages); `--gbrain-history-limit 0` reads every one of them.

   **The default budget makes a large brain a `partial` read.** More than 500
   matched pages and the run stops at 500 by construction: the 500 freshest are
   confirmed, every remaining note keeps its backend `updated_at` labelled
   `unverified`, the build is reported `partial` with the requested/read/skipped
   counts spelled out, and `--gbrain-required` refuses it. That is the intended
   behaviour of a cap — the alternative is a build that quietly reports history
   for part of the brain as if it covered all of it. To get full coverage, ask
   for it: `--gbrain-history-limit 0` (or any N at least as large as the number
   of matched pages), at the cost of one round-trip per page.
2. **`get_versions` is not source-scoped.** Its schema has no `source_id`
   parameter, so in a multi-source brain holding the same slug twice the
   returned rows are the union of both pages' histories, and nothing in them
   identifies which page they belong to. History is therefore only ever read
   for slugs that are *proven* unique brain-wide:

   * a federated read (`--gbrain-source __all__`, or no scope) proves that from
     the page index it already walked;
   * a concrete `--gbrain-source` cannot — the other sources' rows were
     filtered out of `list_pages` before they could be counted — so it is
     followed by one extra read-only `list_pages source_id='__all__'` walk made
     purely to count collisions. The audit's rows enrich nothing; only the
     collision counts are used, and the source ids never reach the map.

   A slug the audit finds in more than one source is never sent to
   `get_versions`. It keeps the backend `updated_at` the scoped index proved,
   labelled `unverified`, and is counted in the build's `partial` gaps.

   The proof is all-or-nothing by necessity. A collision check that stopped
   early, carried an unreadable row, or failed outright might have hidden any
   slug, so nothing it saw once can be called unique: **no** history is read,
   every matched note keeps an `unverified` backend timestamp, and the build is
   `partial`.
3. **No backend `created_at`.** `list_pages` returns `updated_at` only, so the
   timeline's creation axis still comes from frontmatter `created`.
4. **Single-write pages cannot be audited.** With no version rows there is
   nothing to compare `updated_at` against, so a re-embed stamp on a
   never-revised page is indistinguishable from a real write. Those notes are
   labelled `single-write`, not `verified`.
5. **History is not an audit log.** `page_versions` rows cascade away when a
   page is deleted, and only writes through the page upsert path create them.
6. **A whole batch tied on one timestamp stops the walk.** If more pages share
   a single `updated_at` than fit in one batch, the cursor cannot advance; the
   adapter stops, sets `diagnostics.truncated` and reports the build as
   `partial` rather than looping. Under the strict-`>` reading the tied pages
   are also skipped by the filter itself. Either way the affected notes simply
   keep their Markdown timestamps. The detection is deliberately conservative —
   "a batch as large as the one we asked for that could not advance" — so it
   never cries truncation on a healthy walk; the cost is that a server capping
   *below* the requested limit can stall on a tie without being noticed, since
   a short batch is indistinguishable from the end of the brain.
7. **A reply cannot be streamed.** MCP stdio frames one JSON-RPC message per
   line, so a page's whole history arrives as one message and is decoded whole
   (§3). The adapter bounds that at `MAX_REPLY_CHARS` (16 MiB) per reply and
   drops anything larger unread; the page is reported as a history error and
   the rest of the build continues.

## 10. Failure behaviour

Fail-open is the contract. A missing `gbrain` binary, a brain that will not
start, a refused handshake, a protocol change, a tool error, non-JSON content,
a wrongly shaped payload and a hung server all end the same way: the map is
built from Markdown, `payload["gbrain"]["status"]` is `unavailable`, the reason
is recorded in `detail`, and the CLI prints a `warning:` line to stderr while
still writing the file.

**Partial is its own status, and it is per note.** A read that half worked is
never reported as a clean one. `payload["gbrain"]["status"]` becomes `partial`,
`payload["gbrain"]["gaps"]` names every hole in plain words, the CLI prints
both a `warning:` line on stderr and a `PARTIAL:` suffix on its summary line,
and the map's header reads `· gbrain (partial)` with the detail in its tooltip.
It is raised by any of:

* the page index stopped early (a tie the cursor could not step over, or the
  call budget running out) — some pages were never seen;
* a slug came back from more than one source and was refused;
* a row came back from a source other than the one requested and was dropped;
* a row was unreadable;
* version history stopped early because the session died mid-walk;
* the brain-wide collision audit could not cover the brain, so no slug could be
  shown to belong to exactly one page and no history was read at all (§9.2);
* a matched page shares its slug with another source, so its history would be a
  union of both and was not read (§9.2);
* `--gbrain-history-limit` stopped short of the eligible pages — the gap names
  the limit and the requested / read / skipped counts (§9.1);
* one page's `get_versions` failed: a tool error, a reply past
  `MAX_REPLY_CHARS`, a payload that was not a list of rows, or a transport loss;
* one page's version rows came back partly unreadable. How many revisions it
  really has is then unknowable, and an unknowable count is withheld rather
  than rounded down to whatever happened to parse — the page is labelled
  `error`, keeps its backend `updated_at`, and its readable rows are left out
  of the revision totals.

In every one of those cases the enrichment that *did* happen is kept, per note:
a note matched to a page keeps the brain's timestamp and says where it came
from, and every other note keeps its Markdown one. That is the single rule for
the whole adapter — enrichment is per note, never all-or-nothing, in the clean
case and in the partial case alike — and it is why a partial read is worth
keeping rather than discarding. Notes whose history was not read — because the
budget stopped short, because the slug collides with another source, because
the collision audit itself fell short, or because history was never requested —
are labelled `unverified` and keep the backend `updated_at`; notes whose
history was read and failed are labelled `error`. Neither ever carries a
revision count, and neither is ever called `verified`.

`--gbrain-required` inverts all of it: an unavailable backend *and* a merely
partial read are both hard errors, raised before anything is written, so a
required build either read the brain cleanly or produced no file at all.

## 11. Checking it yourself

```bash
# what the adapter sees, without the adapter
gbrain call list_pages '{"limit":5,"sort":"updated_desc"}'
gbrain history <slug> --json

# build with backend timestamps and history, against a fixed reporting date
python3 scripts/build_map.py <export_dir> out.html \
    --as-of 2026-06-15 --gbrain-history

# the same build, Markdown-only, to compare
python3 scripts/build_map.py <export_dir> markdown-only.html --as-of 2026-06-15
```

The run prints a `gbrain :` line with how many notes matched, how many pages
were indexed and how many revisions were found.
