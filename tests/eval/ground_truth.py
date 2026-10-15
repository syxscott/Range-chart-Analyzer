"""Name-level ground truth for figures that were read off the page by eye.

WHY THIS FILE IS FIDDLE
=======================
Two rounds of testing were compared against a "ground truth" typed by hand, and
that truth was wrong three times -- every error landing on a name the model had
actually got RIGHT, so each one inflated the error count and inverted the
conclusion. A second failure sat on the other side: the first two comparison
scripts both reported clean results for broken reasons (a substring match with
no length floor; then no handling of OCR splitting a printed name across
lines). Both errors flattered the tool.

So every entry here carries its own provenance and confidence:

  evidence  "zoom4"     read from a crop enlarged to >= 4x
            "page"      read from the full-size figure render
            "ocr"       read from the OCR pass only, NOT independently checked
  verified  True only for "zoom4". An "ocr" entry is a lead, not a fact.

`scorer.py` refuses to let an unverified entry count toward a confident
verdict, and reports the two populations separately for exactly that reason.

SOURCE OF THE FIGURES
=====================
  r10_boxu       Bo Xu et al. 2019, Early Jurassic radiolarians from the Luoqu
                 Section, Fig. 8  (p10)
  r10_ozsvart    Ozsvart et al. 2020, New Albian radiolarians from the Neh
                 accretionary complex, Fig. 4  (p5)
  r10_bjorklund  Bjorklund et al. 2019, Younger Dryas-Holocene radiolarian
                 assemblages, Fig. 4  (p6)

These three are the ones where every printed name could be read off cleanly.
The other seven round-2 figures are NOT in this file, on purpose: they are
compound, rotated-label or raster-scanned figures where a complete hand
transcription would have been guesswork dressed as truth, which is the exact
failure this file exists to stop. Absence of a figure here means "not
transcribed", not "no defects".
"""

#: figure tag -> {"names": [...], "legend": [...], "extra": {...}}
GROUND_TRUTH = {
    "r10_boxu": {
        # Fig. 8 prints a GENUS column and a stage grid numbered 1-25.
        # All 34 genus names were legible at full render; the model returned
        # all 34, in order, character for character.
        "names": [
            "Archaeocenosphaera", "Ares", "Broctus", "Canoptum", "Canutus",
            "Charlottea", "Citriduma", "Crucella", "Emiluvia", "Eucyrtidiellum",
            "Farcus", "Gorgansium", "Hagiastrum", "Helvetocapsa", "Katroma",
            "Lantus", "Minocapsa", "Napora", "Neoconocaryomma",
            "Orbiculiformella", "Pantanellium", "Parahsuum", "Paronaella",
            "Praeconocaryomma", "Pseudocrucella", "Pseudoristola", "Religa",
            "Saitoum", "Trexus", "Triactoma", "Trillus", "Xiphostylus",
            "Zartus", "Zhamoidellum",
        ],
        "name_key": "species",
        "table": "species_ranges",
        "expect_mode": "range_chart",
    },

    "r10_ozsvart": {
        # Fig. 4: 26 taxon rows on the left, an age scale on the right.
        # Read at full render; the two entries marked in `zoom4` below were
        # re-checked on an enlarged crop.
        "names": [
            "Archaeocenosphaera clathrata", "Acaeniotyle diaphorogona",
            "Acaeniotyle umbilicata", "Crucella euganea",
            "Archaeospongoprunum cortinaense", "Dactyliodiscus cayeuxi",
            "Acanthocircus levis", "Acanthocircus multidentatus",
            "Napora crassispina", "Archaeodictyomitra chalilovi",
            "Archaeodictyomitra vulgaris", "Thanarla brouweri",
            "Dictyomitra gracilis", "Dictyomitra montisserei",
            "Dictyomitra obesa", "Obeliscoites perspicuus",
            "Pseudodictyomitra lodogaensis", "Xitus mclaughlini",
            "Xitus spinosus",
            # VERIFIED at 4.5x on an enlarged crop: the figure prints
            # "Xitus subitus". The model returned "Xitus subtilis", which is a
            # real fossil name and is NOT what is printed. One of the two
            # confirmed fabrications of round 2, and the grounding check
            # independently flagged the same string.
            "Xitus subitus",
            "Parvimitrella communis", "Parvimitrella magna",
            "Dorypyle communis", "Squuinabollum fossile",
            "Crococapsa asseni", "Hiscocapsa grutterinki",
        ],
        "name_key": "species",
        "table": "species_ranges",
        "expect_mode": "range_chart",
        # The fourth time this transcription was wrong, and the most expensive
        # one so far: it turned a CORRECT model answer into a counted error.
        # Re-read at 5x, the figure prints "Dactyliodiscus cayeuxi" -- which is
        # what the model returned -- and the draft truth said "cayexui".
        # Recorded because the next person to doubt the model on this row
        # should be told the doubt was already checked.
        "truth_corrections": [
            ("Dactyliodiscus cayexui", "Dactyliodiscus cayeuxi", "zoom5"),
        ],
        # Printed as a number cell above a name cell, never as one string.
        # Verified at 4x. The model synthesised "UAZ 1 - Asseni" etc., which is
        # an assembled label the figure never prints.
        "assembled_labels_forbidden": True,
        "zone_numbers": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
                         16, 17, 18, 19],
        "zone_words": ["Asseni", "Verbeeki", "Costata", "Romans", "Missilis",
                       "Anisa", "Spica", "Biacta"],
    },

    "r10_bjorklund": {
        # Fig. 4: 20 taxa printed as rotated column headers above a core-depth
        # range chart. Read from a 90-degree rotation of the header band,
        # enlarged 4x. Note the three the model got wrong, listed in
        # `known_transcription_errors`.
        "names": [
            "Lithomelissa setosa", "Phorticium clevei",
            "Stylodictya validispinal tenuispina", "Lithelius (Larcospira) minor",
            "Actinomma medianum", "Hexacontium pachydermum",
            "Hexacontium gigantheum", "Lithomelissa hystrix",
            "Spongotrochus glacialis", "Stichocorys seriata",
            "Peridium longispinum", "Plectacantha oikiskos",
            "Pseudodictyophimus gracilipes clevei", "Plagiacantha arachnoides",
            "Amphimelissa setosa", "Actinomma leptodermum leptodermum",
            "Cycladophora davisiana", "Sethoconus tabulatus",
            "Cleveiplegma boreale", "Phormacantha hystrix",
        ],
        "name_key": "taxon",
        "table": "abundances",
        "expect_mode": "abundance_diagram",
        "verified": "zoom4",
    },
}

#: (figure, what the model returned, what is printed, how it was confirmed)
#:
#: These are the round-2 transcription errors, kept as named cases rather than
#: folded into a rate, because the interesting property is that every one of
#: them is a REAL fossil name. A rate would hide that; a list does not.
KNOWN_TRANSCRIPTION_ERRORS = [
    ("r10_ozsvart", "Xitus subtilis", "Xitus subitus", "zoom4.5"),
    ("r10_bjorklund", "Stylocydia valispinali tenuispina",
     "Stylodictya validispinal tenuispina", "zoom4"),
    ("r10_bjorklund", "Cleviplegma (Rhizoplegma) boreale",
     "Cleveiplegma boreale", "zoom4"),
    ("r10_bjorklund", "Actinomma leptoderma longispinum",
     "Actinomma leptodermum leptodermum", "zoom4"),
]

#: Labels the model assembled from two separate printed cells.
KNOWN_ASSEMBLED_LABELS = [
    ("r10_ozsvart", "UAZ 1 - Asseni", "number cell `1` over name cell `Asseni`"),
    ("r10_ozsvart", "UAZ 2 - Verbeeki (subzones 3, 4, 5)",
     "number cells `2 3 4 5` over name cell `Verbeeki`"),
    ("r10_souza", "Geomagnetic Polarity Time Scale (GPTS)",
     "column header reads `Paleomagnetic scale` with rows "
     "`Polarity` / `Chrons` / `Age`"),
]
