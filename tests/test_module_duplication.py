# -*- coding: utf-8 -*-
"""Guards against drift between the duplicated wall / non-wall modules.

lib/ carries two parallel module trees. The wall tools import wf_wall_*
and the floor, ceiling and roof tools import the unprefixed wf_*, and for
several of those pairs the two files are byte-identical apart from the
prefix in their own import lines.

That duplication is a live hazard rather than a tidiness complaint: a fix
applied to one side and not the other produces two engines that disagree,
and nothing at runtime notices. It has already happened during
development -- the BOM mass columns were added to wf_schedule_utils and
had to be replayed into wf_wall_schedule_utils by hand.

These tests make that impossible to miss. They do NOT require the pairs to
be identical forever; wf_placement and wf_wall_placement have genuinely
diverged and are excluded by name, with the reason recorded. What they
require is that a pair listed as identical *stays* identical, so removing
a file from IDENTICAL_PAIRS is a deliberate decision someone makes rather
than an accident that slips through.
"""

import io
import os
import re
import unittest

_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")

# Pairs that must remain byte-identical once the wall prefix is normalised.
IDENTICAL_PAIRS = (
    ("wf_geometry.py", "wf_wall_geometry.py"),
    ("wf_host.py", "wf_wall_host.py"),
    ("wf_tracking.py", "wf_wall_tracking.py"),
    ("wf_families.py", "wf_wall_families.py"),
    ("wf_schedule_utils.py", "wf_wall_schedule_utils.py"),
)

# Pairs that have genuinely diverged, with why. Listed so the divergence
# is a recorded decision rather than an unexamined difference.
KNOWN_DIVERGENT = {
    ("wf_config.py", "wf_wall_config.py"):
        "the wall config carries wall-only defaults",
    ("wf_placement.py", "wf_wall_placement.py"):
        "the wall engine places columns and handles joins differently",
}

_PREFIX_RE = re.compile(r"\bwf_wall_")


def _normalised(filename):
    """File contents with the wall prefix stripped, so the two sides of a
    pair are directly comparable."""
    with io.open(os.path.join(_LIB_DIR, filename), encoding="utf-8") as handle:
        return _PREFIX_RE.sub("wf_", handle.read())


class IdenticalPairTests(unittest.TestCase):
    def test_declared_pairs_are_still_identical(self):
        for plain, walled in IDENTICAL_PAIRS:
            self.assertEqual(
                _normalised(plain), _normalised(walled),
                "\n{0} and {1} have drifted apart.\n"
                "These modules are meant to be identical, so a change to "
                "one must be applied to the other. Either replay the "
                "change, or -- if the divergence is intended -- move the "
                "pair into KNOWN_DIVERGENT with a reason.".format(
                    plain, walled))

    def test_both_files_of_every_declared_pair_exist(self):
        for pair in IDENTICAL_PAIRS + tuple(KNOWN_DIVERGENT):
            for filename in pair:
                self.assertTrue(
                    os.path.exists(os.path.join(_LIB_DIR, filename)),
                    "{0} is declared in a pair but does not exist".format(filename))

    def test_no_pair_is_declared_twice(self):
        overlap = set(IDENTICAL_PAIRS) & set(KNOWN_DIVERGENT)
        self.assertEqual(overlap, set(),
                         "a pair cannot be both identical and divergent")


class DivergentPairTests(unittest.TestCase):
    def test_divergent_pairs_really_do_differ(self):
        # If a pair on this list becomes identical, the exemption is stale
        # and the pair should be promoted to IDENTICAL_PAIRS so it is
        # guarded from then on.
        for (plain, walled), reason in KNOWN_DIVERGENT.items():
            self.assertNotEqual(
                _normalised(plain), _normalised(walled),
                "{0} and {1} are now identical -- move them into "
                "IDENTICAL_PAIRS so they stay that way. Recorded reason "
                "for the exemption was: {2}".format(plain, walled, reason))

    def test_every_exemption_carries_a_reason(self):
        for pair, reason in KNOWN_DIVERGENT.items():
            self.assertTrue(reason and reason.strip(),
                            "{0} is exempted without a reason".format(pair))


class RetiredEngineTests(unittest.TestCase):
    """The retired engines must stay retired.

    wf_framing.py and wf_wall_framing_v2.py are superseded and imported by
    nothing. Leaving them in place is fine; what is not fine is quietly
    depending on them again, because they read config options the live
    engines do not. That exact confusion cost real debugging time: the
    "King studs" and "Jack studs" options appeared to work because these
    modules read them, while the live v4 engine ignored them.
    """

    RETIRED = ("wf_framing", "wf_wall_framing_v2")

    def _source_files(self):
        root = os.path.dirname(_LIB_DIR)
        for dirpath, dirnames, filenames in os.walk(root):
            if ".git" in dirpath:
                continue
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)

    def test_retired_engines_are_not_imported_anywhere(self):
        for module in self.RETIRED:
            pattern = re.compile(
                r"^\s*(?:from\s+{0}\s+import|import\s+{0})\b".format(module),
                re.MULTILINE)
            for path in self._source_files():
                if os.path.basename(path) == module + ".py":
                    continue
                with io.open(path, encoding="utf-8", errors="replace") as handle:
                    if pattern.search(handle.read()):
                        self.fail(
                            "{0} imports the retired engine {1}. It reads "
                            "config options the live engines ignore, so "
                            "depending on it makes options appear to work "
                            "when they do not.".format(path, module))


if __name__ == "__main__":
    unittest.main()
