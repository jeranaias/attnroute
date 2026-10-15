"""The product must not advertise a number whose denominator is not the bill.

WARNING -- WHY THIS FILE EXISTS. `attnroute --help` opened with "98-99% token reduction". That
   figure is real and it is measured against THE CONCATENATED SOURCE OF THE REPOSITORY -- a
   baseline no session has ever sent and nobody has ever paid for. `benchmarks/pipeline_benchmark`
   says so in its own output: *"compares against all source files concatenated (theoretical max),
   not Claude Code's native behavior."* Meanwhile the measured effect on the actual bill was 8.2%
   per turn with a 95% interval of [-40.5%, +13.4%], which INCLUDES ZERO.

   So the headline and the measurement disagreed by two orders of magnitude, and the headline was
   the one a user read first. A correct number, correctly computed, answering a question nobody
   asked, printed where the answer to the real question belongs.

WARNING -- AND A TEST IS THE ONLY THING THAT KEEPS IT GONE. A claim deleted by hand comes back the
  next time somebody wants a headline for a README, because nothing in a diff review says "this
  percentage has the wrong denominator".

WARNING -- THIS READS THE SOURCE AND DOES NOT RUN THE CLI. The first version of this file called
  `cli.main()` to capture the parser. `main()` parses `sys.argv`, which under pytest is PYTEST's
  command line, so the test invoked an arbitrary attnroute subcommand and hung. A test of what the
  help TEXT says must not be able to execute a product command to find out.
"""
import importlib
import re
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "attnroute" / "cli.py"


def _help_text():
    """-> (description, epilog), lifted from the source of the top-level parser."""
    src = CLI.read_text(encoding="utf-8")
    at = src.index('prog="attnroute"')
    desc = re.search(r'description="([^"]*)"', src[at:at + 2000])
    start = src.index('epilog="""', at) + len('epilog="""')
    end = src.index('"""', start)
    return (desc.group(1) if desc else ""), src[start:end]


class TestTheHeadlineDoesNotOutrunTheMeasurement:

    def test_the_source_still_has_a_parser_to_read(self):
        """A positive control: if the parser is renamed or restructured, every assertion below
        would pass against an empty string and report nothing wrong."""
        desc, epilog = _help_text()
        assert desc.strip(), "captured no description -- the other checks cannot fire"
        assert "attnroute init" in epilog, "captured no command list -- wrong block"

    def test_the_description_claims_no_percentage(self):
        """THE SPECIFIC REGRESSION. Any bare percentage in the one line shown above the command
        list is a claim about the product, and the only percentage we can support there is one
        whose denominator is the bill -- an interval containing zero, i.e. not a headline."""
        desc, _ = _help_text()
        assert not re.search(r"\d+\s*(-\s*\d+\s*)?%", desc), (
            f"the one-line description advertises a percentage: {desc!r}")
        assert "token reduction" not in desc.lower(), (
            f"'token reduction' in the description reads as a saving on the bill: {desc!r}")

    def test_the_help_names_the_command_that_measures_the_bill(self):
        """A user told the effect is unestablished needs somewhere to go and check.

        WARNING -- IT MUST BE IN THE COMMAND LIST, NOT MERELY MENTIONED. The first version
        asserted the substring, which the prose below the list also satisfies: deleting the
        command's own row left the test green. A user scanning `--help` for what they can run
        reads the list, so the list is what has to carry it.
        """
        epilog = _help_text()[1]
        listed = [ln for ln in epilog.splitlines()
                  if re.match(r"\s+attnroute savings\s\s+\S", ln)]
        assert listed, "`attnroute savings` is not a row in the command list"

    def test_the_help_says_the_effect_is_not_established(self):
        """THE INTERVAL INCLUDES ZERO, AND SAYING SO IS THE POINT. An epilog that printed
        '8.2% per turn' and stopped would be WORSE than the claim it replaced: it would carry the
        authority of a measurement while hiding that the measurement cannot tell the saving apart
        from nothing."""
        epilog = _help_text()[1].upper()
        assert "NOT ESTABLISHED" in epilog
        for part in ("8.2%", "-40.5%", "+13.4%"):
            assert part in epilog, "the interval must be shown with the point estimate, not after it"

    @pytest.mark.parametrize("module,needle", [
        ("attnroute.outliner", "90%"),
        ("attnroute.repo_map", "95%"),
    ])
    def test_a_component_ratio_names_what_it_is_a_ratio_OF(self, module, needle):
        """THESE RATIOS ARE TRUE AND THEY ARE NOT SAVINGS, so they are kept and labelled rather
        than deleted. Deleting a true measurement to avoid a misreading loses the measurement;
        naming its denominator loses only the misreading."""
        doc = (importlib.import_module(module).__doc__ or "")
        assert needle in doc, "the ratio itself should still be stated"
        low = doc.lower()
        assert ("not a saving" in low) or ("nobody ever sends" in low), (
            f"{module} states {needle} without naming what it is a ratio of")
