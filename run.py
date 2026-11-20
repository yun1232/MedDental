"""Start the complete local oral leukoplakia application on localhost:8080."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "oral_memory"))

from local_app import main


if __name__ == "__main__":
    main()
