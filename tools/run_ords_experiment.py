"""Run repeated test iterations under load for ORDS backup flake experiment."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def copy_variant(variant: str) -> None:
    source = ROOT / "tools" / "variants" / f"backup_db_{variant}.sh"
    destination = ROOT / "scripts" / "backup_db.sh"
    if not source.is_file():
        raise SystemExit(f"Variant file not found: {source}")
    shutil.copy2(source, destination)
    # Ensure executable permission
    destination.chmod(0o755)
    print(f"Copied {source.name} -> {destination.name}")


def run_one_iteration(worker_id: int, iteration: int) -> tuple[int, int, int, float, str, str]:
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_backup_ords_cli.py",
        "-k",
        "test_an_ords_failure_installs_no_table_or_code_mirror",
        "-k",
        "test_every_untrustworthy_export_is_refused_and_the_previous_mirror_kept",
    ]
    start = time.monotonic()
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    elapsed = time.monotonic() - start
    return worker_id, iteration, result.returncode, elapsed, result.stdout, result.stderr


def worker_task(worker_id: int, count: int) -> list[tuple[int, int, int, float, str, str]]:
    results = []
    for i in range(1, count + 1):
        res = run_one_iteration(worker_id, i)
        worker_id, it, rc, elapsed, stdout, stderr = res
        if rc == 0:
            print(f"[Worker {worker_id}] iteration {it}/{count} passed in {elapsed:.1f}s")
        else:
            print(f"[Worker {worker_id}] iteration {it}/{count} FAILED in {elapsed:.1f}s (rc={rc})")
            print("=== STDOUT ===")
            print(stdout)
            print("=== STDERR ===")
            print(stderr)
            print("==============")
        results.append(res)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", required=True, choices=["team_current", "template_26_1", "team_traced"])
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--iterations-per-worker", type=int, default=8)
    args = parser.parse_args()

    copy_variant(args.variant)

    print(f"Starting experiment: variant={args.variant}, workers={args.workers}, iter_per_worker={args.iterations_per_worker}")
    start_total = time.monotonic()

    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(worker_task, worker_id, args.iterations_per_worker)
            for worker_id in range(1, args.workers + 1)
        ]
        for f in concurrent.futures.as_completed(futures):
            all_results.extend(f.result())

    total_time = time.monotonic() - start_total
    total_runs = len(all_results)
    failed_runs = sum(1 for r in all_results if r[2] != 0)
    passed_runs = total_runs - failed_runs

    print("\n" + "=" * 60)
    print(f"EXPERIMENT SUMMARY: variant={args.variant}")
    print(f"Total time: {total_time:.1f}s")
    print(f"Total runs: {total_runs}, Passed: {passed_runs}, Failed: {failed_runs}")
    print("=" * 60)

    if failed_runs > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
