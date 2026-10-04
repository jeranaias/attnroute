"""`import attnroute` must stay cheap, because every hook event pays for it.

Measured before this was fixed: 5.67 s to import the package, 5.9-6.4 s for a hook process
end to end, on every UserPromptSubmit and every Stop. The cost was `attnroute.compressor`
pulling in chromadb, which pulls in opentelemetry -- none of which a hook touches.

These tests run in a FRESH interpreter, because `sys.modules` in this one already holds
everything the rest of the suite imported, and a check of this one's `sys.modules` would
pass no matter what the package does.
"""

import subprocess
import sys

#: Modules no hook needs, and which cost seconds to import.
HEAVY = ("attnroute.compressor", "attnroute.context_router", "attnroute.repo_map",
         "attnroute.learner", "chromadb")


def fresh(code: str):
    """Run `code` in a new interpreter. -> CompletedProcess"""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=120)


def test_importing_the_package_pulls_in_nothing_heavy():
    r = fresh(
        "import sys, attnroute\n"
        f"heavy = [m for m in {HEAVY!r} if m in sys.modules]\n"
        "print('HEAVY:' + ','.join(heavy))\n"
    )
    assert r.returncode == 0, r.stderr[-2000:]
    line = [x for x in r.stdout.splitlines() if x.startswith("HEAVY:")][0]
    assert line == "HEAVY:", f"eagerly imported {line[6:]}"


def test_the_package_imports_in_well_under_a_second():
    """A wall-clock bound, not a module count: the module list could change and the cost is
    the thing that mattered. Generous on purpose -- the measured figures are 5670 ms before
    and 5 ms after, so anything near the bound is a regression, not jitter."""
    r = fresh(
        "import time\n"
        "t = time.perf_counter()\n"
        "import attnroute\n"
        "print('MS:%d' % ((time.perf_counter() - t) * 1000))\n"
    )
    assert r.returncode == 0, r.stderr[-2000:]
    ms = int([x for x in r.stdout.splitlines() if x.startswith("MS:")][0][3:])
    assert ms < 1500, f"import attnroute took {ms} ms"


def test_the_lazy_names_still_resolve():
    """Positive control for the two tests above: if the names did NOT work, a package that
    imports nothing would pass them both while having broken every caller."""
    r = fresh(
        "import attnroute\n"
        "names = ['build_context_output', 'get_tier', 'update_attention', 'RepoMapper',\n"
        "         'ObservationCompressor', 'ProgressiveRetriever', 'Learner']\n"
        "bad = [n for n in names if getattr(attnroute, n, None) is None]\n"
        "print('BAD:' + ','.join(bad))\n"
    )
    assert r.returncode == 0, r.stderr[-2000:]
    line = [x for x in r.stdout.splitlines() if x.startswith("BAD:")][0]
    assert line == "BAD:", f"these re-exports no longer resolve: {line[4:]}"


def test_touching_a_lazy_name_is_what_imports_its_module():
    r = fresh(
        "import sys, attnroute\n"
        "before = 'attnroute.repo_map' in sys.modules\n"
        "attnroute.RepoMapper\n"
        "after = 'attnroute.repo_map' in sys.modules\n"
        "print('BEFORE:%s AFTER:%s' % (before, after))\n"
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "BEFORE:False AFTER:True" in r.stdout, r.stdout


def test_an_unknown_name_raises_AttributeError_not_ImportError():
    import attnroute
    try:
        attnroute.no_such_thing
    except AttributeError as exc:
        assert "no_such_thing" in str(exc)
    else:
        raise AssertionError("an unknown attribute must raise AttributeError")


def test_dir_still_lists_the_exports():
    import attnroute
    listed = dir(attnroute)
    for name in ("RepoMapper", "Learner", "__version__"):
        assert name in listed, name
