"""Where the frozen reference cases live.

The three cases carry their full artifact sets and are committed under
`tests/fixtures/golden/`. Tests copy from here rather than from
`paths.AGENTIC_FLOW_OUT`, which is gitignored runtime output: a developer's
machine has it because they have run the pipeline, and a clean checkout does
not. That difference is invisible locally and turned CI red on its first run
from a fresh clone.
"""

from pathlib import Path

GOLDEN_CASES = Path(__file__).resolve().parent / "fixtures" / "golden" / "cases"
