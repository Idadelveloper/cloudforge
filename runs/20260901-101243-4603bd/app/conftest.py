"""Ensure the project root is importable when pytest collects tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
