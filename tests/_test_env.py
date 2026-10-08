"""Imported first by every test module.

quantcheck modules resolve ROOT/STATE/LOGS from QUANTCHECK_HOME (or the repo
root) at import time, and the repo root *is* the production install on the
server. Running the suite there used to append fake events to production logs
(e.g. "official mail forwarded=1; triggering forced picks check" in
quantcheck_scheduler.log) and could read the production .env. Point
QUANTCHECK_HOME at a throwaway directory before anything imports quantcheck.
"""

import atexit
import os
import shutil
import tempfile

if not os.environ.get("QUANTCHECK_TEST_HOME"):
    _home = tempfile.mkdtemp(prefix="quantcheck-test-home-")
    os.environ["QUANTCHECK_TEST_HOME"] = _home
    atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["QUANTCHECK_HOME"] = os.environ["QUANTCHECK_TEST_HOME"]
