#!/usr/bin/env python3
"""Run regression tests without installing the skill into an agent registry."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'youtube-transcript/scripts/runtime'))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['PYTHONPATH'] = str(ROOT / 'youtube-transcript/scripts/runtime') + (
    os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT / 'tests')))
    raise SystemExit(not result.wasSuccessful())
