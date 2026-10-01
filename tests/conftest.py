"""Test configuration — keep everything in a temp directory."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="rubika_dl_tests_")
os.environ.setdefault("RUBIKA_BOT_TOKEN", "123:TESTTOKEN")
os.environ.setdefault("STATE_DIR", os.path.join(_tmp, "state"))
os.environ.setdefault("DOWNLOAD_DIR", os.path.join(_tmp, "downloads"))
os.environ.setdefault("STATE_GIT_PUSH", "false")
os.environ.setdefault("GITHUB_ACTIONS", "false")
