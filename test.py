"""Evaluate etiology and treatment using an immutable training-memory snapshot."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'integrations/oral-memory'))
from offline_run import main

if __name__ == '__main__':
    raise SystemExit(main('test'))
