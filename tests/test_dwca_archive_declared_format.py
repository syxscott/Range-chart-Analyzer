"""AUDIT-2026-10-01 [item 29]: the DwC-A archive shipped quotes its meta.xml
says it does not use, so a strict reader mis-split the record.

``meta.xml`` declares::

    <core encoding="UTF-8" fieldsTerminatedBy="\t" linesTerminatedBy="\n"
          fieldsEnclosedBy="" ignoreHeaderLines="1" ...>

``fieldsEnclosedBy=""`` means the format has NO enclosure character: a consumer
must not strip quotes, and may only split on the declared TAB.  The writer
used to be ``csv.writer(output, delimiter="\t", lineterminator="\n")`` with
the default ``quoting=QUOTE_MINIMAL`` and ``quotechar='"'``, so as soon as a
value contained the delimiter, a line break, or a quote it wrapped that field
in real quote characters -- which the declaration disclaims.

Reachable from model output, measured end to end through the real chain
(``extractor._normalize_species_into`` -> ``to_darwin_core_archive``):
``_stringify_scalar`` returns ``str(value)`` for a string with no strip and no
whitespace collapsing, so a name the model wrapped across two lines survives
normalisation verbatim.  The shipped occurrence.txt then looks like this::

    "Inoceramus
    cf._elongatus_SecA_0"  "Inoceramus
    cf. elongatus"  Smith 1900  ...

A strict reader sees 1-, 2-, 17- and 18-field rows under an 18-field
declaration.  The tab variant is the quieter one: 20 fields where 18 are
declared, so every column from the second onward is shifted into the wrong
term.  Either way the result is ingested into a public repository already
mis-parsed, and nothing in the export says so.

The existing suite (tests/test_dwc_archive_tab.py) cannot catch this: it
hardcodes ``delimiter="\\t"`` instead of reading what the XML declares, and
only uses benign values.

Fix: write with ``quoting=csv.QUOTE_NONE`` after neutralising exactly the
characters that would make the declared format impossible -- TAB, CR and LF
become a single space.  A literal ``"`` is left alone: it is not the
delimiter, and the probe confirms a quoted value still parses to the declared
field count.  That keeps the interchange contract (plain TSV, no enclosure)
and makes the declaration true by construction instead of by coincidence.
"""
from __future__ import annotations

import io
import re
import zipfile

import pytest

from rca_core.extractor import _normalize_species_into
from rca_core.standards.darwin_core import to_darwin_core_archive

#: Values a chart digitiser can genuinely produce.  The model wraps a name when
#: the chart text wraps; it does not sanitise afterwards.
ADVERSARIAL = [
    ("newline_in_species", "species", "Inoceramus\ncf. elongatus"),
    ("tab_in_species", "species", "Inoceramus\tcf. elongatus"),
    ("newline_in_biozone", "biozone", "Zone\n1"),
    ("tab_in_biozone", "biozone", "Zone\t1"),
    ("crlf_in_species", "species", "Inoceramus\r\ncf. elongatus"),
    ("plain_quote", "species", 'Inoceramus "cf." elongatus'),
]
BENIGN = "Inoceramus cf. elongatus"


def _archive(tmp_path, species, biozone="Zone 1", idx=0):
    """Build a real archive through the real normaliser + archive writer."""
    payload = {
        "species": species,
        "section": "SecA",
        "biozone": biozone,
        "author_year": "Smith 1900",
        "range_base": "Wuchiapingian",
        "range_top": "Changhsingian",
    }
    rows: list[dict] = []
    _normalize_species_into(payload, rows)
    result = {
        "sections": [{"name": "SecA", "coordinates": "31N, 117E",
                      "formations": ["Formation X"]}],
        "species_ranges": rows,
    }
    out = tmp_path / ("dwca_%d.zip" % idx)
    to_darwin_core_archive(result, str(out))
    with zipfile.ZipFile(str(out)) as zf:
        return (zf.read("occurrence.txt").decode("utf-8"),
                zf.read("meta.xml").decode("utf-8"))


def _declared(meta):
    """Read the format the way a consumer must: out of meta.xml, not hardcoded."""
    delim = re.search(r'fieldsTerminatedBy="([^"]*)"', meta).group(1)
    encl = re.search(r'fieldsEnclosedBy="([^"]*)"', meta).group(1)
    fields = len(re.findall(r"<field index=", meta))
    return delim, encl, fields


@pytest.mark.parametrize("label,key,value", ADVERSARIAL)
def test_shipped_rows_match_the_declared_format(tmp_path, label, key, value):
    raw, meta = _archive(tmp_path, value if key == "species" else BENIGN,
                         biozone=value if key == "biozone" else "Zone 1")
    delim, encl, declared = _declared(meta)
    assert encl == "", (
        "this test asserts the plain-TSV contract; meta.xml now declares an "
        "enclosure character (%r), so the expectations need re-deriving"
        % encl)

    lines = [ln for ln in raw.split("\n") if ln != ""]
    widths = {len(ln.split(delim)) for ln in lines}
    assert widths == {declared}, (
        "%s: a strict reader sees field counts %r but meta.xml declares %d "
        "-- the record will be ingested mis-split.\n  first line: %r"
        % (label, sorted(widths), declared, lines[0][:200]))

    assert '"' not in raw, (
        "%s: the file carries a '\"' while meta.xml declares "
        "fieldsEnclosedBy=\"\" -- a strict reader must keep those as data"
        % label)
    # The structural characters must not survive INSIDE a cell.  (They do of
    # course appear BETWEEN cells -- that is the delimiter.)
    for line in lines:
        for cell in line.split(delim):
            assert not any(ch in cell for ch in ("\t", "\r", "\n")), (
                "%s: a structural character survived inside a cell: %r"
                % (label, cell))


def test_benign_values_still_round_trip_exactly(tmp_path):
    raw, meta = _archive(tmp_path, BENIGN, biozone="Zone 1")
    delim, _encl, declared = _declared(meta)
    lines = [ln for ln in raw.split("\n") if ln != ""]
    header = lines[0].split(delim)
    data = lines[1].split(delim)
    assert len(header) == declared
    assert len(data) == declared
    row = dict(zip(header, data))
    assert row["scientificName"] == BENIGN
    assert row["lowestBiostratigraphicZone"] == "Zone 1"


def test_whitespace_only_difference_is_collapsed_not_dropped(tmp_path):
    """A wrapped name keeps its words; it does not lose content."""
    raw, meta = _archive(tmp_path, "Inoceramus\ncf. elongatus")
    delim, _encl, declared = _declared(meta)
    lines = [ln for ln in raw.split("\n") if ln != ""]
    assert len(lines) == 2, (
        "a wrapped name must not split the row across physical lines; got %d "
        "lines" % len(lines))
    assert len(lines[1].split(delim)) == declared
    row = dict(zip(lines[0].split(delim), lines[1].split(delim)))
    assert row["scientificName"] == "Inoceramus cf. elongatus"
