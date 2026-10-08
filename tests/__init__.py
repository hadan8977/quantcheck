# Lets `python -m unittest tests.test_x` resolve `import _test_env` too
# (discovery with `-s tests` already puts this directory on sys.path).
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
