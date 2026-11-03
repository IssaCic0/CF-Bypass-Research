"""Run a command under a Windows Job Object with a hard memory cap.

Usage: python scripts/run_limited.py --max-mb 4096 -- <command> [args...]

The job object kills the whole process tree when committed memory exceeds the
cap, so a runaway solver can never take the machine down. Requires pywin32;
falls back to an uncapped run with a loud warning when unavailable.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

# Job state flags we need.
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


def _job_capsule(max_bytes: int):
    """Creates a Job Object capped at `max_bytes`, or returns None without pywin32."""
    try:
        import win32api  # noqa: F401
        import win32job
        import win32process
    except ImportError:
        return None

    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    basic = info["BasicLimitInformation"]
    basic["LimitFlags"] = (
        basic.get("LimitFlags", 0) | JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info["ProcessMemoryLimit"] = max_bytes
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-mb", type=int, default=4096, help="memory cap in MB (default 4096)")
    parser.add_argument("command", nargs="+", help="command to run (use -- before it)")
    args = parser.parse_args()

    max_bytes = args.max_mb * 1024 * 1024
    job = _job_capsule(max_bytes)
    if job is None:
        print(
            f"WARNING: pywin32 unavailable; running WITHOUT the {args.max_mb}MB cap. "
            "pip install pywin32 to enable the limiter.",
            file=sys.stderr,
        )

    process = subprocess.Popen(args.command)
    if job is not None:
        import win32job

        handle = int(process._handle)  # noqa: SLF001 - the documented way to pass a proc handle
        win32job.AssignProcessToJobObject(job, handle)

    try:
        code = process.wait()
    except KeyboardInterrupt:
        process.kill()
        return 130
    if code and code < 0:
        print(f"process was terminated (likely by the {args.max_mb}MB cap)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
