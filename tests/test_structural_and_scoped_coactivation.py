"""Tests for structural 1-hop edges in RepoMapper and per-repo co-activation in Learner."""

from pathlib import Path

import pytest

from attnroute import learner as learner_mod
from attnroute.learner import Learner, repo_namespace_id
from attnroute.repo_map import RepoMapper


class TestStructuralEdges:
    """RepoMapper builds 1-hop caller/callee, middleware, and multi-language import edges."""

    def test_ts_esm_relative_imports_and_middleware_edges(self, tmp_path: Path):
        (tmp_path / "src" / "lib").mkdir(parents=True)
        (tmp_path / "src" / "routes").mkdir(parents=True)
        (tmp_path / "src" / "misc").mkdir(parents=True)

        (tmp_path / "src" / "lib" / "auth.ts").write_text(
            "export function verifyBearerToken(header: string) { return true; }\n"
            "export function requireAuthMiddleware(req: any) { return verifyBearerToken(req); }\n",
            encoding="utf-8",
        )
        (tmp_path / "src" / "routes" / "roster.ts").write_text(
            "import { verifyBearerToken } from '../lib/auth.js';\n"
            "export function registerRosterRoutes(app: any) {\n"
            "  app.post('/units', { preHandler: [requireAuthMiddleware] }, () => {\n"
            "    return verifyBearerToken('x');\n"
            "  });\n"
            "}\n",
            encoding="utf-8",
        )
        (tmp_path / "src" / "misc" / "unrelated.ts").write_text(
            "export function computeMetrics() { return 42; }\n",
            encoding="utf-8",
        )

        mapper = RepoMapper(str(tmp_path))
        mapper.index()

        # Callees / imports from route -> auth (works even via ESM .js extension & ../)
        callees = mapper.get_structural_neighbors(
            "src/routes/roster.ts", include_callers=False, include_callees=True
        )
        assert "src/lib/auth.ts" in callees
        assert "src/misc/unrelated.ts" not in callees

        # Reverse callers from auth -> route (including Windows backslash input)
        callers = mapper.get_structural_neighbors(
            r"src\lib\auth.ts", include_callers=True, include_callees=False
        )
        assert "src/routes/roster.ts" in callers

        # Active file boosts its 1-hop structural neighbor above unrelated files
        scores = mapper._score_files(query=None, active_files=["src/routes/roster.ts"])
        assert scores["src/routes/roster.ts"] > scores["src/lib/auth.ts"] > scores["src/misc/unrelated.ts"]

        # Context output surfaces related structural neighbor
        ctx = mapper.get_context_for_files(["src/routes/roster.ts"], include_related=True)
        assert "src/lib/auth.ts" in ctx

    def test_python_relative_imports_and_call_edges_without_networkx(self, tmp_path: Path):
        (tmp_path / "pkg" / "core").mkdir(parents=True)
        (tmp_path / "pkg" / "api").mkdir(parents=True)

        (tmp_path / "pkg" / "core" / "policy.py").write_text(
            "class BaseGuard:\n"
            "    pass\n\n"
            "def enforce_rotation_lock(rot_id):\n"
            "    return rot_id\n",
            encoding="utf-8",
        )
        (tmp_path / "pkg" / "api" / "units.py").write_text(
            "from ..core.policy import enforce_rotation_lock\n\n"
            "class UnitGuard(BaseGuard):\n"
            "    pass\n\n"
            "def create_unit(rot_id):\n"
            "    return enforce_rotation_lock(rot_id)\n",
            encoding="utf-8",
        )

        mapper = RepoMapper(str(tmp_path))
        mapper.dependency_graph = None  # Force stdlib-only adjacency path
        mapper.index()

        assert "pkg/core/policy.py" in mapper.get_structural_neighbors(
            "pkg/api/units.py", include_callers=False, include_callees=True
        )
        assert "pkg/api/units.py" in mapper.get_structural_neighbors(
            "pkg/core/policy.py", include_callers=True, include_callees=False
        )

    def test_go_and_dart_import_and_symbol_edges(self, tmp_path: Path):
        (tmp_path / "internal" / "auth").mkdir(parents=True)
        (tmp_path / "cmd" / "server").mkdir(parents=True)
        (tmp_path / "lib" / "services").mkdir(parents=True)
        (tmp_path / "lib" / "screens").mkdir(parents=True)

        (tmp_path / "internal" / "auth" / "token.go").write_text(
            'package auth\n\nfunc VerifyEd25519Token(raw string) bool { return true }\n',
            encoding="utf-8",
        )
        (tmp_path / "cmd" / "server" / "main.go").write_text(
            'package main\n\nimport (\n\t"github.com/example/app/internal/auth"\n)\n\n'
            'func serveHttp() { auth.VerifyEd25519Token("abc") }\n',
            encoding="utf-8",
        )
        (tmp_path / "lib" / "services" / "ptt_link.dart").write_text(
            "class LiveKitPttLink {\n  void connectRoom() {}\n}\n",
            encoding="utf-8",
        )
        (tmp_path / "lib" / "screens" / "radio_screen.dart").write_text(
            "import 'package:tacsnap/services/ptt_link.dart';\n"
            "class RadioScreen {\n  final link = LiveKitPttLink();\n}\n",
            encoding="utf-8",
        )

        mapper = RepoMapper(str(tmp_path))
        mapper.index()

        assert "internal/auth/token.go" in mapper.get_structural_neighbors("cmd/server/main.go")
        assert "lib/services/ptt_link.dart" in mapper.get_structural_neighbors("lib/screens/radio_screen.dart")

    def test_two_hop_traversal_and_max_neighbors_cap(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("from b import helper_b\ndef entry_a():\n    return helper_b()\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("from c import leaf_c\ndef helper_b():\n    return leaf_c()\n", encoding="utf-8")
        (tmp_path / "c.py").write_text("def leaf_c():\n    return 1\n", encoding="utf-8")

        mapper = RepoMapper(str(tmp_path))
        mapper.index()

        hop1 = mapper.get_structural_neighbors("a.py", hops=1, include_callers=False)
        assert hop1 == ["b.py"]

        hop2 = mapper.get_structural_neighbors("a.py", hops=2, include_callers=False)
        assert hop2 == ["b.py", "c.py"]

        capped = mapper.get_structural_neighbors("a.py", hops=2, include_callers=False, max_neighbors=1)
        assert capped == ["b.py"]


class TestRepoScopedCoactivation:
    """Learner namespaces co-activation graphs per repository to prevent cross-repo bleed."""

    @pytest.fixture
    def isolated_learner_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        state_file = tmp_path / "learned_state.json"
        monkeypatch.setattr(learner_mod, "LEARNED_STATE_FILE", state_file)
        monkeypatch.setattr(learner_mod, "TELEMETRY_DIR", tmp_path)
        return state_file

    def test_repo_namespace_id_anchors_git_root_and_worktrees(self, tmp_path: Path):
        repo_a = tmp_path / "alpha"
        sub_a = repo_a / "backend" / "api"
        sub_a.mkdir(parents=True)
        (repo_a / ".git").mkdir()

        # Simulate a linked git worktree pointing back to repo_a/.git/worktrees/wt1
        wt_gitdir = repo_a / ".git" / "worktrees" / "wt1"
        wt_gitdir.mkdir(parents=True)
        wt_dir = tmp_path / "alpha-wt1"
        wt_dir.mkdir()
        (wt_dir / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")

        repo_b = tmp_path / "beta"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        id_root = repo_namespace_id(repo_a)
        id_sub = repo_namespace_id(sub_a)
        id_wt = repo_namespace_id(wt_dir)
        id_other = repo_namespace_id(repo_b)

        assert id_root == id_sub == id_wt
        assert id_root != id_other
        assert id_root.startswith("alpha-")
        assert id_other.startswith("beta-")

    def test_record_coactivation_isolates_repositories(self, isolated_learner_state):
        learner = Learner()
        rid_a = repo_namespace_id("c:/projects/repo-alpha")
        rid_b = repo_namespace_id("c:/projects/repo-beta")

        assert learner.record_coactivation(["auth.ts", "roster.ts"], repo_id=rid_a)
        assert learner.record_coactivation(["billing.py", "invoices.py"], repo_id=rid_b)

        # Reload from disk to verify persistence
        reloaded = Learner()
        graph_a = reloaded.get_learned_coactivation(repo_id=rid_a)
        graph_b = reloaded.get_learned_coactivation(repo_id=rid_b)

        assert graph_a == {"auth.ts": ["roster.ts"], "roster.ts": ["auth.ts"]}
        assert graph_b == {"billing.py": ["invoices.py"], "invoices.py": ["billing.py"]}
        assert "billing.py" not in graph_a
        assert "auth.ts" not in graph_b

    def test_learn_from_turns_isolates_projects_automatically(self, isolated_learner_state):
        turns = []
        for i in range(4):
            turns.append({
                "turn_id": f"a{i}",
                "project": "c:/work/tacsnap",
                "files_injected": ["routes/roster.ts", "lib/auth.ts"],
                "files_used": ["routes/roster.ts"],
                "prompt_keywords": ["roster", "auth"],
            })
            turns.append({
                "turn_id": f"b{i}",
                "project": "c:/work/signet",
                "files_injected": ["audit/chain.py", "audit/verifier.py"],
                "files_used": ["audit/chain.py"],
                "prompt_keywords": ["audit", "chain"],
            })

        learner = Learner()
        learner.learn_from_turns(turns)

        rid_tacsnap = repo_namespace_id("c:/work/tacsnap")
        rid_signet = repo_namespace_id("c:/work/signet")

        tac_graph = learner.get_learned_coactivation(repo_id=rid_tacsnap)
        sig_graph = learner.get_learned_coactivation(repo_id=rid_signet)

        assert tac_graph == {
            "lib/auth.ts": ["routes/roster.ts"],
            "routes/roster.ts": ["lib/auth.ts"],
        }
        assert sig_graph == {
            "audit/chain.py": ["audit/verifier.py"],
            "audit/verifier.py": ["audit/chain.py"],
        }
        # Zero cross-repo edges in either scoped graph or legacy global graph
        legacy_global = learner.get_learned_coactivation()
        assert "audit/chain.py" not in legacy_global.get("routes/roster.ts", [])
        assert "routes/roster.ts" not in legacy_global.get("audit/chain.py", [])

    def test_merge_ingested_state_merges_per_repo_coactivation(self, isolated_learner_state):
        learner = Learner()
        rid = repo_namespace_id("c:/work/tacsnap")
        learner.record_coactivation(["a.ts", "b.ts"], repo_id=rid, save=False)

        learner.merge_ingested_state({
            "coactivation_by_repo": {
                rid: {"a.ts": ["c.ts"], "c.ts": ["a.ts"]},
            },
            "meta": {"turns_learned": 10},
        })

        merged = learner.get_learned_coactivation(repo_id=rid)
        assert set(merged["a.ts"]) == {"b.ts", "c.ts"}
        assert merged["c.ts"] == ["a.ts"]
