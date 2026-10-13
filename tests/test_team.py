"""Which team a session belongs to, derived from its cwd when nothing told it.

The hooks are loaded globally on the shared machine while `ATTNROUTE_TEAM` is set per
worktree, so a session started outside a worktree recorded no team -- and therefore fetched
no board row, silently, which looks exactly like a team that has not written one.

The tests that matter here are about a wrong answer rather than a missing one. Telemetry
attributed to the wrong team is worse than telemetry attributed to none, because nobody can
tell it is wrong.
"""

import json
import os
from pathlib import Path

import pytest

from attnroute import team as tm

BS = chr(92)


@pytest.fixture
def mapping(tmp_path):
    def write(obj):
        p = tmp_path / "attnroute-teams.json"
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p
    return write


class TestTheEnvironmentStillWins:
    """A session that has been told who it is is not second-guessed by a path."""

    def test_the_variable_beats_the_mapping(self, mapping):
        p = mapping({"/work/x": "T1"})
        found = tm.team_for("/work/x/y", env={tm.TEAM_ENV: "T7"}, map_file=p)
        assert found == {"team": "T7", "source": "env", "why": ""}

    def test_a_blank_variable_does_not_win(self, mapping):
        p = mapping({"/work/x": "T1"})
        assert tm.team_for("/work/x/y", env={tm.TEAM_ENV: "  "}, map_file=p)["team"] == "T1"


#: Every matching case runs in both styles. The first version of this file used only
#: Windows paths, which are RELATIVE on POSIX -- so CI went red on Linux and macOS while
#: passing here, and the resolver bug it was hiding (relocating a relative path to the hook
#: process's own directory) stayed invisible on the machine it was written on.
STYLES = {
    "windows": "C:/Users/Jesse",
    "posix": "/home/jesse",
}


@pytest.mark.parametrize("root", list(STYLES.values()), ids=list(STYLES))
class TestMatchingIsOnComponentsNotCharacters:
    """⚠ A plain `startswith` files T50's work under T5."""

    def test_a_longer_sibling_is_not_a_match(self, mapping, root):
        p = mapping({f"{root}/meridian-t5": "T5", f"{root}/meridian-t50": "T50"})
        assert tm.team_for(f"{root}/meridian-t5/nav", env={}, map_file=p)["team"] == "T5"
        assert tm.team_for(f"{root}/meridian-t50", env={}, map_file=p)["team"] == "T50"

    def test_a_partial_component_is_not_a_match(self, mapping, root):
        p = mapping({f"{root}/meridian-t5": "T5"})
        found = tm.team_for(f"{root}/meridian-t5-old", env={}, map_file=p)
        assert found["team"] is None, "matched across a directory boundary"

    def test_the_longest_prefix_wins(self, mapping, root):
        p = mapping({root: "shared", f"{root}/meridian-t5": "T5"})
        assert tm.team_for(f"{root}/meridian-t5/nav", env={}, map_file=p)["team"] == "T5"
        assert tm.team_for(f"{root}/other", env={}, map_file=p)["team"] == "shared"

    @pytest.mark.skipif(os.name != "nt", reason="case folding and backslash separators are "
                                                "Windows semantics; on POSIX two spellings "
                                                "are two directories")
    def test_case_and_separators_do_not_matter_on_windows(self, mapping, root):
        """⚠ THIS TEST WAS ASSERTING SOMETHING UNTRUE ON THE PLATFORM IT FAILED ON.

        It ran everywhere and failed on macOS, and the reason was not the test: `_normalise`
        lower-cased every component on every platform. On POSIX `/work/Meridian-T5` and
        `/work/meridian-t5` are two different directories, so folding them is the exact harm
        this module exists to prevent. See the POSIX counterpart below.
        """
        p = mapping({f"{root}/meridian-t5": "T5"})
        back = (root + "/meridian-T5/nav").replace("/", BS)
        assert tm.team_for(back, env={}, map_file=p)["team"] == "T5"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX paths are case-sensitive; Windows "
                                                "paths are not")
    def test_case_DOES_matter_on_posix(self, mapping, root):
        """The counterpart, and the reason the one above is not simply deleted: on POSIX a
        different case is a different directory, and must not be another team's."""
        p = mapping({f"{root}/meridian-t5": "T5"})
        assert tm.team_for(f"{root}/meridian-T5/nav", env={}, map_file=p)["team"] is None
        assert tm.team_for(f"{root}/meridian-t5/nav", env={}, map_file=p)["team"] == "T5"

    def test_a_trailing_slash_in_the_mapping_does_not_matter(self, mapping, root):
        p = mapping({f"{root}/meridian-t5/": "T5"})
        assert tm.team_for(f"{root}/meridian-t5", env={}, map_file=p)["team"] == "T5"

    def test_the_source_says_where_the_answer_came_from(self, mapping, root):
        p = mapping({root: "T1"})
        assert tm.team_for(f"{root}/y", env={}, map_file=p)["source"] == "map"


class TestTheResolverDoesNotRelocateAPath:
    """⚠ THE BUG THAT TURNED CI RED, and it was production code, not the tests.

    `Path(path).resolve()` on a RELATIVE path joins it to the current directory of whatever
    process is asking. A hook process does not necessarily run in the session's directory,
    so relocating a path to the hook's own cwd answers a different question -- and the
    answer is a team attributed from the wrong directory.
    """

    def test_a_relative_path_keeps_its_own_components(self):
        assert tm._resolve("some/rel/path") == ("some", "rel", "path")

    def test_a_relative_path_does_not_acquire_the_process_directory(self):
        from pathlib import Path as _P

        here = tm._normalise(_P.cwd())
        got = tm._resolve("worktree/t5")
        assert got[:len(here)] != here, f"the process cwd leaked into {got}"

    @pytest.mark.parametrize("absolute", ["C:/Users/Jesse/x", "/home/jesse/x"])
    def test_an_absolute_path_in_either_style_keeps_its_components(self, absolute):
        got = tm._resolve(absolute)
        assert got[-2:] == tm._normalise(absolute)[-2:], got

    def test_a_path_that_is_not_a_string_is_not_fatal(self):
        assert tm._resolve(None) == ()
        assert tm._resolve(12) == ("12",)


class TestACollidingMappingIsRefusedNotGuessed:
    """⚠ TWO BUGS LIVE IN THIS CLASS'S HISTORY.

    First: two differently spelled keys can mean one directory, and a plain dict kept
    whichever came last, filing one team's sessions under another with nothing to show it.

    Then: these tests were written with CASE variants, which are one directory on Windows
    and two on POSIX. So after `normcase` went in -- correctly -- they were asserting a
    Windows fact on every platform, and went red on Linux and macOS where there was no
    collision to report.

    The platform-independent collisions are the ones that survive normalisation everywhere:
    a trailing slash and a `./` segment. The case variants stay, marked Windows-only,
    because on Windows they are a real and likely mistake in a hand-written file.
    """

    @pytest.mark.parametrize("second", ["/work/a/", "/work/./a", "/work//a"])
    def test_two_spellings_of_one_directory_naming_two_teams_is_reported(
            self, mapping, second):
        """These spellings are the same directory on every platform."""
        p = mapping({"/work/a": "T1", second: "T2"})
        found = tm.team_for("/work/a/b", env={}, map_file=p)
        assert found["team"] is None, "a colliding mapping must not resolve to either team"
        assert "ambiguous" in found["why"]
        assert "T1" in found["why"] and "T2" in found["why"]

    def test_two_spellings_of_one_directory_naming_the_SAME_team_is_fine(self, mapping):
        """Positive control: the check must not refuse a merely redundant mapping."""
        p = mapping({"/work/a": "T1", "/work/a/": "T1"})
        assert tm.team_for("/work/a/b", env={}, map_file=p)["team"] == "T1"

    def test_a_collision_elsewhere_still_refuses_the_whole_file(self, mapping):
        """Deliberate: a file with a contradiction in it is not trustworthy in its other
        rows either, and a half-used mapping is harder to reason about than a refused one."""
        p = mapping({"/work/a": "T1", "/work/./a": "T2", "/work/b": "T3"})
        assert tm.team_for("/work/b", env={}, map_file=p)["team"] is None

    @pytest.mark.skipif(os.name != "nt", reason="case variants are one directory on Windows "
                                                "and two on POSIX")
    def test_case_variants_collide_on_windows(self, mapping):
        p = mapping({"/work/a": "T1", "/work" + BS + "A": "T2"})
        found = tm.team_for("/work/a/b", env={}, map_file=p)
        assert found["team"] is None
        assert "ambiguous" in found["why"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX paths are case-sensitive")
    def test_case_variants_are_two_directories_on_posix(self, mapping):
        """The counterpart: not a collision at all, so both rows stand and each path gets
        its own team. Refusing here would be the bug."""
        p = mapping({"/work/a": "T1", "/work/A": "T2"})
        assert tm.team_for("/work/a/b", env={}, map_file=p)["team"] == "T1"
        assert tm.team_for("/work/A/b", env={}, map_file=p)["team"] == "T2"


class TestAMissingOrBrokenMappingSaysSo:

    def test_an_absent_file_names_the_variable_to_set(self, tmp_path):
        found = tm.team_for("/work/x", env={}, map_file=tmp_path / "nope.json")
        assert found["team"] is None
        assert tm.TEAM_ENV in found["why"]

    def test_unparseable_json_is_reported_with_the_path(self, tmp_path):
        p = tmp_path / "attnroute-teams.json"
        p.write_text("{not json", encoding="utf-8")
        found = tm.team_for("/work/x", env={}, map_file=p)
        assert "could not be read" in found["why"]
        assert "attnroute-teams.json" in found["why"]

    def test_a_json_list_is_reported_rather_than_ignored(self, mapping):
        p = mapping(["T1", "T2"])
        assert "not an object" in tm.team_for("/work/x", env={}, map_file=p)["why"]

    def test_rows_that_are_not_strings_are_skipped_without_failing(self, mapping):
        p = mapping({"/work/x": "T1", "/work/y": 5, "/work/z": "", "7": "T2"})
        assert tm.team_for("/work/x/a", env={}, map_file=p)["team"] == "T1"

    def test_an_unmatched_path_says_what_it_tried(self, mapping):
        p = mapping({"/work/x": "T1"})
        found = tm.team_for("/elsewhere", env={}, map_file=p)
        assert found["team"] is None
        assert "no attnroute-teams.json prefix matches" in found["why"]

    def test_current_never_raises(self, monkeypatch):
        monkeypatch.setattr(tm, "team_for",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        tm._CACHE.clear()
        found = tm.current("/work/x")
        assert found["team"] is None and "boom" in found["why"]
        tm._CACHE.clear()


class TestEveryStreamRecordCarriesTheTeam:

    def test_the_team_and_its_source_are_on_the_record(self, tmp_path, monkeypatch):
        from attnroute import telemetry_stream as ts

        monkeypatch.setenv(tm.TEAM_ENV, "T5")
        tm._CACHE.clear()
        monkeypatch.setattr(ts.Path, "home", staticmethod(lambda: tmp_path))
        ts.emit("read_ledger", "read_ledger", session_id="s", acting=False)
        rec = json.loads(ts.stream_path().read_text(encoding="utf-8").splitlines()[0])
        assert rec["team"] == "T5"
        assert rec["team_source"] == "env"
        tm._CACHE.clear()

    def test_an_unknown_team_is_null_and_not_an_error(self, tmp_path, monkeypatch):
        from attnroute import telemetry_stream as ts

        monkeypatch.delenv(tm.TEAM_ENV, raising=False)
        monkeypatch.setattr(tm, "map_path", lambda: tmp_path / "nope.json")
        tm._CACHE.clear()
        monkeypatch.setattr(ts.Path, "home", staticmethod(lambda: tmp_path))
        ts.emit("output_cap", "result", session_id="s", acting=False)
        rec = json.loads(ts.stream_path().read_text(encoding="utf-8").splitlines()[0])
        assert rec["team"] is None
        assert rec["team_source"] == "none"
        tm._CACHE.clear()

    def test_a_team_the_session_DECLARED_reaches_the_record(self, tmp_path, monkeypatch):
        """⚠⚠ THE END-TO-END PROPERTY, AND THE ONLY REASON THE BRANCH EXISTS. `emit` knows
        the session id; the resolver needs it to find a declared team. Nothing covered this, so
        dropping `session_id=` from that one call left every other test green while putting
        `team: null` back on every row from the shared machine -- which is the state this work
        set out to fix.
        """
        from attnroute import telemetry_stream as ts

        monkeypatch.delenv(tm.TEAM_ENV, raising=False)
        monkeypatch.setattr(tm, "map_path", lambda: tmp_path / "nope.json")
        monkeypatch.setattr(ts.Path, "home", staticmethod(lambda: tmp_path))
        sessions = tmp_path / ".claude" / "attn_state" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "s9.json").write_text(json.dumps({"team": "T6"}), encoding="utf-8")
        monkeypatch.setattr(tm.Path, "home", staticmethod(lambda: tmp_path))
        tm._CACHE.clear()
        ts.emit("session_state", "handback", session_id="s9", acting=False)
        rec = json.loads(ts.stream_path().read_text(encoding="utf-8").splitlines()[0])
        assert rec["team"] == "T6"
        assert rec["team_source"] == "session"
        tm._CACHE.clear()

    def test_a_caller_may_still_override_it(self, tmp_path, monkeypatch):
        """`emit(..., team=...)` keeps working: the automatic value is a floor, not a lock."""
        from attnroute import telemetry_stream as ts

        monkeypatch.setattr(ts.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv(tm.TEAM_ENV, "T5")      # the automatic answer would be T5
        tm._CACHE.clear()
        ts.emit("session_state", "handback", session_id="s", team="architect")
        rec = json.loads(ts.stream_path().read_text(encoding="utf-8").splitlines()[0])
        # ⚠ The first version of this test asserted `rec["team"] in ("architect",
        #   rec["team"])`, which is true of every value there has ever been. It passed while
        #   the override was in fact being discarded, because the automatic value was set
        #   before the fields were merged.
        assert rec["team"] == "architect", "the caller's team was discarded"
        assert rec["team_source"] == "caller"
        tm._CACHE.clear()


class TestTheHandbackUsesTheDerivedTeam:

    def test_an_unknown_team_is_recorded_with_its_reason(self, tmp_path, monkeypatch):
        from attnroute import session_state as ss

        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(ss.Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv(tm.TEAM_ENV, raising=False)
        monkeypatch.setattr(tm, "map_path", lambda: home / "nope.json")
        tm._CACHE.clear()

        state = ss.empty_state("s")
        ss.add_note(state, "a ruling", kind="ruling")
        ss.save("s", state)
        ss.hook({"hook_event_name": "SessionStart", "source": "compact",
                 "session_id": "s", "cwd": str(repo)}, repo=repo)

        why = ss.load("s").get("facts", {}).get("team_unknown")
        assert why, "an unknown team must be said out loud, not left as an absent row"
        tm._CACHE.clear()


class TestBothSidesAreResolvedTheSameWay:
    """⚠ THE macOS RED, and it was an asymmetry rather than a platform quirk.

    `load_map` normalised each prefix while `team_for` RESOLVED the session path. On Linux
    and Windows the two agree for ordinary paths, so it looked correct. On macOS `/home` is
    a firmlink, so the session path resolved elsewhere, the mapping key stayed literal, and
    every match returned None.

    Off CI the same asymmetry bites on a symlinked worktree, which is what these tests use:
    a real symlink, both spellings, in both directions.
    """

    @pytest.fixture
    def linked(self, tmp_path):
        """A real directory and a symlink pointing at it, or skip."""
        real = tmp_path / "real-worktrees" / "meridian-t5"
        real.mkdir(parents=True)
        link = tmp_path / "t5-link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc!r}")
        return real, link

    def test_a_mapping_on_the_link_matches_a_session_in_the_real_path(self, mapping, linked):
        real, link = linked
        p = mapping({str(link): "T5"})
        assert tm.team_for(str(real), env={}, map_file=p)["team"] == "T5"

    def test_a_mapping_on_the_real_path_matches_a_session_in_the_link(self, mapping, linked):
        real, link = linked
        p = mapping({str(real): "T5"})
        assert tm.team_for(str(link), env={}, map_file=p)["team"] == "T5"

    def test_a_subdirectory_under_either_spelling_matches(self, mapping, linked):
        real, link = linked
        (real / "nav").mkdir()
        p = mapping({str(link): "T5"})
        assert tm.team_for(str(real / "nav"), env={}, map_file=p)["team"] == "T5"
        assert tm.team_for(str(link / "nav"), env={}, map_file=p)["team"] == "T5"

    def test_an_unrelated_sibling_still_does_not_match(self, mapping, linked):
        """Positive control: resolving both sides must not turn everything into a match."""
        real, link = linked
        other = real.parent / "meridian-t6"
        other.mkdir()
        p = mapping({str(link): "T5"})
        assert tm.team_for(str(other), env={}, map_file=p)["team"] is None

    def test_the_prefix_and_the_path_go_through_one_function(self):
        """Blunt, and it is here because the bug was invisible on two of three platforms:
        the mapping key must be built by the same resolver the session path uses."""
        src = Path(tm.__file__).read_text(encoding="utf-8")
        assert "key = _resolve(prefix)" in src
        assert "key = _normalise(prefix)" not in src


class TestASessionCanSayWhichTeamItIs:
    """⚠⚠ MEASURED BEFORE IT WAS BUILT, on jeranaias 2026-10-13: the mapping file held 128
    prefixes, every one of them a real directory, and NOT ONE was the cwd of a live session.
    Every team session there starts in `C:/Users/Jesse` and `cd`s into its worktree per command,
    so all twenty live sessions -- eleven over 90 MB, touched that day -- resolved to team=None.
    The mapping was not missing entries. It keyed the directory a session runs in, and every
    session runs in the same one, so it looked complete while answering a question nobody asked.

    The two that matter most here:

      * a DECLARED team outranks a MAPPED one, because one is the session's statement about
        itself and the other is an inference from where it happens to be sitting;
      * and the ENVIRONMENT still outranks both, so a session that has been told who it is
        cannot be relabelled by its own state file.
    """

    def test_a_declared_team_is_used(self, tmp_path):
        state_dir = tmp_path / "sessions"
        state_dir.mkdir()
        (state_dir / "s1.json").write_text(json.dumps({"team": "T4"}), encoding="utf-8")
        out = tm.team_for(cwd="C:/nowhere/at/all", env={}, map_file=tmp_path / "none.json",
                          session_id="s1", state_dir=state_dir)
        assert out["team"] == "T4"
        assert out["source"] == "session", "the source says it was declared, not inferred"

    def test_it_outranks_the_mapping(self, tmp_path):
        """⚠ A statement beats an inference. The session is sitting inside a mapped worktree
        and still says it is T4; attributing it to T1 would file one team's work under
        another's name, which is the harm this whole module is built around."""
        state_dir = tmp_path / "sessions"
        state_dir.mkdir()
        (state_dir / "s1.json").write_text(json.dumps({"team": "T4"}), encoding="utf-8")
        map_file = tmp_path / "teams.json"
        map_file.write_text(json.dumps({str(tmp_path / "wt"): "T1"}), encoding="utf-8")
        out = tm.team_for(cwd=str(tmp_path / "wt"), env={}, map_file=map_file,
                          session_id="s1", state_dir=state_dir)
        assert out["team"] == "T4"

    def test_the_environment_outranks_a_declaration(self, tmp_path):
        state_dir = tmp_path / "sessions"
        state_dir.mkdir()
        (state_dir / "s1.json").write_text(json.dumps({"team": "T4"}), encoding="utf-8")
        out = tm.team_for(cwd=".", env={tm.TEAM_ENV: "T9"}, session_id="s1",
                          state_dir=state_dir)
        assert out["team"] == "T9"
        assert out["source"] == "env"

    def test_no_session_id_means_no_declaration(self, tmp_path):
        """⚠⚠ AND THE STRAY FILES ARE REALLY THERE, which the first version of this test
        missed. It asserted against an empty directory, so defaulting the session id to some
        literal name was inert and the mutation that did exactly that stayed green -- a check
        that could not fire. `x.json` is the specific hazard: `session_state.state_path` writes
        that name when the session id is empty, so a resolver that defaulted instead of
        refusing would read one unnamed session's label onto every other."""
        state_dir = tmp_path / "sessions"
        state_dir.mkdir()
        for name in ("x", "s1", "session"):
            (state_dir / f"{name}.json").write_text(json.dumps({"team": "T4"}),
                                                    encoding="utf-8")
        assert tm.declared_team(None, state_dir) == ""
        assert tm.declared_team("", state_dir) == ""
        assert tm.declared_team("   ", state_dir) == ""
        assert tm.declared_team("s1", state_dir) == "T4", "positive control"

    def test_an_unreadable_state_file_is_not_a_declaration(self, tmp_path):
        """⚠ Resolving a team runs on a hook path, so every failure here is "not declared"
        rather than an exception: a label must never cost the user their turn."""
        state_dir = tmp_path / "sessions"
        state_dir.mkdir()
        (state_dir / "s1.json").write_text("{not json", encoding="utf-8")
        assert tm.declared_team("s1", state_dir) == ""
        (state_dir / "s2.json").write_text(json.dumps({"team": 7}), encoding="utf-8")
        assert tm.declared_team("s2", state_dir) == ""
        (state_dir / "s3.json").write_text(json.dumps({"team": "  "}), encoding="utf-8")
        assert tm.declared_team("s3", state_dir) == ""

    def test_the_why_names_the_way_out_when_nothing_is_known(self, tmp_path):
        """⚠⚠ ON THE SHARED MACHINE THIS IS THE EXPECTED MESSAGE, NOT A MISCONFIGURATION,
        and that is why it has to name the remedy. A diagnostic that says only "no prefix
        matches" sends the reader to edit a mapping file that cannot answer the question."""
        map_file = tmp_path / "teams.json"
        map_file.write_text(json.dumps({str(tmp_path / "elsewhere"): "T1"}), encoding="utf-8")
        out = tm.team_for(cwd=str(tmp_path), env={}, map_file=map_file)
        assert out["team"] is None
        assert "team set" in out["why"]
        assert tm.TEAM_ENV in out["why"]

    def test_the_cache_does_not_serve_one_session_another_answer(self, monkeypatch):
        """The cache key used to be the cwd alone, which was right while the cwd was all that
        was consulted. One hook process asking about two sessions would then have been handed
        the first answer twice -- and on this fleet every session shares a cwd, so the key
        would have been identical for all of them."""
        monkeypatch.setattr(tm, "_CACHE", {})
        monkeypatch.delenv(tm.TEAM_ENV, raising=False)
        monkeypatch.setattr(tm, "declared_team",
                            lambda sid, state_dir=None: {"a": "T4", "b": "T6"}.get(sid, ""))
        assert tm.current(session_id="a")["team"] == "T4"
        assert tm.current(session_id="b")["team"] == "T6", "the cwd alone is not the key"
