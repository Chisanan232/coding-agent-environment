#!/usr/bin/env python3
"""Test-only launcher for scripts/release-line.py: makes the account database report the fixture home, then runs the CLI as __main__.

The CLI takes its home from pwd.getpwuid, never from $HOME, so tests cannot steer it with an environment variable; they patch the account lookup
here instead. Nothing in scripts/ reads RL_TEST_PW_HOME."""
import os
import pathlib
import pwd
import runpy
import sys

real = pwd.getpwuid(os.getuid())
fixture = os.environ["RL_TEST_PW_HOME"]
fake = pwd.struct_passwd((real.pw_name, real.pw_passwd, real.pw_uid, real.pw_gid, real.pw_gecos, fixture, real.pw_shell))
pwd.getpwuid = lambda uid: fake if uid == os.getuid() else real
cli = str(pathlib.Path(__file__).resolve().parents[1] / "scripts/release-line.py")
sys.argv = [cli, *sys.argv[1:]]
runpy.run_path(cli, run_name="__main__")
