"""Read-only verification of the previously approved pinned installation."""
from pathlib import Path
import sys

from run_local_contract import verify

if __name__ == "__main__":
    verify(Path(sys.argv[1]).resolve())
