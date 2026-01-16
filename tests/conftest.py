"""
Pytest configuration for UDR tests.

This file ensures the python package is on the path before tests run.
"""

import os
import sys

# Add python package to path for imports
python_path = os.path.join(os.path.dirname(__file__), '..', 'python')
if python_path not in sys.path:
    sys.path.insert(0, python_path)
