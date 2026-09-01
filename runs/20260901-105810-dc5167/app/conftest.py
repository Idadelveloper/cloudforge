"""Pytest configuration making the application modules importable from tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
