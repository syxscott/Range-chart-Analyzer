"""Score an extraction against a transcribed ground truth.

THIS MODULE IS TESTED, BECAUSE THE COMPARISON SCRIPTS THAT CAME BEFORE IT WERE
NOT, AND BOTH OF THEM WERE WRONG IN THE SAME DIRECTION.
===========================================================================
Two rounds of figure testing were scored by ad-hoc scripts. Both reported
clean results for broken reasons:

* the first used `a in b` substring containment with no length floor and
  matched against one concatenated blob of every OCR line, so a short name
  vouched for itself inside any unrelated text -- it reported "100%
  supported" across all ten figures;
* the second fixed the substring bug but compared each model name against each
  OCR string individually, and OCR routinely splits one printed name into three
  lines ("Pseudodictyophimus" / "gracilipes" / "clevei"), so correct
  extractions scored as unsupported.

Both errors inflated the score. A checker that flatters the thing it checks is
worse than no checker, so the rules below are pinned by `test_figure_eval_
scorer.py`:

  * containment requires BOTH sides >= MIN_MATCH_CHARS and a ratio bound, and
    is evaluated on word boundaries;
  * matching is per-candidate-string, never against a concatenation;
  * OCR line splits are handled by explicit n-gram joins supplied by the
    caller, not by guessing;
  * every match carries the string that justified it, so any verdict can be
    audited;
  * "not found in the figure" is reported as UNSUPPORTED, never as
    FABRICATION. Only a human (or a second independent detector) can promote
    one to the other.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence

#: A name shorter than this is never matched by containment. Below it,
#: "A" matches "anything", which is how the first scorer reported 100%.
MIN_MATCH_CHARS = 6

#: Containment also requires the container to be no more than this much
#: longer, so a long caption cannot vouch for a short taxon name.
MAX_CONTAIN_RATIO = 1.6

#: Similarity above which two names are treated as the same string with
#: OCR / marking damage -- but ONLY inside :func:`same_taxon`. On its own this
#: number is a trap: ``Buryella pentadica`` vs ``Buryella tetradica`` measures
#: 0.8889, so a bare 0.88 cutoff silently merges two different species and
#: scores a wrong answer correct. Found by
#: ``test_fuzzy_cutoff_is_strict_enough_to_not_merge_two_species``.
FUZZY_CUTOFF = 0.88


def same_taxon(a: str, b: str) -> bool:
    """Do these two normalised names denote the same taxon?

    Requires the FIRST and LAST token to agree. A binomial is identified by its
    genus and its epithet; fuzzy similarity over the whole string is not an
    identity test, because the epithets in this domain differ by one or two
    letters far more often than they differ by much:

        Buryella tetradica  vs  Buryella pentadica   ratio 0.8889  SAME epithet? no
        Xitus subitus       vs  Xitus subtilis      ratio 0.8148  SAME epithet? no
        Phorticium clevei   vs  Cleveiplegma boreale ratio 0.3243 SAME genus?  no

    None of those three is the same taxon, and none of them is anywhere near a
    safe identity test. For a genus-only name the single token must match,
    which falls out of the same rule.
    """
    ta, tb = _significant(a), _significant(b)
    if not ta or not tb:
        return False
    return ta[0] == tb[0] and ta[-1] == tb[-1]


def forms(text: Any) -> list[str]:
    """Every acceptable normalised reading of one printed name.

    A figure legitimately prints ``Lithelius (Larcospira) minor`` -- genus, a
    synonym in brackets, then the epithet -- and copying that verbatim is
    correct. It must ALSO match a model that reported ``Lithelius minor``.

    Both readings have to be derived from the ORIGINAL text. ``normalise``
    turns brackets into spaces, after which a bracketed synonym is
    indistinguishable from a real middle word. An earlier version tried to
    recover the bracket after normalising and silently did nothing: the regex
    it ran could never match, and deleting the rule left the whole suite green
    because a second, positional guess covered the same case by accident.
    One mechanism, derived before the punctuation is destroyed.
    """
    raw = str(text or "")
    out: list[str] = []
    for f in (normalise(raw), normalise(re.sub(r"\([^)]*\)", " ", raw))):
        if f and f not in out:
            out.append(f)
    return out


def _significant(key: str) -> list[str]:
    """Tokens of an already-normalised key."""
    return key.split()


def alternates(key: str) -> list[str]:
    """Plausible alternative readings of a printed name.

    For ``lithelius larcospira minor`` -- the bracketed synonym already removed
    by :func:`_significant` -- this yields ``lithelius minor``.

    ONLY the parenthetical is stripped. An earlier version also guessed
    "drop a middle token" positionally, which duplicated the parenthetical rule
    and made both untested: deleting the regex left every test green because
    the positional guess covered the same case. One mechanism, one test.
    """
    out = [key]
    stripped = _significant(key)
    if stripped:
        joined = " ".join(stripped)
        if joined and joined != key:
            out.append(joined)
    return [k for k in out if k]


def normalise(name: Any) -> str:
    """Fold a taxon name to a comparison key.

    Case, punctuation and open-nomenclature '?' are dropped: figures print
    ``Lychnocanoma? costata`` and models write ``Lychnocanoma costata``, and
    that difference is not a defect. Author strings are NOT stripped here --
    whether an authority belongs in the species field is a separate decision
    this scorer must not silently make.
    """
    s = str(name or "").casefold()
    s = s.replace("?", "")
    s = re.sub(r"[^a-z ]+", " ", s)
    return " ".join(s.split())


def _joined(texts: Sequence[str]) -> list[str]:
    """Adjacent n-grams of a reading-ordered list, for split printed names."""
    out: list[str] = []
    for i in range(len(texts)):
        for n in (2, 3):
            if i + n <= len(texts):
                out.append(" ".join(texts[i:i + n]))
    return out


def find_evidence(want_text: Any,
                  candidates: Sequence[tuple[str, Any]]) -> str | None:
    """The candidate that supports ``want_text``, or None.

    ``candidates`` is a sequence of ``(label, text)`` pairs -- the text is RAW
    and is normalised here, because the bracketed-synonym reading has to be
    taken before punctuation is destroyed.

    Each candidate is scanned one at a time on purpose: a concatenated blob
    makes a short name match inside an unrelated line, which is the bug this
    function exists to not have.
    """
    want_forms = forms(want_text)
    if not any(len(f) >= 4 for f in want_forms):
        return None
    cand_forms = [(label, forms(text)) for label, text in candidates]
    cand_forms = [(label, fs) for label, fs in cand_forms if fs]

    # No separate "exact equality" pass. An earlier version had one, ahead of
    # the containment pass; deleting it turned the suite GREEN, which proved it
    # was redundant rather than load-bearing -- two equal strings always satisfy
    # containment (ratio 1.0, whole-word boundary), so the first pass could
    # only ever duplicate the second. Removed rather than left in as a rule that
    # looks important and is not.
    for label, fs in cand_forms:
        for want in want_forms:
            if len(want) < MIN_MATCH_CHARS:
                continue
            for kf in fs:
                if len(kf) < MIN_MATCH_CHARS:
                    continue
                shorter, longer = sorted((want, kf), key=len)
                if len(longer) > len(shorter) * MAX_CONTAIN_RATIO:
                    continue
                if re.search(r"(?:^| )%s(?: |$)" % re.escape(shorter), longer):
                    return label
    # same taxon AND similar enough -- both, never either alone
    for label, fs in cand_forms:
        for want in want_forms:
            for kf in fs:
                if same_taxon(want, kf) and \
                        SequenceMatcher(None, want, kf).ratio() >= FUZZY_CUTOFF:
                    return label
    return None


def collect_model_names(
    data: Mapping[str, Any],
    tables: Iterable[tuple[str, str]] = (
        ("species_ranges", "species"),
        ("abundances", "taxon"),
        ("zones", "name"),
        ("zonations", "name"),
        ("biozones", "name"),
        ("occurrences", "taxon"),
    ),
) -> list[tuple[str, str, str]]:
    """``(table, row_key, name)`` for every name the extraction carries."""
    out: list[tuple[str, str, str]] = []
    if not isinstance(data, Mapping):
        return out
    for table, key in tables:
        rows = data.get(table)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, Mapping) and row.get(key):
                out.append((table, key, str(row[key]).strip()))
    return out


class Verdict:
    """One name's outcome. Named fields, not a dict, so a typo fails loudly."""

    __slots__ = ("name", "kind", "evidence")

    def __init__(self, name: str, kind: str, evidence: str = ""):
        #: "exact"    identical after folding
        #: "variant"  matched with OCR / marking damage, evidence recorded
        #: "unsupported" no evidence found -- NOT the same as "fabricated"
        self.kind = kind
        self.name = name
        self.evidence = evidence

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Verdict(%r, %r, %r)" % (self.name, self.kind, self.evidence)


def compare_names(
    truth: Sequence[str],
    produced: Sequence[str],
    ocr_lines: Sequence[str] = (),
) -> tuple[list[Verdict], list[Verdict]]:
    """Split ``produced`` into (supported, unsupported) against ``truth``.

    ``ocr_lines`` is optional and, when given, is treated as a reading-ordered
    list: adjacent pairs and triples are added as candidates so a name printed
    across two lines is not scored as absent because OCR cut it. It can only
    ADD support; it never removes a name from the unsupported list, because an
    OCR hit is weaker evidence than the figure itself.
    """
    truth_cands = [(t, t) for t in truth if forms(t)]
    ocr_cands = ([(o, o) for o in ocr_lines if forms(o)]
                 + [(j, j) for j in _joined(list(ocr_lines)) if forms(j)])

    supported: list[Verdict] = []
    unsupported: list[Verdict] = []
    for name in produced:
        key = normalise(name)
        if not key:
            unsupported.append(Verdict(name, "unsupported"))
            continue
        hit = find_evidence(name, truth_cands)
        if hit is not None:
            kind = "exact" if normalise(hit) == key else "variant"
            supported.append(Verdict(name, kind, hit))
            continue
        hit = find_evidence(name, ocr_cands)
        if hit is not None:
            supported.append(Verdict(name, "variant", hit))
            continue
        unsupported.append(Verdict(name, "unsupported"))
    return supported, unsupported


def score(
    result: Mapping[str, Any],
    truth: Mapping[str, Any],
    ocr_lines: Sequence[str] = (),
) -> dict[str, Any]:
    """Score one extraction against one figure's ground truth.

    Returns the two populations SEPARATELY and never a single flattering
    number:

        precision_supported   produced names with evidence / produced names
        recall                truth names matched / truth names
        unsupported           produced names with NO evidence. A candidate for
                              fabrication, and nothing stronger.
        missed                truth names the extraction never produced.
    """
    table = truth.get("table", "species_ranges")
    key = truth.get("name_key", "species")
    truth_names = list(truth.get("names") or [])
    rows = collect_model_names(result, ((table, key),))

    # De-duplicate, keeping order.
    #
    # An abundance table has one row per SAMPLE, not per taxon: Bjorklund Fig. 4
    # produced 189 rows carrying 20 distinct names, and scoring those as 189
    # produced names made precision meaningless (a name the model got right 15
    # times and wrong once was counted 15 right / 1 wrong). Distinct names are
    # what a reader checks; row counts are reported separately.
    produced: list[str] = []
    seen: set[str] = set()
    for _, _, name in rows:
        k = normalise(name)
        if k and k not in seen:
            seen.add(k)
            produced.append(name)

    supported, unsupported = compare_names(truth_names, produced, ocr_lines)
    matched_truth, missed = [], []
    for t in truth_names:
        if any(find_evidence(t, [(v.name, v.name)]) is not None
               for v in supported):
            matched_truth.append(t)
        else:
            missed.append(t)
    n_prod = len(produced)
    return {
        "table": table,
        "n_truth": len(truth_names),
        "n_rows": len(rows),
        "n_produced": n_prod,
        "n_supported": len(supported),
        "n_unsupported": len(unsupported),
        "recall": (round(len(matched_truth) / len(truth_names), 4)
                   if truth_names else None),
        "precision_supported": (round(len(supported) / n_prod, 4)
                                if n_prod else None),
        "supported": [(v.name, v.kind, v.evidence) for v in supported],
        "unsupported": [v.name for v in unsupported],
        "missed": missed,
    }
