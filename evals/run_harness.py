#!/usr/bin/env python3
"""Entry point for the evaluation harness. See evals/harness/README.md.

    python evals/run_harness.py run --agent null
    python evals/run_harness.py self-check
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
