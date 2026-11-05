"""Evaluation harness for the interview scheduling agent.

See evals/harness/README.md for the agent contract and how to run evaluations.
"""
from __future__ import annotations

import os
import sys

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

HARNESS_VERSION = "0.1.0"
CONTRACT_VERSION = "1.0"
