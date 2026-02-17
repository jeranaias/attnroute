#!/usr/bin/env python3
"""
ETL pipeline for converting GH Archive commit data into attnroute training format.

GH Archive records all public GitHub events. PushEvents contain commit messages
and file lists, which map naturally to attnroute's training format:
  - commit_message → prompt
  - changed_files → file_sequence

Two data ingestion modes:
  1. BigQuery (recommended for large-scale): SQL query against GitHub Archive dataset
  2. Local git repos: Extract from cloned repositories' git log

Usage:
    # From local git repos
    python -m attnroute.data.gh_archive_etl local /path/to/repos/ --output training_data.json

    # Generate BigQuery SQL (run in Google Cloud Console, export results)
    python -m attnroute.data.gh_archive_etl bigquery-sql --output query.sql
"""
import json
import subprocess
import sys
from pathlib import Path

# Target languages (file extensions to include)
TARGET_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt",
}

# Limits
MAX_FILES_PER_COMMIT = 20
MIN_FILES_PER_COMMIT = 1
MIN_MESSAGE_LENGTH = 10
MAX_MESSAGE_LENGTH = 500


def generate_bigquery_sql(
    max_rows: int = 200_000,
    start_date: str = "2025-01-01",
) -> str:
    """Generate BigQuery SQL for extracting training data from GH Archive.

    Run this SQL in Google Cloud BigQuery console, then export results as JSON.
    Free tier: 1TB/month query limit.
    """
    return f"""-- attnroute training data extraction from GH Archive
-- Run in Google Cloud BigQuery (free 1TB/month)
-- Export results as JSONL

WITH push_events AS (
  SELECT
    repo.name as repo_name,
    created_at,
    JSON_EXTRACT_SCALAR(payload, '$.head') as commit_sha,
    c.message as commit_message,
    ARRAY(
      SELECT DISTINCT filename
      FROM UNNEST(c.files) as f
      WHERE f.filename IS NOT NULL
        AND REGEXP_CONTAINS(f.filename, r'\\.(py|js|ts|tsx|jsx|go|rs|java|c|cpp|h|rb)$')
    ) as changed_files
  FROM `githubarchive.month.{start_date.replace('-', '')}*`,
  UNNEST(payload.commits) as c
  WHERE type = 'PushEvent'
    AND c.message IS NOT NULL
    AND LENGTH(c.message) >= {MIN_MESSAGE_LENGTH}
    AND LENGTH(c.message) <= {MAX_MESSAGE_LENGTH}
    AND c.distinct = true
)
SELECT
  repo_name,
  commit_message,
  changed_files,
  ARRAY_LENGTH(changed_files) as file_count
FROM push_events
WHERE ARRAY_LENGTH(changed_files) BETWEEN {MIN_FILES_PER_COMMIT} AND {MAX_FILES_PER_COMMIT}
ORDER BY created_at DESC
LIMIT {max_rows}
"""


def extract_from_git_log(
    repo_dir: str | Path,
    max_commits: int = 500,
) -> list[dict]:
    """Extract training data from a local git repository.

    Args:
        repo_dir: Path to git repository
        max_commits: Maximum commits to process

    Returns:
        List of {prompt, files, repo_name} dicts
    """
    repo_dir = Path(repo_dir)
    repo_name = repo_dir.name

    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_commits}",
             "--name-only", "--pretty=format:---MSG---%n%s"],
            capture_output=True, text=True, cwd=str(repo_dir),
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    samples = []
    current_msg = None
    current_files = []

    for line in result.stdout.splitlines():
        if line.startswith("---MSG---"):
            # Save previous commit
            if current_msg and current_files:
                samples.append({
                    "prompt": current_msg,
                    "files": current_files[:MAX_FILES_PER_COMMIT],
                    "repo_name": repo_name,
                })
            current_msg = None
            current_files = []
            continue

        if current_msg is None:
            # This is the commit message
            msg = line.strip()
            if len(msg) >= MIN_MESSAGE_LENGTH:
                current_msg = msg
            continue

        # File path line
        fpath = line.strip()
        if fpath and Path(fpath).suffix.lower() in TARGET_EXTENSIONS:
            current_files.append(fpath)

    # Don't forget the last commit
    if current_msg and current_files:
        samples.append({
            "prompt": current_msg,
            "files": current_files[:MAX_FILES_PER_COMMIT],
            "repo_name": repo_name,
        })

    return samples


def extract_from_repos_dir(
    repos_dir: str | Path,
    max_repos: int = 100,
    max_commits_per_repo: int = 500,
) -> list[dict]:
    """Extract training data from a directory of git repositories.

    Args:
        repos_dir: Directory containing multiple git repos
        max_repos: Maximum repositories to process
        max_commits_per_repo: Commits per repo

    Returns:
        Combined list of training samples
    """
    repos_dir = Path(repos_dir)
    all_samples = []
    repos_found = 0

    for entry in repos_dir.iterdir():
        if repos_found >= max_repos:
            break
        if not entry.is_dir():
            continue
        git_dir = entry / ".git"
        if not git_dir.exists():
            continue

        repos_found += 1
        print(f"  [{repos_found}] Extracting from {entry.name}...", file=sys.stderr)
        samples = extract_from_git_log(entry, max_commits=max_commits_per_repo)
        all_samples.extend(samples)
        print(f"    → {len(samples)} samples", file=sys.stderr)

    return all_samples


def convert_to_training_format(
    samples: list[dict],
) -> list[dict]:
    """Convert raw samples to attnroute neural_predictor training format.

    Groups samples by repo into sessions, with file sequences.
    """
    from collections import defaultdict

    # Group by repo
    by_repo: dict[str, list] = defaultdict(list)
    for s in samples:
        by_repo[s.get("repo_name", "unknown")].append(s)

    sessions = []
    for repo, repo_samples in by_repo.items():
        turns = []
        file_sequence = []
        for s in repo_samples:
            turns.append({
                "prompt": s["prompt"],
                "files": s["files"],
            })
            file_sequence.extend(s["files"])

        if len(turns) >= 3:
            sessions.append({
                "repo": repo,
                "turns": turns,
                "file_sequence": file_sequence,
            })

    return sessions


def main():
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="GH Archive ETL for attnroute")
    sub = parser.add_subparsers(dest="mode")

    # Local git repos mode
    local_p = sub.add_parser("local", help="Extract from local git repos")
    local_p.add_argument("repos_dir", help="Directory containing git repos")
    local_p.add_argument("--output", default="training_data.json")
    local_p.add_argument("--max-repos", type=int, default=100)
    local_p.add_argument("--max-commits", type=int, default=500)

    # BigQuery SQL mode
    bq_p = sub.add_parser("bigquery-sql", help="Generate BigQuery SQL")
    bq_p.add_argument("--output", default=None)
    bq_p.add_argument("--max-rows", type=int, default=200_000)

    args = parser.parse_args()

    if args.mode == "local":
        print(f"Extracting from repos in {args.repos_dir}...", file=sys.stderr)
        samples = extract_from_repos_dir(
            args.repos_dir,
            max_repos=args.max_repos,
            max_commits_per_repo=args.max_commits,
        )
        sessions = convert_to_training_format(samples)
        print(f"\nTotal: {len(samples)} samples → {len(sessions)} sessions",
              file=sys.stderr)

        output_path = Path(args.output)
        output_path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
        print(f"Saved to {output_path}", file=sys.stderr)

    elif args.mode == "bigquery-sql":
        sql = generate_bigquery_sql(max_rows=args.max_rows)
        if args.output:
            Path(args.output).write_text(sql, encoding="utf-8")
            print(f"SQL saved to {args.output}", file=sys.stderr)
        else:
            print(sql)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
