"""Name grounding: which extracted names are actually PRINTED in the figure?

THE DEFECT THIS EXISTS FOR
==========================
Measured on one published figure (Motoyama 2019, Fig. 1 - a Neogene radiolarian
zonation/correlation chart), four runs of the SAME image produced 36 / 91 / 45 /
51 zone rows, and every one of those four runs included names that appear
nowhere in the image - ``Buccinosphaera invaginata``, ``Calocycletta costata``,
``Callimitra rebus``, ``Arachnocorallium pentacrinoid`` and so on. Those are all
REAL Neogene radiolarian names, so the output is indistinguishable from a good
extraction by inspection: the model completed the zone list from its training
knowledge instead of reading the pixels.

``rca_core.quality.score_range_chart`` returned grade A / score 1.0 / zero
issues on all four. That is not a bug in the grader - completeness, ordering,
coverage and endpoints are all genuinely fine, and no structural check can see
that a string is absent from an image. It is the one thing the grader CANNOT
do, so it must not be asked to. This module is the missing signal, and it is
deliberately a SEPARATE, separately reported one.

WHY A SECOND LLM CALL
=====================
The check is "does this literal string occur in this picture", which is a vision
question with no non-LLM answer available here: there is no OCR engine in the
pipeline, and a taxon name in a rotated header of a dense correlation chart is
exactly the case where OCR is least reliable. So the same provider that
produced the result is asked a second, much smaller question about it. The call
goes through ``rca_core.llm`` like every other call in the app - this module
writes no HTTP of its own.

THE VERIFIER IS PROMPTED TO REPORT ABSENCES ONLY
================================================
Asymmetry is the design, and it is deliberate. A verifier asked "which of
these names are in the figure?" answers with a list, and a model under pressure
to be useful will always produce a list - so its silence becomes indistinguishable
from laziness. A verifier asked "which are you CERTAIN are absent?" can only
speak up when it has positive evidence, so a wrong-but-quiet verifier degrades to
the honest, conservative answer ("nothing is missing") instead of inventing
absences. The false-negative direction is the one we can afford; a clean report
costs a re-read, a false accusation costs the user their trust in the tool.

MEASURED, AND THE ONE PLACE THAT ARGUMENT DID NOT HOLD
======================================================
Running the check over ten real results (round 2) showed the asymmetry alone
is not enough. Of the 7 absences it reported across 10 figures, my own reading
of the images confirms 2 true positives and 5 false positives:

  Zhang Lei et al. 2018 Fig. 3   Kungurian Stage / Artinskian Stage /
                                 Sakmarian Stage / Asselian Stage
                                 -> ALL FOUR PRINTED, in the rotated
                                 stage-header column. Four false positives.
  Ozsvart et al. 2016 Fig. ...  UAZ 16 - Spica -> PRINTED (16 sits inside the
                                 merged "Spica" cell). One false positive.
  Ozsvart et al.                 Xitus subtilis -> figure prints "Xitus
                                 subitis". TRUE POSITIVE.
  Souza et al.                   "Geomagnetic Polarity Time Scale (GPTS)" ->
                                 the header reads "Paleomagnetic scale". TRUE
                                 POSITIVE.
  Tomimatsu et al. 2022 Fig. 8   no_names - see NAME_SOURCES.

The stage-name failure is not the verifier being unreliable; it is the
question being asked. A stage printed as a rotated axis header is among the
most visually salient text in the figure, but it is not a taxon row, and the
verifier resolved "not a taxon row" into "not present". Rule 2 already
forbade reporting names printed "in a header"; it did not say that a name
which IS a header is present. Rules 3-6 of the prompt now say so explicitly,
each anchored to a case measured above, and the candidate list is framed as
"names printed in this figure, of every kind" rather than "taxon names".

Tightening the presence test is the safe direction: every rule added above
moves names from absent to present. That is also why each one is paired with
a positive instruction to keep reading, and why the regression test pins the
two true positives above - a prompt that stops reporting anything would score
better on false positives while being worthless.

EVERY FAILURE PATH IS "NOT PERFORMED", NEVER "CLEAN"
====================================================
A timeout, a non-200, an unparsable reply, a truncated reply, a missing
provider, or an exception anywhere in here yields
``status == GROUNDING_NOT_PERFORMED`` - never an empty ``unverified`` list.
Collapsing "I checked and found nothing missing" into "I could not check" is
precisely the failure this module was written to eliminate, so the three
outcomes are kept apart and the caller can tell them apart:

    GROUNDING_VERIFIED       the check ran; ``unverified`` is the evidence
    GROUNDING_NOT_PERFORMED  the check could not run - unknown, not clean
    GROUNDING_NO_NAMES       this mode carries no verifiable names at all

Nothing in this module raises and nothing in it blocks an extraction.

SCOPE OF THE NUMERIC SCORE
==========================
This signal is NOT folded into the weighted quality score, and that is a
decision rather than an omission. The four weighted dimensions are computed
from the result alone and are reproducible offline; this one depends on a second
model call that can fail, time out, or be wrong. Letting it move the composite
would let an unreliable signal outrank the reliable ones, and a grade that
changes between two runs of one image is worse than no grade. It is reported as
its own warning + its own field instead. See ``_apply_grounding`` in quality.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .json_utils import safe_json_loads
from .llm import call_llm_api_with_retry

__all__ = [
    "GroundingResult",
    "check_name_grounding",
    "collect_candidate_names",
    "flag_unverified_rows",
    "build_verifier_user_text",
    "GROUNDING_SYSTEM_PROMPT",
    "GROUNDING_ROW_FLAG",
    "GROUNDING_VERIFIED",
    "GROUNDING_NOT_PERFORMED",
    "GROUNDING_NO_NAMES",
    "NAME_SOURCES",
    "ROW_IS_NAME",
    "BARE_STRING_ROW_TABLES",
    "GROUNDING_MAX_TOKENS",
    "GROUNDING_MAX_CANDIDATES",
]

# ---------------------------------------------------------------------------
# Statuses. Three, never two.
# ---------------------------------------------------------------------------

#: The verifier ran and returned a usable answer. ``unverified`` is whatever it
#: reported - possibly empty, which means "checked, nothing absent".
GROUNDING_VERIFIED = "verified"
#: The check could not be completed. UNKNOWN - never to be read as clean.
GROUNDING_NOT_PERFORMED = "not_performed"
#: This payload carries no name-bearing table, so there is nothing to check.
GROUNDING_NO_NAMES = "no_names"

# ---------------------------------------------------------------------------
# Which keys hold a name that is printed in the figure.
#
# ONE TABLE. A mode is covered by adding its keys HERE, not by editing five
# branches: every collector, every row-flagging pass and every test reads this
# tuple. The keys and row keys were read off the normalizers in
# rca_core/extractor.py (see ``_PRIMARY_ID_KEYS`` / ``_KNOWN_CHEMICAL_STRAT_*``
# / ``normalize_columnar_result``) and the column lists in
# rca_core/exporter.py::get_configs_for_result, rather than guessed.
#
# A key that is absent from this table is a key whose contents are NOT claimed
# to be printed in the figure - so a result that carries only those keys gets
# GROUNDING_NO_NAMES instead of a fabricated "verified clean".
# ---------------------------------------------------------------------------

#: Row key meaning "the row IS the name". ``other_fossils`` is normalised to a
#: list of PLAIN DISPLAY STRINGS (``extractor._merge_other_fossils`` keeps
#: only ``isinstance(x, str)``; ``_other_fossils_from`` stringifies a dict
#: entry), so a row has no dict to read a field from. Empty string rather than
#: ``None`` so it can never collide with a real field name, and so this stays
#: ONE table rather than a second one keyed by table name.
ROW_IS_NAME = ""

NAME_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # range_chart
    ("species_ranges", ("species",)),
    # abundance_diagram (also the taxon axis of the step-0 baseline shape)
    ("abundances", ("taxon",)),
    # range_chart biozones
    ("biozones", ("name",)),
    # zonation_chart zones; abundance_diagram zones share the same row key
    ("zones", ("name",)),
    # zonation_chart column headers (the named frameworks being correlated)
    ("zonations", ("name",)),
    # occurrence_matrix (2026-10-15)
    ("occurrences", ("taxon",)),
    # occurrence_matrix locality headers + generic_table column headers; the
    # normaliser accepts "label" OR "name" for either, so both are read.
    ("columns", ("label", "name")),
    # zonation_chart correlation edges - both ends are printed zone names.
    ("correlations", ("from_zone", "to_zone", "from_zonation", "to_zonation")),

    # --- AUDIT item 10: modes that carry names and were checked for NOTHING.
    # Round 2 measured the cost: Tomimatsu et al. 2022 Fig. 8 was classified
    # ``chemical_stratigraphy``, so every name it carries sat in one of the
    # three tables below, ``collect_candidate_names`` returned [], and the check
    # answered ``no_names`` - a figure whose entire content is taxon ranges
    # with ZERO names verified. An unchecked result reported no problem; that
    # is the failure mode this table exists to prevent.
    #
    # chemical_stratigraphy: every one of its tables is name-bearing and none
    # was listed. ``sample_id`` is the row's printed label
    # (``extractor._PRIMARY_ID_KEYS["data_points"]``); ``stage`` is the
    # chronostratigraphic name on the age axis. ``events`` and ``intervals``
    # carry ``name`` in their normalisers AND as a column in the generic-table
    # exporter path (chemical_stratigraphy is in ``_TABLELESS_MODES`` but still
    # exports its three tables).
    ("data_points", ("sample_id", "stage")),
    ("events", ("name",)),
    ("intervals", ("name",)),
    # abundance_diagram locality headers - the column labels every abundance
    # row is keyed on, printed verbatim above each column.
    ("sites", ("name",)),
    # range_chart section headers carry ``name`` (``_normalize_section_into``);
    # columnar_section carries ``id`` instead (``normalize_columnar_result``
    # writes id/group, never name). One table key, two modes, both read.
    ("sections", ("name", "id")),
    # range_chart free-text fossil labels. Rows are PLAIN STRINGS after
    # normalisation (11 of 12 FAD/LAD events landed here in an earlier round),
    # so ROW_IS_NAME carries them; ``fossil``/``text`` are the dict-shaped
    # spellings the exporter's own row lambda reads for third-party producers.
    ("other_fossils", (ROW_IS_NAME, "fossil", "text")),
)

#: Tables whose rows may be plain strings rather than dicts. ``other_fossils``
#: is the only one today; the collector treats a non-dict row in one of these
#: as the name itself instead of skipping it.
BARE_STRING_ROW_TABLES = frozenset(k for k, keys in NAME_SOURCES
                                   if ROW_IS_NAME in keys)

#: Deliberately small. The reply is meant to be a handful of names, and the
#: verifier is asked for ABSENCES only - a large budget invites a large answer.
GROUNDING_MAX_TOKENS = 1000

#: Upper bound on the inline candidate list. A pathological result (thousands
#: of rows) would otherwise blow the prompt up for no gain; the overflow is
#: counted and reported rather than silently dropped.
GROUNDING_MAX_CANDIDATES = 250

#: Default per-call timeout. Shorter than an extraction's 120 s: this is one
#: question with a short expected answer, and it must not hold up a result.
GROUNDING_TIMEOUT_SEC = 90

#: The ``_warning`` tag written onto a row whose name the verifier did not find
#: in the figure. Matches the extractor's flag vocabulary (``_add_row_flag``).
GROUNDING_ROW_FLAG = "name_not_in_figure"

GROUNDING_SYSTEM_PROMPT = (
    "You verify scientific names against a single figure for a paleontology "
    "chart-extraction tool. You are shown ONE figure and a list of taxon / "
    "biozone / stage names that a vision model claimed it read from that "
    "figure.\n"
    "\n"
    "Report ONLY the names that are ABSENT from the figure. This is the only "
    "thing you are asked for.\n"
    "\n"
    "Rules:\n"
    "1. Report a name ONLY when you are certain it appears NOWHERE in the "
    "figure. Silence is a valid and often correct answer.\n"
    "2. Do NOT report a name that appears in ANY form: abbreviated "
    "(\"Calocycletta sp.\"), truncated at a line break, spelled with an "
    "orthographic variant or a typo, or printed in a legend, an axis label, a "
    "header, a caption inside the figure, or a footnote. Do NOT report a name "
    "merely because you cannot read it clearly or it is small.\n"
    "3. A name printed as a STRATIGRAPHIC STAGE OR AGE is PRINTED. In a "
    "zonation chart the stage names sit in their own rotated axis header "
    "column, in a stage/age header row, or in a column header cell - they are "
    "visually among the LARGEST, boldest text in the figure. \"It is not a "
    "taxon row\" and \"it is not next to a range bar\" are NOT evidence of "
    "absence; a stage name is not in the figure only if no stage name like it "
    "appears at all.\n"
    "4. A candidate that is a printed name plus a RANK SUFFIX is PRESENT. Read "
    "the printed name and match it against the candidate's stem: if the "
    "figure prints \"Kungurian\" and the candidate is \"Kungurian Stage\", or "
    "the figure prints \"Albian\" and the candidate is \"Albian Stage\", the "
    "candidate is PRESENT. The same applies to Zone, Subzone, Group, "
    "Substage, Superstage, Age, Formation and Member.\n"
    "5. A name whose characters are DISTRIBUTED over two lines, two table "
    "cells or two columns is PRESENT. \"P. scalprata m.\" above "
    "\"rhombothoracata Zone\" is one zone name, not two; \"GTS\" above "
    "\"(Subcommission on\" above \"Permian stratigraphy, 2016)\" is one "
    "header. A hyphen at the end of a line is a LAYOUT HYPHEN from wrapping, "
    "so \"P. lomentaria-\" over \"P. sakmarensis\" matches the candidate "
    "\"P. lomentaria-P. sakmarensis Assemblage-Zone\". Absorb whitespace and "
    "line breaks before you compare.\n"
    "6. A numeral that falls inside a MERGED cell counts as labelled by that "
    "cell. If a band prints \"15  16\" above the single word \"Spica\", then "
    "both \"UAZ 15 - Spica\" and \"UAZ 16 - Spica\" are PRESENT.\n"
    "7. NEVER report a name because it is unfamiliar to you. Many valid names "
    "look odd, archaic or misspelled. Plausibility is NOT evidence of "
    "absence, and a real fossil name you have never seen is still a real "
    "fossil name.\n"
    "8. NEVER add, expand, correct, split or reformat names. Echo each "
    "reported name character-for-character from the candidate list you were "
    "given.\n"
    "9. If every candidate name appears in the figure, answer with an empty "
    "list.\n"
    "\n"
    "Reply with a single JSON object and nothing else - no prose, no markdown "
    "fence, no explanation:\n"
    '{"absent": ["<name exactly as given>", "..."]}\n'
)


@dataclass(frozen=True)
class GroundingResult:
    """Outcome of one name-grounding pass.

    ``status`` is the load-bearing field. ``unverified`` being empty means
    different things under different statuses, and callers must not collapse
    them: under :data:`GROUNDING_VERIFIED` it means "checked, nothing absent",
    under :data:`GROUNDING_NOT_PERFORMED` it means "not known at all".
    """

    status: str
    checked: int = 0
    unverified: tuple[str, ...] = ()
    reason: str = ""
    #: Names the collector could not fit into the prompt because the result was
    #: pathologically large. Non-zero means the check covered a subset.
    candidates_truncated: int = 0

    @property
    def performed(self) -> bool:
        """True when the check actually ran. Not "clean" - see ``clean``."""
        return self.status == GROUNDING_VERIFIED

    @property
    def clean(self) -> bool:
        """True ONLY for a completed check that found nothing absent."""
        return self.status == GROUNDING_VERIFIED and not self.unverified

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked": self.checked,
            "unverified": list(self.unverified),
            "reason": self.reason,
            "candidates_truncated": self.candidates_truncated,
        }


def _not_performed(reason: str, checked: int = 0,
                   truncated: int = 0) -> GroundingResult:
    return GroundingResult(status=GROUNDING_NOT_PERFORMED, checked=checked,
                           reason=reason, candidates_truncated=truncated)


def _clean_text(value: Any) -> str:
    """Collapse whitespace on a candidate; never raises on odd payloads."""
    try:
        text = str(value)
    except Exception:
        return ""
    return " ".join(text.split())


def _match_key(value: Any) -> str:
    """Comparison key for a name: case-folded, whitespace-collapsed.

    Trailing sentence punctuation is dropped because the candidate list and
    the verifier's echo reach this function through different paths (the
    normaliser's ``_stringify_scalar`` vs the model's own text), and a
    trailing period on either side must not hide a real match.
    """
    return _clean_text(value).strip().rstrip(".;,:").casefold()


def collect_candidate_names(data: Any) -> tuple[list[str], int]:
    """Every printed taxon / zone name in ``data``, deduplicated in order.

    Driven entirely by :data:`NAME_SOURCES`, so a new mode is covered by adding
    one entry there. Returns ``(names, dropped)`` where ``dropped`` counts
    names beyond :data:`GROUNDING_MAX_CANDIDATES`.

    Never raises: a payload that is not a dict, a table that is not a list, a
    row that is not a dict, and a name field holding ``None`` are all skipped
    rather than propagated - this runs on whatever the model produced. The one
    exception is a table listed in :data:`BARE_STRING_ROW_TABLES`, whose rows
    ARE the name (``other_fossils``); elsewhere a non-dict row is junk and is
    skipped.
    """
    names: list[str] = []
    seen: set[str] = set()
    if not isinstance(data, dict):
        return names, 0
    for table_key, row_keys in NAME_SOURCES:
        rows = data.get(table_key)
        if not isinstance(rows, list):
            continue
        bare = table_key in BARE_STRING_ROW_TABLES
        for row in rows:
            if isinstance(row, dict):
                values: list[Any] = [row.get(k) for k in row_keys
                                     if k != ROW_IS_NAME]
            elif bare and isinstance(row, str):
                # ``other_fossils`` after normalisation: the row is the name.
                values = [row]
            else:
                continue
            for raw in values:
                if raw is None:
                    continue
                name = _clean_text(raw)
                if not name:
                    continue
                key = _match_key(name)
                if key and key not in seen:
                    seen.add(key)
                    names.append(name)
    dropped = max(0, len(names) - GROUNDING_MAX_CANDIDATES)
    if dropped:
        names = names[:GROUNDING_MAX_CANDIDATES]
    return names, dropped


def build_verifier_user_text(candidates: Sequence[str]) -> str:
    """The inline candidate list the verifier is asked to check.

    The list MIXES kinds of printed name on purpose - taxon/zone names,
    zonation-framework column headers, and chronostratigraphic stage names all
    came out of one result, so all of them are things the figure is expected
    to print. Saying so is load-bearing: measured on Zhang Lei et al. 2018
    Fig. 3, the verifier reported four stage names absent while all four were
    printed in the rotated stage header column, and the failure mode was
    reading \"not a taxon row\" as \"not in the figure\". A list framed as
    \"taxon / biozone names\" invites exactly that; a list framed as \"names
    printed in this figure, of every kind\" does not.
    """
    listing = "\n".join("%d. %s" % (i, n)
                        for i, n in enumerate(candidates, start=1))
    return (
        "The figure is attached above. These %d names were extracted from it "
        "by a vision model. They are not all taxon names: the list mixes "
        "taxon and zone names with the figure's column headers (author / "
        "framework names) and with chronostratigraphic stage names. Every one "
        "of them is expected to be printed SOMEWHERE in the figure - in a row, "
        "a cell, a header, or a rotated axis label.\n\n%s\n\n"
        "Which of them appear NOWHERE in the figure? Check every name against "
        "the whole figure, headers included, before deciding. Reply with the "
        'JSON object only, e.g. {"absent": []} if they all appear.'
        % (len(candidates), listing)
    )


def _parse_absent_reply(text: str, candidates: Sequence[str]) -> list[str]:
    """Names the verifier reported as absent, matched back onto candidates.

    Returns ``None`` when the reply is unusable, which the caller turns into
    GROUNDING_NOT_PERFORMED. Anything the verifier says that is not one of the
    candidates is DROPPED rather than reported: it is not evidence about any
    row of this result, and surfacing it would put a string the user never
    asked about into their warning list.
    """
    try:
        parsed = safe_json_loads(text)
    except Exception:
        return None
    if isinstance(parsed, dict):
        # The documented shape first; the bare-array wrapper is what
        # safe_json_loads produces when the model answered with [..] alone.
        absent = parsed.get("absent")
        if absent is None:
            absent = parsed.get("_array_root")
        if isinstance(absent, dict):
            absent = absent.get("absent")
    elif isinstance(parsed, list):
        absent = parsed
    else:
        return None
    if not isinstance(absent, list):
        return None

    by_key: dict[str, str] = {}
    for name in candidates:
        key = _match_key(name)
        if key and key not in by_key:
            by_key[key] = name

    out: list[str] = []
    for item in absent:
        key = _match_key(item)
        if key and key in by_key and by_key[key] not in out:
            out.append(by_key[key])
    return out


def check_name_grounding(
    *,
    data: Any,
    image_b64: str = "",
    media_type: str = "image/png",
    provider: Any = None,
    llm_caller: Callable[..., Any] | None = None,
    timeout_sec: int = GROUNDING_TIMEOUT_SEC,
    max_tokens: int = GROUNDING_MAX_TOKENS,
) -> GroundingResult:
    """Ask the provider which of ``data``'s names are not in the figure.

    Args:
        data: a normalized result dict (same shape ``score_range_chart`` takes).
        image_b64: the SAME figure the result was extracted from. Required in
            spirit; an empty value means the check cannot run.
        media_type: image mime type, forwarded verbatim to the transport.
        provider: the ``LlmProvider`` used for the extraction. ``None`` means
            the check cannot run.
        llm_caller: injection seam for tests - any callable taking the
            ``call_llm_api`` keyword arguments and returning its 5-tuple.
            Defaults to ``rca_core.llm.call_llm_api_with_retry``. Tests MUST
            pass a stub; this path reaches the network.
        timeout_sec: per-call timeout for the verification call.
        max_tokens: reply budget. Kept small on purpose - see the module
            docstring on why the verifier is pushed toward brevity.

    Returns:
        A :class:`GroundingResult`. NEVER raises, and never blocks the
        extraction it is checking: every failure - no provider, no image, a
        timeout, a non-200, a truncated reply, unparsable text, or any
        exception raised by the caller - becomes
        :data:`GROUNDING_NOT_PERFORMED`.
    """
    caller = llm_caller or call_llm_api_with_retry
    try:
        names, dropped = collect_candidate_names(data)
        if not names:
            return GroundingResult(
                status=GROUNDING_NO_NAMES,
                reason="this result carries no printed taxon / zone names to "
                       "verify (see NAME_SOURCES)",
            )
        if provider is None:
            return _not_performed("no provider configured", checked=len(names),
                                  truncated=dropped)
        if not image_b64:
            return _not_performed("no image supplied to check against",
                                  checked=len(names), truncated=dropped)

        raw_text, truncated, status, err_body, _usage = caller(
            provider=provider,
            system_prompt=GROUNDING_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=build_verifier_user_text(names),
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
        )
    except Exception as exc:  # a broken provider must not break extraction
        return _not_performed("verifier call raised %s: %s"
                              % (type(exc).__name__, exc))

    if raw_text is None:
        detail = _short(err_body)
        return _not_performed(
            "verifier returned no text (HTTP %s%s)"
            % (status if status is not None else "network failure",
               ": " + detail if detail else ""),
            checked=len(names), truncated=dropped)
    if status is not None and not (200 <= int(status) < 300):
        return _not_performed("verifier HTTP %s" % status, checked=len(names),
                              truncated=dropped)
    if truncated:
        # A reply cut off mid-JSON cannot be read as "that was the whole list",
        # and reporting its partial contents as the full set of absences would
        # understate the evidence. Unknown, not clean.
        return _not_performed("verifier reply was truncated at max_tokens",
                              checked=len(names), truncated=dropped)

    absent = _parse_absent_reply(raw_text, names)
    if absent is None:
        return _not_performed("verifier reply was not readable JSON",
                              checked=len(names), truncated=dropped)
    return GroundingResult(status=GROUNDING_VERIFIED, checked=len(names),
                           unverified=tuple(absent),
                           candidates_truncated=dropped)


def _short(text: Any, limit: int = 160) -> str:
    """A one-line, length-capped excerpt of an upstream error body."""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def flag_unverified_rows(data: Any, unverified: Iterable[str]) -> dict[str, int]:
    """Tag every row whose name the verifier did not find in the figure.

    Uses ``extractor._add_row_flag`` rather than assigning ``_warning``
    directly: that field is a bare string when one flag fires and a list when
    several do, and a plain assignment would silently destroy whatever the
    normaliser or the order-repair pass already recorded on the row.

    Returns ``{table_key: rows_flagged}`` for the tables that were touched.
    Mutates ``data`` in place, which is the caller's own result dict.

    A table whose rows are plain strings (``other_fossils``) contributes
    candidates that get checked and reported but cannot be flagged per row -
    there is no dict to attach ``_warning`` to.
    """
    wanted: set[str] = set()
    for name in (unverified or ()):
        key = _match_key(name)
        if key:
            wanted.add(key)
    if not wanted or not isinstance(data, dict):
        return {}

    # Imported lazily: quality.py imports this module for the result shape,
    # and extractor.py pulls in PIL / llm / prompt. Keeping the edge lazy
    # means nothing that only reads a GroundingResult pays for it.
    from .extractor import _add_row_flag

    flagged: dict[str, int] = {}
    for table_key, row_keys in NAME_SOURCES:
        rows = data.get(table_key)
        if not isinstance(rows, list):
            continue
        dict_keys = tuple(k for k in row_keys if k != ROW_IS_NAME)
        hits = 0
        for row in rows:
            # A plain-string row (other_fossils) is verified as a candidate but
            # CANNOT carry a flag: there is no dict to write ``_warning`` onto
            # and string rows are immutable. The name still reaches the user
            # through the ``grounding.unverified`` list and the warning that
            # lists it; only the per-row marker is unavailable there.
            if not isinstance(row, dict):
                continue
            for row_key in dict_keys:
                if _match_key(row.get(row_key)) in wanted:
                    _add_row_flag(row, GROUNDING_ROW_FLAG)
                    hits += 1
                    break
        if hits:
            flagged[table_key] = flagged.get(table_key, 0) + hits
    return flagged
