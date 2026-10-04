"""Which team a session belongs to, derived from its cwd when nothing told it.

The hooks are loaded globally on the shared machine while `ATTNROUTE_TEAM` is set per
worktree, so a session started outside a worktree recorded no team -- and therefore fetched
no board row, silently, which looks exactly like a team that has not written one.

The tests that matter here are about a wrong answer rather than a missing one. Telemetry
attributed to the wrong team is worse than telemetry attributed to none, because nobody can
tell it is wrong.
"""

import json

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
        p = mapping({"C:/x": "T1"})
        found = tm.team_for("C:/x/y", env={tm.TEAM_ENV: "T7"}, map_file=p)
        assert found == {"team": "T7", "source": "env", "why": ""}

    def test_a_blank_variable_does_not_win(self, mapping):
        p = mapping({"C:/x": "T1"})
        assert tm.team_for("C:/x/y", env={tm.TEAM_ENV: "  "}, map_file=p)["team"] == "T1"


class TestMatchingIsOnComponentsNotCharacters:
    """⚠ A plain `startswith` files T50's work under T5."""

    def test_a_longer_sibling_is_not_a_match(self, mapping):
        p = mapping({"C:/repo/meridian-t5": "T5", "C:/repo/meridian-t50": "T50"})
        assert tm.team_for("C:/repo/meridian-t5/nav", env={}, map_file=p)["team"] == "T5"
        assert tm.team_for("C:/repo/meridian-t50", env={}, map_file=p)["team"] == "T50"

    def test_a_partial_component_is_not_a_match(self, mapping):
        p = mapping({"C:/repo/meridian-t5": "T5"})
        found = tm.team_for("C:/repo/meridian-t5-old", env={}, map_file=p)
        assert found["team"] is None, "matched across a directory boundary"

    def test_the_longest_prefix_wins(self, mapping):
        p = mapping({"C:/Users/Jesse": "shared", "C:/Users/Jesse/meridian-t5": "T5"})
        assert tm.team_for("C:/Users/Jesse/meridian-t5/nav", env={}, map_file=p)["team"] == "T5"
        assert tm.team_for("C:/Users/Jesse/other", env={}, map_file=p)["team"] == "shared"

    def test_case_and_separators_do_not_matter(self, mapping):
        p = mapping({"C:/Users/Jesse/meridian-t5": "T5"})
        windows_style = BS.join(["C:", "Users", "Jesse", "MERIDIAN-T5", "nav"])
        assert tm.team_for(windows_style, env={}, map_file=p)["team"] == "T5"

    def test_a_trailing_slash_in_the_mapping_does_not_matter(self, mapping):
        p = mapping({"C:/Users/Jesse/meridian-t5/": "T5"})
        assert tm.team_for("C:/Users/Jesse/meridian-t5", env={}, map_file=p)["team"] == "T5"

    def test_the_source_says_where_the_answer_came_from(self, mapping):
        p = mapping({"C:/x": "T1"})
        assert tm.team_for("C:/x/y", env={}, map_file=p)["source"] == "map"


class TestACollidingMappingIsRefusedNotGuessed:
    """⚠ THE BUG MY FIRST VERSION HAD. Comparison is case- and separator-insensitive, so two
    differently spelled keys can mean one directory. A plain dict kept the last one and filed
    one team's sessions under another with nothing to show it."""

    def test_two_spellings_naming_two_teams_is_reported(self, mapping):
        p = mapping({"C:/a": "T1", "C:" + BS + "A": "T2"})
        found = tm.team_for("C:/a/b", env={}, map_file=p)
        assert found["team"] is None, "a colliding mapping must not resolve to either team"
        assert "ambiguous" in found["why"]
        assert "T1" in found["why"] and "T2" in found["why"]

    def test_two_spellings_naming_the_SAME_team_is_fine(self, mapping):
        """Positive control: the check must not refuse a merely redundant mapping."""
        p = mapping({"C:/a": "T1", "C:/A/": "T1"})
        assert tm.team_for("C:/a/b", env={}, map_file=p)["team"] == "T1"

    def test_a_collision_elsewhere_still_refuses_the_whole_file(self, mapping):
        """Deliberate: a file with a contradiction in it is not trustworthy in its other
        rows either, and a half-used mapping is harder to reason about than a refused one."""
        p = mapping({"C:/a": "T1", "C:/A": "T2", "C:/b": "T3"})
        assert tm.team_for("C:/b", env={}, map_file=p)["team"] is None


class TestAMissingOrBrokenMappingSaysSo:

    def test_an_absent_file_names_the_variable_to_set(self, tmp_path):
        found = tm.team_for("C:/x", env={}, map_file=tmp_path / "nope.json")
        assert found["team"] is None
        assert tm.TEAM_ENV in found["why"]

    def test_unparseable_json_is_reported_with_the_path(self, tmp_path):
        p = tmp_path / "attnroute-teams.json"
        p.write_text("{not json", encoding="utf-8")
        found = tm.team_for("C:/x", env={}, map_file=p)
        assert "could not be read" in found["why"]
        assert "attnroute-teams.json" in found["why"]

    def test_a_json_list_is_reported_rather_than_ignored(self, mapping):
        p = mapping(["T1", "T2"])
        assert "not an object" in tm.team_for("C:/x", env={}, map_file=p)["why"]

    def test_rows_that_are_not_strings_are_skipped_without_failing(self, mapping):
        p = mapping({"C:/x": "T1", "C:/y": 5, "C:/z": "", "7": "T2"})
        assert tm.team_for("C:/x/a", env={}, map_file=p)["team"] == "T1"

    def test_an_unmatched_path_says_what_it_tried(self, mapping):
        p = mapping({"C:/x": "T1"})
        found = tm.team_for("D:/elsewhere", env={}, map_file=p)
        assert found["team"] is None
        assert "no attnroute-teams.json prefix matches" in found["why"]

    def test_current_never_raises(self, monkeypatch):
        monkeypatch.setattr(tm, "team_for",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        tm._CACHE.clear()
        found = tm.current("C:/x")
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
