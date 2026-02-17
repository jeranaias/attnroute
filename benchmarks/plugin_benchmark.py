#!/usr/bin/env python3
"""
Plugin Benchmark — Tests VerifyFirst, LoopBreaker, and BurnRate
against predefined scenarios to measure detection accuracy,
false positive rates, and performance.

Usage:
    python benchmarks/plugin_benchmark.py             # Run all plugin benchmarks
    python benchmarks/plugin_benchmark.py --plugin verifyfirst  # Single plugin
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


# ============================================================================
# SCENARIO RUNNER
# ============================================================================

def run_verifyfirst_scenarios() -> dict:
    """Run VerifyFirst scenarios and measure accuracy."""
    from attnroute.plugins.verifyfirst import VerifyFirstPlugin

    scenario_file = SCENARIOS_DIR / "verifyfirst_scenarios.json"
    scenarios = json.loads(scenario_file.read_text(encoding="utf-8"))

    results = []

    for scenario in scenarios["scenarios"]:
        plugin = VerifyFirstPlugin()
        plugin.on_session_start({"session_id": f"bench-{scenario['name']}"})

        warnings = []
        timings = []

        for step in scenario["steps"]:
            action = step["action"]

            if action == "on_stop":
                t0 = time.perf_counter()
                warning = plugin.on_stop(step["tool_calls"], {})
                elapsed = (time.perf_counter() - t0) * 1000
                timings.append(elapsed)
                if warning:
                    warnings.append(warning)

            elif action == "on_stop_repeat":
                for _ in range(step["repeat"]):
                    t0 = time.perf_counter()
                    warning = plugin.on_stop(step["tool_calls"], {})
                    elapsed = (time.perf_counter() - t0) * 1000
                    timings.append(elapsed)
                    if warning:
                        warnings.append(warning)

        # Evaluate results
        expect_violation = scenario.get("expect_violation", False)
        got_violation = len(warnings) > 0
        passed = got_violation == expect_violation

        # Check severity if expected
        severity_correct = True
        if expect_violation and scenario.get("expect_severity"):
            expected_sev = scenario["expect_severity"]
            severity_correct = any(expected_sev in w for w in warnings)

        # Check analytics if requested
        analytics_ok = True
        if scenario.get("check_analytics"):
            summary = plugin.get_session_summary()
            # Should have both violations and clean edits
            analytics_ok = (
                summary.get("total_edits_all_time", 0) > 0
                and summary.get("violations", 0) > 0
            )

        results.append({
            "name": scenario["name"],
            "description": scenario["description"],
            "passed": passed and severity_correct and analytics_ok,
            "expect_violation": expect_violation,
            "got_violation": got_violation,
            "severity_correct": severity_correct,
            "analytics_ok": analytics_ok,
            "warnings": warnings,
            "avg_time_ms": sum(timings) / len(timings) if timings else 0,
            "max_time_ms": max(timings) if timings else 0,
        })

    return {
        "plugin": "VerifyFirst",
        "version": "2.0.0",
        "scenarios": results,
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "avg_time_ms": sum(r["avg_time_ms"] for r in results) / len(results) if results else 0,
    }


def run_loopbreaker_scenarios() -> dict:
    """Run LoopBreaker scenarios and measure accuracy."""
    from attnroute.plugins.loopbreaker import LoopBreakerPlugin

    scenario_file = SCENARIOS_DIR / "loopbreaker_scenarios.json"
    scenarios = json.loads(scenario_file.read_text(encoding="utf-8"))

    results = []

    for scenario in scenarios["scenarios"]:
        plugin = LoopBreakerPlugin()
        plugin.on_session_start({"session_id": f"bench-{scenario['name']}"})

        warnings = []
        timings = []
        severities_seen = set()

        for step in scenario["steps"]:
            action = step["action"]

            if action == "on_stop":
                t0 = time.perf_counter()
                warning = plugin.on_stop(step["tool_calls"], {})
                elapsed = (time.perf_counter() - t0) * 1000
                timings.append(elapsed)
                if warning:
                    warnings.append(warning)
                    if "[CRITICAL]" in warning:
                        severities_seen.add("CRITICAL")
                    elif "[WARNING]" in warning:
                        severities_seen.add("WARNING")
                    else:
                        severities_seen.add("NOTICE")

            elif action == "on_stop_repeat":
                for _ in range(step["repeat"]):
                    t0 = time.perf_counter()
                    warning = plugin.on_stop(step["tool_calls"], {})
                    elapsed = (time.perf_counter() - t0) * 1000
                    timings.append(elapsed)
                    if warning:
                        warnings.append(warning)
                        if "[CRITICAL]" in warning:
                            severities_seen.add("CRITICAL")
                        elif "[WARNING]" in warning:
                            severities_seen.add("WARNING")
                        else:
                            severities_seen.add("NOTICE")

        # Evaluate results
        expect_detection = scenario.get("expect_detection", False)
        got_detection = len(warnings) > 0
        passed = got_detection == expect_detection

        # Check severity progression
        severity_ok = True
        if scenario.get("expect_severity_progression"):
            expected = set(scenario["expect_severity_progression"])
            severity_ok = expected.issubset(severities_seen)

        # Check bounce detection
        bounce_ok = True
        if scenario.get("expect_bounce"):
            summary = plugin.get_session_summary()
            bounce_ok = summary.get("bounce_detected") is not None

        # Check error tracking
        error_ok = True
        if scenario.get("expect_error_tracking"):
            summary = plugin.get_session_summary()
            state = plugin.load_state()
            error_patterns = state.get("error_patterns", {})
            error_ok = len(error_patterns) > 0

        results.append({
            "name": scenario["name"],
            "description": scenario["description"],
            "passed": passed and severity_ok and bounce_ok and error_ok,
            "expect_detection": expect_detection,
            "got_detection": got_detection,
            "severity_ok": severity_ok,
            "severities_seen": sorted(severities_seen),
            "bounce_ok": bounce_ok,
            "error_ok": error_ok,
            "warnings_count": len(warnings),
            "avg_time_ms": sum(timings) / len(timings) if timings else 0,
            "max_time_ms": max(timings) if timings else 0,
        })

    return {
        "plugin": "LoopBreaker",
        "version": "2.0.0",
        "scenarios": results,
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "avg_time_ms": sum(r["avg_time_ms"] for r in results) / len(results) if results else 0,
    }


def run_burnrate_scenarios() -> dict:
    """Run BurnRate scenarios — tests lifecycle without errors."""
    from attnroute.plugins.burnrate import BurnRatePlugin

    scenario_file = SCENARIOS_DIR / "burnrate_scenarios.json"
    scenarios = json.loads(scenario_file.read_text(encoding="utf-8"))

    results = []

    for scenario in scenarios["scenarios"]:
        plugin = BurnRatePlugin()
        errors = []
        timings = []

        for step in scenario["steps"]:
            action = step["action"]

            try:
                if action == "on_session_start":
                    t0 = time.perf_counter()
                    plugin.on_session_start(step.get("session_state", {}))
                    elapsed = (time.perf_counter() - t0) * 1000
                    timings.append(elapsed)

                elif action == "on_prompt_post":
                    t0 = time.perf_counter()
                    plugin.on_prompt_post(
                        step.get("prompt", ""),
                        step.get("context_output", ""),
                        step.get("session_state", {})
                    )
                    elapsed = (time.perf_counter() - t0) * 1000
                    timings.append(elapsed)

                elif action == "on_stop":
                    t0 = time.perf_counter()
                    plugin.on_stop(step["tool_calls"], {})
                    elapsed = (time.perf_counter() - t0) * 1000
                    timings.append(elapsed)

                elif action == "on_stop_repeat":
                    for _ in range(step["repeat"]):
                        t0 = time.perf_counter()
                        plugin.on_stop(step["tool_calls"], {})
                        elapsed = (time.perf_counter() - t0) * 1000
                        timings.append(elapsed)

            except Exception as e:
                errors.append(f"{action}: {e}")

        expect_error = scenario.get("expect_error", False)
        got_error = len(errors) > 0
        passed = got_error == expect_error

        results.append({
            "name": scenario["name"],
            "description": scenario["description"],
            "passed": passed,
            "errors": errors,
            "avg_time_ms": sum(timings) / len(timings) if timings else 0,
            "max_time_ms": max(timings) if timings else 0,
        })

    return {
        "plugin": "BurnRate",
        "version": "0.3.0",
        "scenarios": results,
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "avg_time_ms": sum(r["avg_time_ms"] for r in results) / len(results) if results else 0,
    }


# ============================================================================
# REPORT
# ============================================================================

def print_plugin_report(result: dict):
    """Print results for one plugin."""
    name = result["plugin"]
    version = result["version"]
    passed = result["passed"]
    total = result["total"]
    avg_ms = result["avg_time_ms"]

    status = "PASS" if passed == total else "FAIL"
    print(f"  {name} v{version}")
    print(f"    Scenarios: {passed}/{total} passed  [{status}]")

    # Detection accuracy
    scenarios = result["scenarios"]
    true_pos = sum(1 for s in scenarios if s.get("expect_detection", s.get("expect_violation", False)) and s.get("got_detection", s.get("got_violation", False)))
    true_neg = sum(1 for s in scenarios if not s.get("expect_detection", s.get("expect_violation", False)) and not s.get("got_detection", s.get("got_violation", False)))
    false_pos = sum(1 for s in scenarios if not s.get("expect_detection", s.get("expect_violation", False)) and s.get("got_detection", s.get("got_violation", False)))
    false_neg = sum(1 for s in scenarios if s.get("expect_detection", s.get("expect_violation", False)) and not s.get("got_detection", s.get("got_violation", False)))

    total_decisions = true_pos + true_neg + false_pos + false_neg
    accuracy = (true_pos + true_neg) / total_decisions * 100 if total_decisions > 0 else 0
    fp_rate = false_pos / (false_pos + true_neg) * 100 if (false_pos + true_neg) > 0 else 0

    print(f"    Detection accuracy: {accuracy:.0f}%")
    print(f"    False positive rate: {fp_rate:.0f}%")
    print(f"    Avg on_stop() time: {avg_ms:.1f}ms")

    # Show failures
    failures = [s for s in scenarios if not s["passed"]]
    if failures:
        print("    Failures:")
        for f in failures:
            print(f"      - {f['name']}: {f['description']}")
    print()


def run_all_plugins(plugin_filter: str = None) -> list[dict]:
    """Run all plugin benchmarks."""
    print()
    print("=" * 70)
    print("PLUGIN BENCHMARK RESULTS")
    print("=" * 70)
    print()

    results = []

    runners = [
        ("verifyfirst", run_verifyfirst_scenarios),
        ("loopbreaker", run_loopbreaker_scenarios),
        ("burnrate", run_burnrate_scenarios),
    ]

    for name, runner in runners:
        if plugin_filter and name != plugin_filter:
            continue
        try:
            result = runner()
            results.append(result)
            print_plugin_report(result)
        except Exception as e:
            print(f"  {name}: ERROR — {e}")
            import traceback
            traceback.print_exc()
            print()

    # Summary
    total_passed = sum(r["passed"] for r in results)
    total_scenarios = sum(r["total"] for r in results)
    all_pass = total_passed == total_scenarios

    print("-" * 70)
    if all_pass:
        print(f"  ALL PLUGINS PASSED ({total_passed}/{total_scenarios} scenarios)")
    else:
        print(f"  FAILURES: {total_passed}/{total_scenarios} scenarios passed")
    print()

    # Save results to JSON
    output_file = Path(__file__).parent / "plugin_results.json"
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "results": results,
        }, f, indent=2, default=str)
    print(f"  Results saved to: {output_file}")
    print()

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Plugin benchmark suite")
    parser.add_argument("--plugin", choices=["verifyfirst", "loopbreaker", "burnrate"],
                        help="Run only one plugin's scenarios")
    args = parser.parse_args()

    results = run_all_plugins(plugin_filter=args.plugin)
    sys.exit(0 if all(r["passed"] == r["total"] for r in results) else 1)
