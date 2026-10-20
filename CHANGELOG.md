# Changelog

All notable changes to Range Chart Analyzer are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A failed call is now CLASSIFIED, and the classification decides whether
  it is worth retrying.** The retryable set was a list of HTTP STATUSES, so
  a 429 was retried three times whatever it meant. But a 429 can be a
  throttle ("you are going too fast", retry after a wait) or an EXHAUSTED
  PLAN (the weekly bucket is empty; retrying cannot succeed, and it spends
  the OTHER bucket).

  MiniMax's Token Plan publishes both — the 5-hour bucket and the weekly
  one, measured at 17 % and 20 % used on 2026-10-20 — and the weekly one
  is exactly what throttled the end-to-end regression. An exhausted plan
  now costs ONE attempt instead of four, and says so.

  The classifier is borrowed from `sst/opencode`'s
  `packages/ai/src/provider-error.ts`, which is the most careful one in
  the four projects compared:

  - the vendor's typed CODE beats the status (a 429 with
    `insufficient_quota` is a dead plan; a 400 with `invalid_request` is a
    malformed request);
  - a message that says "the prompt does not fit" beats a generic
    `invalid_request_error` code — Anthropic sends that code for a prompt
    that is too long, and the two need different answers;
  - the provider's own `x-should-retry` header wins over everything;
  - an UNRECOGNISED failure retries, because the transient failures are
    exactly the ones that arrive in a shape no classifier anticipated —
    while an unrecognised 4xx does not, because a 4xx means the server
    refused the request as it stands.

  The classification changes the DECISION, not the wording: the operator
  still reads the provider's own message.

- **A stream the model never finished is no longer returned as an
  answer.** The SSE reader assembled whatever arrived and handed it back,
  so a stream that ended early -- the connection closed without a
  `message_stop` and without a `stop_reason` -- produced a PARTIAL table
  that looked exactly like a complete one. The reader would get a range
  chart that silently stops halfway down the figure, with nothing to say
  why.

  Measured against MiniMax on 2026-10-20: a real stream's event sequence
  is `message_start`, `ping`, `content_block_start`,
  `content_block_delta`, `content_block_stop`, `message_delta` (with a
  `stop_reason`) and `message_stop`. So "the model never said it was
  done" is a reliable signal, and an unterminated stream is now reported
  as a failure and classified `incomplete_stream` -- which IS retryable,
  because a second attempt can finish what the first did not.

- **The response HEADERS now reach the retry layers.** The repo has
  documented for weeks that `get_retry_delay` honours `Retry-After` "when
  they are passed in" and that both live call sites passed `None` — so a
  server answering `429 Retry-After: 120` was backed off by ~0.8 s and
  retried, which is the opposite of what `Retry-After` asks for, while
  the BROWSER (which has a real `Headers`) waited the requested time. The
  two transports disagreed about the same failure.

  The headers travel in a CALLER-PROVIDED out-parameter rather than in
  the return tuple, because that tuple — `(raw, truncated, status,
  err_body, usage)` — is consumed in a dozen places and changing its
  arity was the reason the job had been put off.

  Measured against MiniMax on 2026-10-20: it sends NO rate-limit headers
  at all (the response carries only edge/CDN tracing headers), so this
  changes nothing for that provider — but it is what the other providers
  in the preset list need, and it is why the two transports no longer
  disagree.

- **The provider UI now has the things cc-switch's has.** Nineteen items were
  taken from `farion1231/cc-switch`'s provider settings, and the ones that
  fit this product were implemented rather than listed:

  | cc-switch | what it is here |
  |---|---|
  | `ModelInputWithFetch` | the model field and a fetched-model picker side by side; the typed text narrows the list, and picking an entry fills the field |
  | `GptContextWindowHint` + `modelMetadataFill` | a line under the model field saying the **measured** ceiling — 524,288 output tokens, 1,000,000 context — instead of letting the user find out with a 400 |
  | `FullUrlToggle` | the endpoint may be the FULL request URL, used verbatim, for a gateway whose path is not the canonical one |
  | `EndpointSpeedTest` | measure the candidate endpoints, colour the latency (cc-switch's <300 / 300–500 / 500–800 / ≥800 ms bands), auto-select the fastest, keep the alternates on the record |
  | `JsonEditor` | a "format" button on the extra-headers field that re-indents the JSON and says **where** it is wrong |
  | provider notes | a free-text note per provider, shown on the card and searchable — what turns five relay accounts from five opaque names into five identifiable ones |
  | custom provider logo | a custom glyph for the card's icon tile, instead of a name-derived abbreviation that gives five relays five identical tiles |
  | profiles | a provider belongs to a named group; the providers page filters to one group at a time |
  | category counts | a line above the list saying how many providers there are, which one is active, how many are healthy and how many are failing |
  | usage query | the plan's remaining quota, on the card and in the browser — the number that decides whether the next extraction can start |
  | environment check | a persistent strip above the extract panel when no provider is configured, or a proxy is set |
  | error boundary | an unhandled exception reaches a dialog with the traceback in the clipboard instead of a console a packaged app does not have |
  | what's new | the first launch after an upgrade says what changed, read out of this file |

  The advanced fields (extra headers, note, icon, profile) are folded into one
  "advanced" row, because the fields a user fills in ONCE were pushing the save
  button off the bottom of the dialog, and a field the user cannot see is a
  field the user cannot fill in.

  The speed test runs on a worker thread. It did not at first, and with three
  candidate endpoints the dialog was frozen for half a minute — a frozen
  dialog is a dialog the user closes and never uses.

- **`effort=max` with `max_tokens=524288` now answers 3 of 3.** The end-to-end
  regression of the SHIPPED path (the real entry point, scored against the
  authority CSV) was re-run on the budgets that scale with the reasoning
  level — 240 s per call, 600 s per path — and every run came back perfect:

  | run | wall clock | cells | precision | recall |
  |---|---|---|---|---|
  | 0 | 252 s | 93 / 93 | 1.0000 | 1.0000 |
  | 1 | 191 s | 93 / 93 | 1.0000 | 1.0000 |
  | 2 | 419 s | 93 / 93 | 1.0000 | 1.0000 |

  The third run is the interesting one: at 419 s it would have been cut off by
  the old 120 s ceiling, which is what the previous round measured as "2 of 3,
  one network failure". The budget was the missing piece, not the model.

  The price is wall clock, and it is worth stating plainly: `low` answers the
  same figure in 24–31 s for about a quarter of the tokens, and `max` is for
  the run where the answer has to be right rather than fast.

- **The plan's remaining quota is now a number you can see.**
  MiniMax's programming plan publishes it, and the route was confirmed against
  a live Token Plan key: `GET /v1/api/openplatform/coding_plan/remains`, with
  a 5-hour bucket that refills quickly and a weekly bucket that does not.
  Measured at the time: the 5-hour bucket 1 % used, the weekly bucket 18 % used
  — which is the one that was actually throttling the account.

  The two windows are shown SEPARATELY, because they are separately binding,
  and the chip shows what is **LEFT**: the API reports the used share, and
  "1 % used" where the user reads "1 % left" is a sign error the user acts on.
  The card shows the same number the browser does, and the browser gets it
  from a new `GET /api/quota` on the local server, which holds the key — the
  browser never sees the credential. That endpoint is loopback-only (or
  token-gated) and has its own, tighter rate bucket, because every call to it
  leaves the machine and spends the account's own limit.

- **The providers file is no longer overwritten in silence.**
  `providers.json` is a plain JSON file in the user's profile, so a text
  editor, a sync client or a second copy of the app can change it while a
  dialog is open. Whichever version is written last used to win, and the loser
  was silent. The digest taken when the dialog opened is now compared with the
  file on disk when it closes, and a change is a QUESTION — keep the file on
  disk, overwrite it with the edit, or cancel — because the file on disk may
  hold a key the user pasted a minute ago.

- **A provider's note, icon, profile, full-URL flag and alternate endpoints are
  FIELDS of the record.** The wizard REBUILDS the provider from a hand-listed
  set of fields, so a field that is not listed is a field the user loses the
  moment they edit that provider — and the product reverts to a configuration
  that times out, with nothing on screen to explain why. The reasoning effort
  was exactly such a field once; the same argument applies to every field
  added here, and the same test covers them.

- **The environment is checked before an extraction, not after.**
  No provider configured, or a system proxy that a local endpoint cannot be
  reached through, is a failure the user is about to hit — and a proxy makes
  `127.0.0.1` fail in a way that looks exactly like a dead server.

### Fixed

- **The front-end was never as thinly covered as the backlog said.** A note
  claimed "about 4900 lines of thin coverage across `js/table.js`,
  `js/export.js` and `js/ics_table.js`". Measured with Node's own V8
  coverage — `scripts/js_coverage.py` — the front-end is at **99.9 %**:
  23,251 of 23,267 lines, and every file within a rounding error of 100 %
  (`table.js` 100.0 %, `app.js` 100.0 %, `minimax.js` 100.0 %,
  `i18n.js` 99.9 %).

  The old number came from measuring the wrong thing, and the two ways it
  went wrong are worth keeping in the record:

  * V8's coverage offsets are in UTF-16 CODE UNITS, not bytes.
    `js/i18n.js` is 62,899 characters and 79,507 UTF-8 bytes, so a
    byte-based line map puts every line after the first CJK block in the
    wrong place. Measured that way, `i18n.js` came out 81.7 % covered and
    `table.js` showed a 30-line hole in its export block — both
    artefacts, in files that demonstrably run end to end.
  * V8 reports block ranges inside functions, so a file's trailing
    top-level export block produces no range of its own and would be
    counted as "never executed" although the suites use the globals it
    exports.

  The script is left in the repository so the number can be re-checked
  instead of re-argued.

- **The provider store could be emptied with no way back.** A probe script
  emptied the live `providers.json` — five providers, two of them paid
  MiniMax keys — and the only way back was a backup somebody had taken by
  hand the day before. A lucky accident, not a design.
  Two things were missing, and both are here now:

  - **A supported way to point the store somewhere else.**
    `RCA_PROVIDER_STORE` overrides the path. The obvious way to redirect
    it — replacing the module attribute `_default_store_path` — does
    NOTHING, and that is the trap worth naming: `ProviderStore.path` is
    declared with `field(default_factory=_default_store_path)`, and the
    factory is bound at CLASS-CREATION TIME, so the class keeps calling
    the original function. The probe believed it was writing to a temp
    file and was in fact writing to the user's real one. The test suite
    now pins the trap itself, so nobody reaches for it again.

  - **A history of the store's own.** Every save that changes the
    CONFIGURATION keeps the file it replaced, as a byte-level snapshot in
    a rolling `.bak` / `.bak.1` … chain — the same rotation and the same
    count `scripts/update_ics.py` already uses for the canonical table.

    The rule is deliberate about what counts as a change. The API key is
    excluded, because it is re-encrypted with a random IV on every write
    and including it would make every save look like a change. `current_id`
    and `is_current` are excluded too, because they are a POINTER rather
    than configuration: the store rewrites the file every time the user
    switches the active provider, and five switches would otherwise push
    the user's real providers out of the chain — which is the chain they
    would need precisely after an accident.

- **`Optional` was used in `rca_core/llm.py` without being imported.** The
  annotations are strings at runtime (`from __future__ import annotations`), so
  the tests were green and the import did not fail — but the CI lint gate
  (`ruff check . --select F821`) was red, and `typing.get_type_hints()` on the
  quota catalogue would have raised `NameError`. The same class of defect was
  already fixed once in this file (`Mapping`, REVIEW-2026-10-20), which is why
  the gate exists.

- **The model catalogue's note was Chinese prose in a three-language UI.** The
  note is now an i18n key, translated in zh/en/ja, with the raw string as the
  fallback so a catalogue entry is never silently blank.

- **`tests_llm.py` only passed on a machine whose DNS is not intercepted.**
  The test asserted the request dispatch against `https://example.com`, and
  the SSRF guard RESOLVES the host before it lets a request out. On a
  machine whose resolver is a proxy or a corporate DNS — most Chinese ISPs
  among them — `example.com` resolves to `127.0.0.1`, the guard rejects it
  as private, the request never reaches the fake `urlopen` the test
  installed, and the test failed on "the model did not answer" while the
  code under test was perfectly fine. CI never saw it because GitHub's
  resolver returns the real public address.

  The endpoint is now a literal public address, so the test asserts what it
  means to assert — the headers, the path and the body — and nothing about
  the machine's resolver. The guard's own behaviour against private hosts
  is covered by `tests/test_ssrf.py` and `tests/test_ssrf_shared.py`.

- **A provider can now say how hard the model should think, and the levels are
  the MODEL's.** MiniMax's M3.1 family ALWAYS thinks and its effort level
  DEFAULTS TO `max`; the thinking tokens are billed against and CONSUME
  `max_tokens`. Measured on the reference figure's label call:

  | model | effort | label call | out_tok |
  |---|---|---|---|
  | `MiniMax-M3` | thinking off by default | 6.5–13.1 s | — |
  | `MiniMax-M3.1-Flash` | `low` | 28.3 s | 3,077 |
  | `MiniMax-M3.1-Flash` | `high` | 48.3 s | 5,948 |
  | `MiniMax-M3.1-Flash` | `max` | 69.1 s / 94 s / 102.4 s / 121 s | 9,932 / 14,738 |

  The failure is invisible from the outside: with `max_tokens=2000` the call
  returned HTTP 200 in 23.3 s with `stop_reason=max_tokens` and an EMPTY
  body, because the thinking had eaten the whole budget before the model
  wrote a character of the answer. Raising `max_tokens` does not help — the
  thinking is unbounded — and lowering the effort does.

  The levels are declared **per model**, the way minimax-code's catalogue
  does it: M3.1 takes five named levels, and M3 does not take an effort at
  all (its thinking is an on/off switch, and an effort value does not change
  its depth). The picker is filled from the active provider's model, so it
  never offers a control that does nothing, and the resolution rule is
  minimax-code's: a valid stored choice wins, the catalogue's default wins
  over the midpoint, and an even-length list without a default uses the
  higher midpoint.

  The wire shape is MiniMax's, not Anthropic's: `thinking: {type: adaptive}`
  PLUS `output_config: {effort: ...}` on the Anthropic-compatible endpoint,
  `reasoning_effort` on the OpenAI one. `thinking: {budget_tokens: N}` is
  NOT in M3.1's documented vocabulary and is never sent — OpenRouter's
  Messages API rejects it outright, and its Claude 4.6 migration note says
  adaptive and the budget mode are different mechanisms.

  `LlmProvider.reasoning_effort` is a first-class field (persisted with the
  record, editable in the provider wizard AND in the settings page, mirrored
  by the browser client) and it is deliberately not an `extra_body` blob:
  the provider wizard REBUILDS the record from a fixed field list, so
  anything smuggled into `extra_body` is silently dropped the moment the
  user edits that provider — and the product would revert to the
  timing-out configuration with nothing on screen to explain why.

  The effort is also part of the **cache key**, by the same rule the cache
  module already states for `extra_body`: a cached result is a result for ONE
  request, not for the image. It was left out at first, so a user who
  switched the effort got the previous effort's answer back with no way to
  tell.
- **The reasoning catalogue is a MAP, not a list of options.** cc-switch
  keeps one universal level set and a per-model map
  (`src/config/piThinkingProfiles.ts`), and that shape is strictly more
  expressive than a list: it can say that a provider accepts only SOME of the
  levels, and that a level's name on the wire differs from its name in the
  UI — cc-switch maps Gemini's `low` to `"LOW"` and its `high` to `"HIGH"`,
  which a list cannot express at all.

  Two real gaps closed by it. A level the provider does not take is now
  REFUSED rather than guessed at — cc-switch's rule is explicit about why:
  "思考等级对部分供应商无效 ... 硬传可能导致请求被拒". And the "default" row
  (let the provider decide) is now distinguished from every concrete level
  by a mapped value rather than by a special case in the caller.
- **The provider wizard can fetch the model list from the endpoint.**
  cc-switch's "自动获取模型": the user pastes a key and picks the model from
  what the endpoint actually serves, instead of copying a slug off a web
  page.

  This is the commonest configuration mistake and it fails LATE: the
  settings look fine and the first extraction comes back 404. The plumbing
  was already here — the connection test GETs `/v1/models` and keeps the
  ids in `ConnectionResult.models_sample` — but the WIZARD never offered
  them, so the user had to go and read the vendor's documentation. Measured
  against MiniMax's own endpoint: the listing answers on both the
  Anthropic-compatible and the OpenAI-compatible route and carries the real
  ids.

  The fetch runs on a worker thread (a GET against a distant relay can take
  seconds, and the wizard is a modal dialog), and it applies the same SSRF
  policy as every other path, so a private endpoint is never dialled with
  the key.
- **The extraction page shows REAL progress, and the model's own thinking.**
  The occurrence-matrix path makes FOUR calls for a typical figure (one for
  the axis labels, one per contact sheet) and each of them can take a minute
  — measured, the label call alone spent 28 s on "low", 48 s on "high" and
  69–121 s on "max". A spinner that says "working" for two minutes tells
  the operator nothing, and there is no way to tell a slow extraction from a
  hung one.

  So the page now carries a four-pixel determinate bar (the fraction of the
  matrix cells the reader has ACTUALLY read — real progress, not a guess)
  and ONE elided line of the model's thinking. The thinking is real because
  the two-stage path now STREAMS its calls: the reply already carried a
  `thinking` block that the reader threw away, and the stream delivers it
  while the model is still thinking. Measured against M3.1-Flash: the first
  thinking chunk arrives about a second in, and the assembled answer is the
  same one the single-body call returns (22 rows / 18 columns / 3 legend).

### Fixed

- **A busy tier and a dead socket did not cost the same, and were retried the
  same number of times.** The transport wrapper gave both exactly one retry.
  A 429/529 comes back in about a second, while a read timeout has already
  cost the whole per-call ceiling. Measured on `MiniMax-M3.1-Flash` at a
  congested tier, the reference figure's three-run regression lost 31 of 93
  cells to bare 529s (precision/recall 0.333 and 0.667) and a third run
  failed outright — one retry is not enough for a busy tier. A server that
  ANSWERED now gets up to four attempts; a network-level failure keeps two.
- **The two-stage path's wall clock was bounded by a call COUNT, which stopped
  being true once each call could retry.** The server's single-run budget is
  `timeout_sec * 8 + 25` s and its meaning is "at most eight outbound
  attempts' worth of seconds" — eight, because the path makes four calls of
  its own and each of them may retry. The path now carries its own deadline,
  checked before every attempt, and the deadline SCALES with the reasoning
  level, because the level decides how long the model thinks: a single 90 s
  ceiling was right for "low" and too small for the others (at "max" the path
  gave up before it could answer, and the end-to-end regression failed 3 of 3
  with an empty reply).
- **The `max_tokens` ceiling was a product-side guess, not the model's limit.**
  MiniMax-M3.1-Flash answers a larger value with `does not support max
  tokens > 524288`, and 1000000 is a 400 — so 524288 is the largest value
  that is accepted. The old 32000 was why a reasoning model could not be
  given room to think: the thinking tokens are billed against `max_tokens`,
  and at 32000 the "max" effort spent the whole budget thinking and returned
  an EMPTY body (HTTP 200, `stop_reason=max_tokens`, zero characters of
  answer). The ceiling is now the model's, in the core, in the web UI's clamp
  and in the desktop spin box alike.

- **A cell-reading failure was invisible: a truncated contact sheet was never
  recognised as one.** The cell call bound its truncation flag to one name and
  the guard tested another — the LABEL call's, always False by then. Measured
  with two sheets and the second cut off mid-array: the run returned
  `ok=True` with `cell_status: "grid_cells"`, 31 of 53 cells carrying a code,
  **22 blank**, and not one "cell sheet unusable" line in the log. The run
  looked verified while two fifths of its cells were empty. The tiles of an
  unusable sheet are still real cells, so they are now recorded with an empty
  code — dropping them would turn a read failure into a false absence, which
  the 2026-10-17 P1 already ruled out.
- **A colour-coded matrix read as an empty table.** The ink test measured
  LUMINANCE, so a yellow cell (luminance 215) and a pale pink one (209) were
  both "not ink" — a matrix that encodes abundance by colour produced no
  occurrences at all. A colour test was added, and the contact sheet is now
  cropped from the original colour image instead of the greyscale one, so the
  reader can tell a red class from a blue one.
- **The colour test then marked every cell of a colour-cast page as filled.**
  The first version used an absolute chroma threshold, and a yellowed book page
  (chroma 50) exceeded it — a 5-mark table became 64 filled cells, and because
  the cell prompt forbids an empty answer, the reader then had to invent a
  letter for all 59 empty ones. The test is now RELATIVE to the page's own
  chroma, so a cast on the paper cancels out.
- **A ruled line wider than the inset made every empty cell read as filled.**
  The 4px inset assumed a 2px rule; a 5px rule (1.25pt at 300dpi) or a
  blurred scan leaves a sliver of the line inside the cell, and a MEAN counts
  that sliver as a mark — 52 of 64 cells read as filled on an 8×8 table with 5
  real marks. The test now measures the MIDDLE of the cell, away from the
  borders, which separates them at any rule width. Re-measured on the
  reference figure: marked cells bottom out at 0.133 and empty ones at 0.000,
  so the existing 0.08 threshold still sits inside the window.
- **The two-stage path threw away the token usage and the model replies of
  every call it made.** The result reported `usage: {}` and `raw: ""` for a run
  that issued two or three billed calls, so the server's token totals
  under-reported the new default by 2–3× and the audit trail stored no model
  reply for those runs at all. Usage is now accumulated and the replies are
  stored as one JSON document.
- **The `cells` strategy claimed geometric verification it never performed.**
  It puts the measured spans in the PROMPT and lets the model place the cells;
  nothing checks where it put them. But it was stamped `grid_cells`, and that
  is exactly the string the normalizer's verified-geometry discriminator keys
  off — so the model's blank-cell padding was kept as occurrences (3 padding
  rows in, 5 occurrences out, where `single_pass` and `gated` gave 2). It now
  carries its own status, `grid_guided`: the geometry was guided, not verified.
- **A partially-read run was titled "Occurrences" (verified).** The plain title
  now also requires that every cell carries a code or a class, on both the
  exporter and the browser side.
- **The reader's code was never checked against the legend it was given.** The
  legend codes went into the prompt and nothing validated the answer, so a
  figure defining A/C/R could come back with "X" — and the label prompt in the
  same module TEACHES the model to write "?" for an illegible character. Such a
  code now counts as unread rather than reaching the table as a class the
  figure never defines.
- **A legitimate figure could be rejected.** The label guard required that no
  two inked rows or columns share a name, which a matrix listing one locality
  twice fails. It now measures a COLLAPSE — 22 printed rows collapsing onto 4
  names is 18% distinct and is rejected; one repeat is not.
- **`INK_LEVEL` was written out as a literal in `matrix.py`** while
  `grid.py` owns it, so tuning the threshold in one place would silently leave
  the other measuring a different thing.
- **`HEAD /health` answered 403 while `GET /health` answered 200.** `/health`
  was the one GET route missing from the HEAD mirror, so an uptime probe using
  the cheap form reported the server as forbidden. RFC 9110 §9.3.2 requires
  HEAD to be identical to GET minus the content.
- **Ctrl+C did not stop the server while an extraction was in flight.** Three
  things were missing on the way out: `server_close()` was never called, so the
  listening socket stayed bound and kept accepting connections; `shutdown()`
  does not touch the request threads, and `concurrent.futures`' atexit hook
  JOINS the pool's workers — so the process waited for the extraction, up to
  745 s at the default timeout, with the port still accepting connections
  throughout; and `daemon_threads = True` looks like it would prevent that but
  is inert, because `process_request` is overridden.
- **The history row cap's documented safeguard never existed.** "Shed the
  thumbnail first, and only delete a row when the table is still over the cap" —
  but shedding a BLOB cannot change the ROW COUNT, and the predicate was the
  row count, so it was the same statement as the condition that entered it and
  was always true. The rows it stripped were exactly the rows that were then
  deleted. The cap is a RETENTION cap and the documentation now says so.
  *The retention policy itself is unchanged: the 2026-09-27 P1 was fixed by
  raising the default from 500 to 5000, and a policy that keeps stripped rows
  forever would trade the audit trail's bound for the database's — that is a
  decision to make deliberately, not as a side effect of a review.*
- **HTTP 529 was not retried.** 529 is the "overloaded" answer — MiniMax
  returns `overloaded_error` with it, meaning the tier is busy. It was absent
  from the retryable set, so a transient congestion blip became a failed
  extraction. On the two-stage path's four sequential calls the per-call rate
  compounds into the run's rate, which is why `MiniMax-M3.1-Flash` failed
  every run of one measurement while answering 7 of 8 single requests.

## [0.1.2] — 2026-10-18

### Changed

- **Fluency pass (UX-2026-10-18).** Full report in
  `docs/UX-SMOOTHNESS-2026-10-18.md`. Every number below was measured on
  this machine before and after; two of the planned "optimizations" were
  rejected because measurement said the benefit was not there.

  | area | change | measured |
  |---|---|---|
  | `gui_fluent.py` | `_xlsx_available()` is `lru_cache`d — it runs inside `_update_result_actions()`, which re-runs on every progress tick, and its cold body is `import openpyxl` | first result render froze the UI thread **351.6 ms → 0.002 ms** |
  | `rca_core/llm.py` + `gui_fluent.py` | transient-failure retries now announce themselves: `retrying:{n}/{retries}:{backoff}s` is emitted before every backoff sleep and rendered as "请求受限，正在重试… 2/3 (1.1s)" | the retry chain used to be silent; nothing else changed (backoff itself is 1.0–2.0 s and stays untouched) |
  | `gui_fluent.py` | single-run extractions emit a 1 Hz `analyzing:1/1:{elapsed}s` heartbeat | the single-run path used to go silent between `analyzing` and the result — worst case ~6 min with no feedback. **The heartbeat timer lives on the GUI thread**: a QTimer created inside `QThread.run()` never fires (no event loop on that thread — measured 0 ticks); the first implementation of this very change shipped that bug and was caught by `tests/qt_thread_probe.py` |
  | `gui_fluent.py` | XLSX export runs on a QThread (`_write_xlsx_worker`, gate-driven button state, `closeEvent` parks the worker) | `to_xlsx` costs **552–772 ms** at 1000–5000 rows — that whole time the window was frozen; CSV/TSV (30 ms) stay synchronous on purpose |
  | `gui_fluent_providers.py` | removed four `store.save()` calls that duplicated the save each CRUD method already performs internally | one "add provider" wrote providers.json **three times**; at 60 providers each write is 70–84 ms of fsync+rename on the UI thread |
  | `js/table.js` + `css/app-ux.css` | tables ≥ 600 rows (`RCA_VIRTUAL_ROW_THRESHOLD`) get `data-rca-big="1"` → `content-visibility: auto` on the rows; tables ≥ 1000 rows (`RCA_LONG_TABLE_HINT_ROWS`) also say so next to the export buttons | honest status: the containment rule **hits** (computed style confirms it) but has **no measurable speed benefit on current Chrome** — Chrome does not size-contain internal table boxes, so nothing is skipped (AX-tree proof: 5000/5000 rows still exposed to assistive tech with the rule on). Kept as defensive styling with the `<tr>`-never-`<td>` scoping enforced by tests; a `<td>` placement measurably shifts columns by up to **267 px** |

  New tests: `tests_frontend_perf.js` (43 assertions incl. an AX-tree
  mechanism check and a `<td>` negative control; `--quick` skips only the
  5000-row cases and says so), a `browser-perf` CI job, and two Qt runtime
  probes (`tests/qt_thread_probe.py`, `tests/qt_export_gate_probe.py`) that
  require PyQt6/PySide6 and verify thread-model behaviour stubs cannot see.

  Rejected after measurement, deliberately: model-izing the Qt table
  (full pipeline is ~50 ms at 5000 rows, not the 149 ms a component-only
  benchmark suggested), making CSV/TSV export async (30 ms — imperceptible),
  and deferring `Database()` creation (30 ms cold, 6 ms warm).

### Fixed

- **Fluent GUI / XLSX export**: the export body is extracted to a module-level
  function; during the fluency pass an earlier edit had left it **called but
  never defined** — every "Export Excel" click would have raised `NameError`
  inside the worker and surfaced only as an InfoBar "export failed"
  (`tests/test_unbound_names.py` caught it).

## [0.1.1] — 2026-10-16

- Cut the one-file exe by 16.4% (323.5 → 270.6 MB) with no loss of function:
  DevTools debug payloads and OpenCV video-IO decoders dropped, audited by
  `scripts/audit_exclude_imports.py` / `audit_exclude_suite.py`.

### Fixed

- **Full-code review of 2026-10-17: six P1 defects, all measured and fixed.**
  Complete report in `docs/CODE-REVIEW-2026-10-17.md`. No P0 (no auth bypass,
  no arbitrary file read/write, no data destruction); every finding below was
  reproduced on this machine before being fixed and again after.

  | area | defect | consequence |
  |---|---|---|
  | `server.py` / `ssrf.py` | a malformed endpoint hostname (an IDNA label > 63 bytes, or an empty label) raised `UnicodeEncodeError` out of `do_POST` | socketserver closed the socket and the client got **zero bytes** while every neighbouring unusable endpoint answers `err.badEndpoint`. Pasting a URL with one wrong character did it. |
  | `server.py` | `_safe_local_path` declined to check for an embedded NUL, on the documented claim that `realpath` raises | On Python 3.13/Windows `realpath` returns the string unchanged, so a NUL path was answered **404 instead of the contractual 403** — seven failing tests, and a safety argument that no longer held. |
  | `extractor.py` | `_extracted_any` counted `warnings` / `_extras` as extracted content, and `_payload_mismatch` opens with that predicate | The "truncated but nothing was rescued" guard **could not fire at all**. A max_tokens truncation that rescued one inner row returned `ok=True` with no warning. |
  | `extractor.py` | the occurrence-matrix normalizer dropped code-less rows, which is what `matrix.py:771` explicitly promised not to do | An inked cell the reader failed to name became a **confident absence** — the one claim this mode must never get wrong. Now kept with `cell_read="unread"`, counted separately from blank padding. |
  | `extractor.py` | `generic_table` skipped a column whose header was unreadable, compacting the list that the positional `cells` were sliced against | Headers and values **mislabelled**, and the last column's data dropped: `columns=[A,"",C]`, `cells=["a1","b1","c1"]` → column C showed `b1` and `c1` vanished, silently. |
  | `js/app.js` | picking a new image never invalidated the previous result | The page showed B's preview above A's table, and "Export all" wrote `{source_file: "B.png", result: <A's rows>}`. The `loading` flag added on 10-16 covers only the *decode* window; the gap between decode-finished and next-extract was left open. |

  Five P2 and three P3 findings are fixed alongside: `modes.__all__` listed a
  name that has never existed (`from rca_core.modes import *` raised
  `AttributeError`); `bed_parser.label_ordinal` answered an ordinal for
  `"zone Z1"` and `"23-25"` while its own docstring promised `None`; the
  two-stage matrix path declared `media_type="image/png"` for label images
  that are returned unmodified and are usually JPEG; `legend_codes` raised
  `AttributeError` on a legend list mixing dicts and strings, discarding a
  paid-for extraction; `is_loopback_bind("")` returned `True` although
  `bind("")` is `0.0.0.0`, so `--host "$RCA_HOST"` with the variable unset
  ran wide open **without printing the exposure warning**; `_extract_qualifiers`
  crashed on a non-string species; and `sections` could report
  `agreement="3/2"` with `agreement_count > runs`.

  The occurrence-matrix normalizer also moved its diagnostics onto
  `_warnings`. It alone wrote a bare `warnings`, which no consumer reads —
  `report.py`, `aggregate.py` and the JS mirror all read `_warnings` — so its
  two warnings reached no UI while the tests that assert on them read the
  local dict and passed. The tests are updated and a new one pins the key.

  Merging across runs now also keeps a numeric field numeric when the runs
  agree on the type (`aggregate._merge_scalar_field` and its
  `js/aggregate.js` mirror, changed together): occurrence-schema
  `columns[].index` merged `0`/`1` into `"0"`/`"1"` before, and
  `_merge_field_across_runs([3.0, 3.0])` answered `'3'`. The vote still keys
  on the string form — that is what makes `1` and `1.0` a single candidate —
  but the value returned is the representative's original. Text is still the
  answer when the runs genuinely disagree (`"8"` in one, `8` in another),
  which is the case where there is no single honest type.
  `tests/fixtures/frontend_parity_2026_09_20.json` was re-baselined by the
  generator that exists for exactly this (`tests/gen_frontend_parity_fixtures.py`),
  and `tests_diff_frontend_parity.js` reports **0 diverged** across its 1340
  cases — the two engines agree on the new values, not only on the old ones.

  Verification: `pytest` 3959 collected, **0 failed** (was 7 failed); `node`
  13 suites, **0 failed** (`tests_frontend.js` 825 → 846 with the new cases).

- **Adversarial re-review of the fixes above** — the round that checked the
  fixes rather than the bugs. Full report in
  `docs/ADVERSARY-2026-10-17.md`. A green suite carries no information about
  whether a fix is *correct*, so every change was re-probed with inputs that
  would have passed before it. **Three real problems, all fixed.**

  - The JS mirror of the type-preserving merge re-derived the winning value
    with `String(v).trim()`, which is **not** the vote key (`rcaStrForMerge`
    is). Integral floats and non-finite values therefore never matched: `[1.0, 1]`
    merged to `1.0` on Python and `1` in the browser, `[1, 1.0]` to different
    values by order, and `[NaN, NaN]` to `nan` versus `null` — where `null` is
    also how "no consensus" is spelled, so the value looked *dropped*.
    `rcaAggMode` already carried a representative map; the extra layer was
    both redundant and wrong, and is gone.
  - `bed_parser`'s two new guards were written against invented labels and
    rejected real well-log notation: `87 B1-4C` and `87 JED 8` answered `None`
    where the pre-fix code read `4` and `8`. Both consumers of `label_ordinal`
    — the exporter's only export-blocking invariant and the extractor's
    endpoint-swap heuristic — go quiet for a whole figure class when it
    returns `None`, and a check that never runs reports nothing while looking
    clean. The range test now requires the two numbers to be adjacent (or
    share a prefix, as in `A1-A5`), and the "names something else" test is a
    **closed** list instead of "anything unrecognised", so a well name or an
    institute code is read as the identifier it is.
  - The differential harness cannot carry non-finite values at all — the
    generator refuses them because `JSON.parse` rejects a bare `NaN` — which
    is exactly the class the merge change broke, and why all 1340 existing
    cases agreed throughout. New `tests/test_aggregate_scalar_type_2026_10_17.py`
    probes both engines on those inputs directly; 13 further cases that JSON
    *can* carry were added to the fixture (1340 → 1353, **0 diverged**).

  Both new test files were mutation-checked: reinstating each regression turns
  them red. One of the three (the JS merge) was introduced by the mirror edit
  in the previous entry — the round that synced the two engines also needed
  syncing, and "synced" was not the same as "equivalent".

  - **A second pass over the JS merge found a fourth divergence, in the same
    function, and it is the reason the three above took three attempts.** The
    type-preserving change also dropped the `.strip()` that
    `_merge_scalar_field` applies when it builds its vote pool, so `"Ml"` and
    `" Ml "` became two tied candidates instead of one and the sorted
    tie-break handed back the padded spelling — `" Ml "`, `" 7 "`, `" -1 "`
    where Python answered `"Ml"`, `"7"`, `"-1"`. Every one of those agreed
    before the change, so it was a regression, and the differential suite
    reported `0 diverged` through all of it: the fixture carried padded
    strings but never one merge field holding a padded **and** an unpadded
    value across runs. The triggering path existed; the triggering condition
    did not. The pool now holds `(trimmed key, original value)` pairs —
    Python's `votes` list — with the mixed-type early return in the same
    position Python puts it. Fifteen trim cases were added (1368 total);
    mutation-checked by reverting the trim, which turns the differential
    suite red at `agscal_318`.

    Fields routed through `rcaAggNorm` — species, section, range_base,
    range_top — were never affected, because that path trims too.

### Added

- **Occurrence matrices are now extracted in two stages: geometry locates the
  cells, the model reads them.** The single-pass prompt asked one call to do two
  jobs — find which cells contain a mark, then say what the mark is — and the
  measurements said the model could do the second and not the first. It read the
  labels well (taxa 20/22, authorities 19/19, conodont band 11/11, legend
  correct) and the dense cells badly: 0.355 recall at 0.579 accuracy, failing by
  omission rather than misreading. Injecting measured column geometry took
  recall to 0.565 and left accuracy at 0.574, which is the split that decided
  the design: the model could *see* the cells and was failing to match them to
  a position, and position is arithmetic's job.

  `rca_core/matrix.py` now locates every cell carrying ink (`detect_filled_cells`)
  and renders only those cells, isolated and magnified, as labelled contact
  sheets the model reads in one pass per sheet. Scored per FILLED CELL against
  the hand-checked 93-cell authority CSV, through the real `_MODE_DISPATCH`
  entry point:

  | strategy | precision | recall | n |
  |---|---|---|---|
  | `single_pass` (was the default) | 0.579 | 0.355 | 1 |
  | `cells` (geometry clause, one call) | 0.574 | 0.565 | 1 |
  | `grid_read` (new default) | **1.000** | **1.000** | 4 |

  The default moved on n=4 with a minimum of 1.000 across those runs, above the
  n>=3 bar set for changing a default. It falls back to `single_pass` — with the
  status rewritten to `single_pass_unverified` — whenever geometry is
  unavailable, a reply is truncated, the reader answers none of the tiles, or the
  model's axis counts disagree with the measured lattice, so the default can
  never turn an input into "no answer" or into an unearned verification claim.

  The letters are deliberately NOT read by template matching. A matcher hard
  coded for A/C/R scores perfectly here and fails on the next figure, where
  abundance is filled and open circles or 1/2/3. The geometric half is general
  because "which cells contain ink" names no letter set; the reading half has
  to be general too.

### Fixed

- **A label call that returned the right COUNT but the wrong CONTENT was being
  accepted and stamped `grid_cells`.** Held out on JPEG-degraded copies, one run
  produced 93 occurrences, 93 codes, zero blanks and `grid_cells` — and attached
  every cell to the wrong taxon, because the reader had collapsed 22 printed
  names onto 4. The axis-alignment guard compares counts, and the counts
  matched, so it had nothing to object to.
  `labels_are_internally_consistent` now adds the two facts a real taxon axis
  guarantees and a degraded transcription breaks: every row geometry found ink
  in must have a name, and two DIFFERENT printed rows do not share one. Either
  violation falls back.
- **An implausible legend was shipped verbatim.** The same held-out run returned
  13 legend entries for a 3-code figure, and 13 rows in the exported table are
  10 rows of invention. The retry trigger was "the legend is empty"; it is now
  "the legend cannot be a legend" — too many entries, an entry with no code, or
  a duplicated code — and a list that is still implausible afterwards is dropped
  rather than exported.
- **Two payload keys the two-stage path emitted were silently dropped by the
  normalizer, making complete-looking runs report nothing.** `taxon_rows` is the
  RESULT's key name but the normalizer builds it from `parsed["rows"]`, so the
  assembled payload lost every row label; and `strat_rows` entries that are not
  dicts are skipped outright, so a bare band label vanished. Both are now emitted
  in the shape the normalizer actually consumes, and both are covered by tests
  that run the real normalizer rather than inspecting the payload.
- **The prompt let the model decline.** It said "if the centre is blank write
  ''", which makes a blank the *safe* answer under uncertainty — and these
  glyphs are ~8 px tall, so the model is often uncertain. Three of six runs
  answered `''` for 64 of 93 tiles and precision fell to 0.29. Every tile handed
  to the reader has already been proven by the ink test to contain a mark, so
  `''` is never correct; the option is gone and the legend codes are supplied as
  a closed vocabulary.
- **The reader was given tiles it could not resolve.** At 6x magnification the
  per-cell reading is unreliable; separating magnification from sheet size with
  everything else fixed gave 8x/31-tiles **1.000 1.000 1.000 1.000 1.000** (n=5)
  against 6x/31-tiles **0.968 1.000 1.000** (n=3). Magnification is the
  decisive variable; the split is a cheap safeguard.
- **The label prompt asked the model to count rows, and it miscounted.** 24
  rows came back for a 22-row table, the alignment guard correctly refused the
  result, and the whole run degraded to `single_pass`. The measured row and
  column counts are now in the prompt, and non-taxon labels (conodont bands,
  correlation strips) are explicitly excluded from `rows`.
- **An empty legend emptied the class column of every exported row.** The legend
  is a few words in a corner of a busy figure and shared that call with 22 taxon
  names and 18 rotated headers; it came back empty in one run of three. It is now
  re-asked once, with a dedicated prompt, when the first read is implausible.
  *Extended after held-out testing — see the 13-entry legend above: emptiness was
  only one of the two ways this goes wrong.*
- **Two tests in `test_occurrence_strategy_defaults_2026_10_16.py` never ran.**
  Both took a `self` parameter, so pytest treated them as fixtures and ERRORed
  at setup; their assertions had been unenforced since the file was written.
- **`log` was used in `extractor.py` without ever being bound.** Inside the
  `grid_read` fallback handler, so a handled transport failure would have
  raised `NameError` — the one thing that handler exists to prevent.

### Held-out results (2026-10-17)

Scored on pixels the path had never seen, with "which cells contain ink"
recomputed locally on each image so the ground truth follows the input.

| variant | geometric cells | `grid_read` | occurrences | distinct taxa | blank codes | `single_pass` |
|---|---|---|---|---|---|---|
| pristine | 93 | `grid_cells` | 93 | 22 | 0 | 73 |
| JPEG q60 | 93 | `grid_cells` | 93 | 22 | 0 | 73 |
| JPEG q40 | 93 | `grid_cells` | 93 | 22 | 0 | 60 |
| downscale 55% | 93 | `grid_cells` | 93 | 22 | 0 | 57 |
| faded toner | 93 | `grid_cells` | 93 | 22 | 0 | 75 |
| sensor speckle | 93 | `grid_cells` | 93 | 22 | 0 | 63 |
| skew 1.2° | — | **detector refused** | — | — | — | — |
| skew + JPEG + speckle | — | **detector refused** | — | — | — | — |

Six non-matrix figures (SEM plate, outcrop photographs, a biographical page, two
range charts) produced **no fabricated cells at all** — all six declined through
the empty-response gate, and `grid_read` behaved identically to `single_pass` on
every one.

**Known limit, stated rather than buried:** `detect_grid` finds axis-parallel
rules only. A 1.2° skew — within what a flatbed scan produces routinely — makes
it decline, and the run falls back to `single_pass`. The module already has a
`deskew` stage that is not wired into this path; until it is, the honest answer
for a skewed page is the older, caveated one.

## [0.1.1] — 2026-10-16

### Fixed

- **`occurrence_matrix` had no table configs in the browser.** `js/table.js`
  carried a `_looks_*` predicate for every mode except this one, so
  `rcaTableConfigs` fell through to the range-chart branch — which returns its
  four configs unconditionally. On a real 73-record result the browser produced
  **8** tables against the desktop's **3**: the three real ones plus four
  PHANTOM EMPTY ones (`sections` / `species_ranges` / `biozones` /
  `other_fossils`), and the real three arrived through the generic fallback with
  humanised, untranslatable headers. `rcaLooksOccurrenceMatrix` and
  `rcaOccurrenceMatrixConfigs` now mirror the Python twin, in the same branch
  position (above abundance, since a partially-migrated matrix answer carries
  abundance-shaped rows).
  *This was latent, not user-visible:* `js/minimax.js` refuses the mode inside
  `extractRangeChart` before the backend dispatch, the desktop renders through
  `exporter.py`, and the browser has no result-import path. Verified by
  execution, not by reading the guard. It was one commit from live, since
  flipping `browser_supported` to `True` is the documented way to enable it.
- **Ten i18n keys the occurrence-matrix tables referenced were never defined.**
  `Translator.t` returns an unknown key *verbatim*, so the desktop rendered
  `sec.occurrences (73)` and headers reading `col.authority` / `col.code` /
  `col.class` in a Chinese UI; the browser renders the same gap as `[?col.x]`.
  Added in zh/en/ja on both sides.
- **`col.index` meant two different things.** It is the ROW-NUMBER header
  (`build_table_export`'s `index_label`, and `js/table.js`'s
  `[t('col.index')].concat(cfg.cols)`) — and the GUI prepends its own `#`. The
  occurrence-matrix `columns` table also used it as a data column, so its header
  read `# | # | Label | Kind` in every language. The data column is now
  `col.columnIndex`.
- **Generic-table headers disagreed between engines.** `rcaColLabel` tested
  `t(key) !== key`, but `t()` renders a missing key as `[?col.topic]`, which is
  never equal to the key — so the test accepted *every* column and the browser
  headed an unrecognised column `col.topic` where the export wrote `Topic`. It
  now tests membership of the English table, exactly as `_col_label` does.
- **The browser-unsupported guard ran BEFORE the backend dispatch.** Reproduced
  live: with the server running, connection mode 「自动」, and 出现矩阵 explicitly
  selected, Extract produced **ZERO network requests** and told the user to
  switch to 「经本地后端」 or start the server — advice already followed on all
  three counts. `extractRangeChart` refused before `rcaCallBackend` was ever
  reached, so the mode was unavailable in the browser even though the backend
  handles it perfectly well (`modes.py` `SERVER_MODE_NAMES`). The guard exists
  because the DIRECT path has no normalizer for those modes; on the backend path
  it was simply the wrong tool. It now sits after the dispatch and guards only
  what it is for. *This also corrects the claim in the entry above, which said
  the refusal happened before the backend dispatch — verified by execution, and
  the browser went on to prove it by sending the request.*
- **The guard was not re-checked after automatic classification.** It ran while
  `mode` was still the literal `auto` (not in `RCA_BROWSER_UNSUPPORTED_MODES`),
  and the classifier then rewrote `mode` to whatever it recognised. There is no
  prompt branch or normalizer below for `paleomap` / `scatter_plot` /
  `chemical_stratigraphy` / `occurrence_matrix`, so the request went out with
  `RANGE_CHART_SYSTEM_PROMPT` and the answer was normalized as a range chart —
  a confidently wrong result, reachable from the default path with no
  misconfiguration. Re-checked after the classification round, and the
  extraction request is refused rather than sent.
- **Cancelling left the results area blank.** `setBusy(true)` hides BOTH
  `#results-empty` and `#results-content`, and only the `!res.ok` branch put one
  back — so the four early returns around it (superseded extraction, cancelled
  twice) all returned to an empty card. With a prior result the user's data was
  still in memory and unreachable until they re-ran the extraction. The
  invariant is now stated once, in `updateActionButtons`, and every exit gets it
  because `setBusy(false)` routes through that function.
- **Extract could send the previous image while the new one was still
  decoding.** `handleFile` deliberately defers `state.file` / `state.dataUrl`
  until the decode resolves, so during that window both still describe the
  PREVIOUS image while the file input already shows the new one. Nothing keyed
  off the window — `updateActionButtons` looked only at `state.busy`, which
  `handleFile` never sets — so Extract and Ctrl+Enter were both live. Worse,
  the misattributed result SURVIVED: the load's token guard covers only the
  load, so once the new image arrived the rows rendered normally and every
  export named the new file. `state.loading` now locks the window.
- **`occurrence_matrix` had no merge schema in the browser.** Python has had
  `OCCURRENCE_MATRIX_SCHEMA` in `SCHEMA_BY_MODE` and `_auto_detect_schema`
  since 2026-10-15; `js/aggregate.js` mirrored neither, so a two-run matrix was
  merged with the range-chart schema's `species_ranges` primary — which this
  payload does not have. The consequence is data loss, not degradation: the
  merge produced nothing for the real rows, so a locality only the second run
  reported vanished and two localities of one genus collapsed into one record.
- **A `null` in a plain-string list became a phantom row.** `str(None)` is the
  literal `"None"` and `String(null)` is `"null"`, so one null in
  `other_fossils` produced a ghost taxon on the desktop AND a differently-spelled
  ghost in the browser. Both now emit nothing. The mixed-list branch had the
  guard on the JS side only.
- **A cancellation was retried like a transient failure.** The backoff loop
  caught every rejection alike, so pressing Cancel still went round it:
  measured through the real `extractRangeChart`, 4 fetch attempts and 4606 ms
  before the user's own command took effect, against 1 attempt and 303 ms for
  the control.
- **`in` walked the prototype chain when validating node ids.** `idToNode` is a
  plain `{}`, so `root_ids: ["toString"]` and a `parent` of `"constructor"`
  passed validation against keys the model chose, and the tree was built on a
  node that does not exist.
- **The occurrence sheet's title did not say where its cells came from.**
  `sec.occurrencesUnverified` existed in all three dictionaries with no
  consumer, so the browser headed identical data 「出现记录」 while the desktop
  exporter headed it 「出现记录 (格子未经图版网格核验)」. `cell_status` now drives
  the choice, defaulting to the CAVEATED title — absence is not proof, since
  merged results and every pre-field payload lack the key. Found by loading the
  real page: the diff-parity suite compares JS output against Python output, and
  a branch missing on BOTH sides compares equal.
- **`rca_core.__version__` read `2.0.0`** while the project shipped 0.1.0. It is
  not cosmetic: `exporter._package_version()` stamps it into the header of every
  CSV/TSV/JSON/XLSX exchange file, so every export claimed to come from a version
  of this software that has never existed. No test pinned it — nothing under
  `tests/` mentions `__version__` — which is how a number that was wrong from the
  start survived the whole release history. It now tracks the CHANGELOG.

### Added

- **`tests/test_occurrence_matrix_merge_parity_2026_10_16.py`.** The browser
  must merge occurrence matrices the way the desktop does. Two things make it
  real rather than decorative: it passes the keymap into `rcaMergeResults` the
  way `js/app.js:1318` does (that function does NOT auto-detect, while Python's
  `merge_results` does — comparing them without it compares a range-chart merge
  against an occurrence merge), and it compares the merged RESULT rather than
  the chosen primary, because two engines agreeing on a list name while writing
  different key sets is the divergence that costs data.
- **The `gated` payload shapes the occurrence detectors were missing.** Five
  shapes where `occurrences` is empty BY DESIGN and `taxon_rows` carries the
  names. Before them, every such shape classified as "not a matrix" on BOTH
  engines, so no shape in `tests/test_table_config_detectors_parity.py` could
  tell them apart and the one-sided fix exporter.py had just received passed
  that suite untouched.
- **`tests/test_table_configs_parity.py`.** Cross-engine parity for the table
  configs themselves (ids and columns), which the harness never covered — it
  proved the two engines picked the same *branch* while nothing replayed
  `rcaTableConfigs` against `get_configs_for_result`. Pins the invariant that
  holds on every payload (the browser may add the four documented editing
  shells, but must never drop or reshape an exported table) separately from the
  one that holds only on specific branches.
- **occurrence-matrix shapes in `tests/test_table_config_detectors_parity.py`**,
  and the scope assertion no longer pins `len(PAIRS) == 5`. That count asserted
  the hand-written list against its own length instead of against the detectors
  the code actually has, which is precisely why a one-sided `_looks_*`
  addition passed unnoticed. The set is now enumerated from the exporter module
  and from what `js/table.js` really defines.
- **`cell_status` on every occurrence-matrix result.** `single_pass_unverified` /
  `grid_cells` / `not_attempted` / `failed`. It records **where the cells came
  from**, not how confident they are, and it is taken from the caller's code path
  rather than from the model's payload — the model has no way to know whether its
  own column coordinates were checked against the figure's geometry. `single_pass`
  can never earn `grid_cells`, and a **missing** status means unverified, because
  `merge_results` drops scalar root keys and older payloads predate the field.
  The exporter turns it into the sheet title, so the caveat appears where a reader
  of the workbook looks first.
  *Why it exists:* on a real 22 × 18 matrix the labels scored 20/22 taxa, 7/8
  authorities, 11/11 conodont zones, while the cells scored 0.58 accuracy at 0.36
  coverage — and the result object looked identical in both cases.
- **An empty-response gate for `occurrence_matrix`.** A near-empty body is valid
  JSON, so it passed both the truncation check and the parser and reached the user
  as "this figure contains no occurrences". Two real runs measured 528 and 40
  output tokens where the same figure costs 4190–5587. Those are now rejected
  with the numbers in the message. The floor is **per strategy** and is `None`
  where it has not been measured: reusing the full-answer floor against a
  labels-only answer would reject correct output, and guessing one rejects good
  output silently.
- **`rca_core/grid.py`.** Deterministic ruling-line detection, so the model is
  not asked to locate cells by eye. Returns `None` rather than guessing —
  a borderless matrix is not exotic, and the caller degrades to the single-pass
  question and says so in `cell_status`. PIL + numpy only; `torch` and `scipy`
  were just excluded from the PyInstaller build.
- **`OCCURRENCE_LABELS_SYSTEM_PROMPT`.** A labels-only question (headers, taxon
  rows, legend) at **4546 characters against the matrix prompt's 7683 (−40.8 %)**.
  It *removes* the cell contract rather than adding rules to it, which is the
  opposite direction from the prompt change reverted earlier today. Mirrored to
  `js/prompt.js` with byte-equality guaranteed by construction.
- **`cell_strategy` on `extract_occurrence_matrix`.** `single_pass` (default,
  unchanged) / `gated` / `cells`. `cells` injects the locally measured pixel
  spans into the matrix prompt; `gated` uses the labels prompt and attempts no
  cell reading. `taxon_rows` carries the labels-only path's taxon names, which
  cannot ride in `occurrences` because that list is sparse by contract and drops a
  row with no code.
- **157 tests** across `tests/test_grid.py` (35),
  `tests/test_occurrence_cell_status_2026_10_16.py` (48) and
  `tests/test_occurrence_strategy_defaults_2026_10_16.py` (4), each proven by
  mutation: 16/16, 15/15 and 10/10 red.

### Changed

- **exe 308.5 MB → 257.9 MB (−16.4%), with no loss of function.** Measured on
  the 0.1.1 baseline rather than estimated: 839.1 MB of uncompressed payload
  over 4342 entries, read from `build/…/PKG-00.toc`. (Site-packages directory
  sizes over-count by up to 3× — torch is 1874 MB on disk and 98.5 MB inside the
  exe — so the 0.1.0 exclusion audit's inputs and this one's are not the same
  quantity.)

  Two entries, 148.9 MB of payload:

  - **87.4 MB of QtWebEngine DevTools bulk** —
    `qtwebengine_devtools_resources.debug.pak` (75.8) and
    `qtwebengine_devtools_resources.pak` (11.6). These load only when a
    profile is opened with DevTools enabled; nothing here calls
    `showDevTools()` or sets a DevTools attribute. PyInstaller's
    `collect_qtwebengine_files()` sweeps the whole `resources/` directory with
    no filter, so debug builds ride along in every bundle.
  - **58.9 MB of opencv ffmpeg video decoders** — `opencv_videoio_ffmpeg500_64`
    (30.9) and `ffmpeg4110` (28.0), for a tool that extracts from still images.
    `deskew.py` and `extractor.py` call `resize` / `GaussianBlur` / `Canny` /
    `HoughLinesP` / `fastNlMeansDenoising` / `createCLAHE` / `cvtColor`, all
    imgproc or photo. Verified by moving both DLLs aside and running every call
    site in a fresh interpreter: 10/10 passed, `import cv2` still succeeded,
    restored afterwards by digest. The existing exclusion audits cover Python
    imports and cannot see a binary dropped from `a.binaries`, which is why
    this one needed its own probe.

- **Corrected a false claim that had been protecting 204.8 MB.** The build spec
  and the 0.1.0 release notes both stated that dropping QtWebEngine "removes
  79.8 MB of Chromium and breaks the main content area", because
  `gui_fluent.py` "embeds the web frontend in the native window". It does not.
  `QWebEngineView` is used in exactly two places — `PhyloTreeWidget`, the D3
  phylogenetic-tree renderer, and the rich History detail dialog — and both
  already have absence paths: `HAS_PHYLO_TREE_WIDGET = False` leaves
  `self.phylotree` as `None` and `_show_phylotree()` logs and returns, and
  `HistoryDetailDialog` falls back to a read-only `QTreeWidget`. A false claim
  that something is mandatory is the most expensive kind of wrong, because it
  reads as a constraint and nobody revisits it.

  **0.1.1 keeps QtWebEngine.** Dropping it would save a further ~68 MB; that was
  put to the user and declined, so the 204.8 MB above is Chromium that is
  demonstrably *not* free to remove — recorded so the next person measures
  instead of citing 0.1.0's reasoning. The functional cost of dropping it would
  be: no interactive D3 tree (the right panel falls back to the empty state) and
  no rich History detail (a plain `QTreeWidget` instead).

- **UPX was considered and rejected on measurement, not taste.** PyInstaller's
  onefile archive is already zlib-compressed — 839.1 MB of payload shipped as
  323.5 MB, so 61% of the bytes were compressed before any outer tool ran. UPX
  over already-compressed data returns single-digit percent, so it was not worth
  adding a binary compressor and another build cycle to chase it. The lever that
  works here is payload, not compression ratio.

- **The truncation warning names its numbers.** It was copy-pasted identically
  into eleven extractors and reported no quantities at all, so a reader could not
  tell 16000 from 32000. That ambiguity had already been misread once — an
  `output_tokens=16058` run was taken as "the cap was hit, raising it will not
  help", when 16058 had only reached `DEFAULT_MAX_TOKENS` and `clamp_max_tokens`
  still allowed 32000. One shared helper now reports the actual usage, this run's
  limit, and the program ceiling.

### Known limitations

- **Filled cells are still unreliable, and the default strategy has not changed.**
  Measured once on the real 22 × 18 figure, same entry point and scorer:

  | | single_pass | gated | cells |
  |---|---|---|---|
  | output tokens | 5151 | 1103 | 6241 |
  | column headers | 9/15 | **12/15** | 11/15 |
  | taxa | 20/22 | 20/22 | 20/22 |
  | authorities | **7/8** | **7/8** | 6/8 |
  | conodont zones | **11/11** | **0/11** | 10/11 |
  | legend | 3 | **0** | 3 |
  | cell accuracy | 0.579 | — | 0.574 |
  | cell coverage | 0.355 | — | **0.565** |

  `gated` reads the most headers and costs a fifth of the tokens, but its schema
  has no `strat_rows` and it returned no zones and no legend — so it is **not** the
  default; `single_pass` keeps everything the others have. `cells`, with measured
  pixel spans injected, lifts correct-cell **coverage** 0.355 → 0.565 at flat
  accuracy, which matches the earlier shift-scan finding that the failure is
  column *identity* rather than reading ability. Offered as an opt-in: n=1 does not
  justify flipping a default, and 0.57 accuracy is not verification either way.
- **Tiled per-row extraction was tried and rejected**, so the "next step" recorded
  in 0.1.0 is not the way forward. Cutting the figure into 5-row strips with a
  repeated header reached full row coverage (22/22) but fabricated **four genera
  that do not appear in the figure** (`Archaeopyramisa engurensis`,
  `Archaeocathartes rectangularis`, and two more) — trading omission for
  hallucination, which is the worse failure for a table that gets cited.
  Upscaling was also measured and made cells *worse* (0.429 → 0.397 → 0.377 at
  1×/2×/3×) while improving only the labels.

### Verification

Python **3921 passed / 0 failed / 0 errors / 52 skipped**. Browser: 13 suites
green, `tests_frontend.js` at 825 assertions (from 760), differential parity
fixture at **1332 matched / 0 diverged**.

Every fix in this release was reverted in turn and required to turn its own
suite red — 8 mutations, 8 reds, files restored by content afterwards. The two
guards that could not have seen a one-sided change (differential parity compares
JS output against Python output, so a branch missing on BOTH sides compares
equal) were written as new cases instead: five `gated` payload shapes in the
detector suite, and a merge suite that passes the keymap the way `app.js` does
rather than the way neither engine's default does.

Browser, live: the exact state that produced **zero** network requests before
the fix now produces `GET 200` + `POST 200` on `/api/extract`, and the
occurrence sheet is headed 「出现记录 (格子未经图版网格核验)」 instead of the bare
「出现记录」.

Packaging: both exes were launched from this tag's source. The windowed build
opens a window titled `Range Chart Analyzer`; the console twin serves on a port
and answers `GET /` with 200. The shipped `js/` was read back *out of the
running exe* — `defaultMaxTokens = 16000`, `maxMaxTokens = 32000`,
`err.imageLoading`, `sec.taxonRows`, `data.cell_status`, the `taxon_rows`
conditional, and the post-classification guard were all confirmed present in
the binary rather than only in the repository.

## [0.1.0] — 2026-10-15

First tagged release. The repository had no tags before this, so there is no
earlier version to compare against.

### Added

- **`occurrence_matrix` chart type.** Taxon × locality matrix figures now extract
  as a *sparse long table*: one record per FILLED cell, with a locality legend
  and the figure's non-taxon comparison band. Blank cells are dropped rather
  than emitted, and the drop is counted in the run's warnings.
- **`generic_table` chart type.** An honest fallback for tabular figures that
  match no known genre. Deliberately the opposite polarity to
  `occurrence_matrix`: dense, short rows padded to the column width, because a
  fallback must not invent sparsity it cannot see.
- **`rca_core/modes.py`.** A single declaration of all 10 chart modes, replacing
  mode strings that had been scattered across 9+ places in the Python core and 7
  in the browser. It is DATA ONLY with no sibling imports, so consumers resolve
  `"module:attr"` references lazily and the registry cannot form a cycle.
  Wiring it in was gated on 70 equivalence checks proving the derived values
  were byte-identical to the constants they replaced.
- **Packaging.** `RangeChartAnalyzer.spec` plus two one-file exes (window and
  console), published as Release assets.
- **PyInstaller exclude audits** (`scripts/audit_exclude_imports.py`,
  `scripts/audit_exclude_suite.py`), so the exclusion list stays verifiable
  instead of being a list someone trusted once.

### Changed

- `DEFAULT_MAX_TOKENS` 4000 → 16000, in the core and in `js/config.js`
  together. A 22 × 18 occurrence matrix needs 4417–5428 output tokens as one
  JSON record per filled cell; at 4000 the model was cut off mid-answer and the
  final row was never emitted, with `stop_reason == "max_tokens"` surfacing
  only as a warning string. `max_tokens` is a ceiling, not a reservation.
- exe size 681.5 MB → 308.5 MB, by excluding 27 packages the tool never calls
  (torch, polars, pyarrow, scipy, spacy, bitsandbytes, …). `QtWebEngine` is
  deliberately kept — `gui_fluent.py` embeds the web frontend with
  `QWebEngineView`, so it is not dead Chromium weight.
- Chart-mode autodetect reads its keyword tables from the registry rather than
  from 16 hand-maintained tuples in `chart_mode.py`.

### Fixed

- `_looks_occurrence_matrix` keyed off `columns`, which `generic_table` also
  emits — a generic table was silently exported as zero rows.
- The browser's quality detector disagreed with the core's about which mode a
  payload belonged to, because the branches were in a different order.
- An `or` chain short-circuited on the integer `0`, so a reading of `0` was
  treated as a blank cell, dropped, and reported as a false warning.
- A `col_index` that contradicted its `site` was accepted silently; it now
  raises `column_index_conflict` and marks the row uncertain.
- `modes.py` pointed `chemical_stratigraphy` at a normalizer that did not exist.
- The extraction prompt licensed name "correction": it asked for *standard
  Latin names*, which permits replacing what was read with what the model
  believes. It now says **transcribe the taxon name exactly as printed, letter
  by letter**, and to mark a row uncertain rather than guess.
- A contract clause saying "never delete the row" contradicted the sparse rule;
  replaced with one that applies only where the contract is dense.

### Known limitations

- On a dense 22 × 18 matrix the recall of filled cells is **51 %** (73 of 93
  measured against the figure's own grid). Sparse rows are essentially exact;
  the loss concentrates in the densest rows. It is not a pipeline defect —
  column mapping was verified against the figure by shift analysis, and
  upscaling the image made results worse, not better. The next step is tiled
  per-row extraction with a merge, which is not implemented.
- The genus printed `Capmuchosphaera` is read as `Capnuchosphaera`
  consistently. Small italic serif text at the figure's native resolution is
  the limit here.

[Unreleased]: https://github.com/syxscott/Range-chart-Analyzer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/syxscott/Range-chart-Analyzer/releases/tag/v0.1.0
