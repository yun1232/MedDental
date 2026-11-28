"""Train the case memory using direct multimodal API calls (model weights stay frozen)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'integrations/oral-memory'))
from offline_run import main

if __name__ == '__main__':
    raise SystemExit(main('train'))
