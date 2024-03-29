#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pylint: disable=not-callable

import contextlib
import datetime
import logging
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
from typing import Dict, List

import constants


ORIG_ENV = dict(os.environ)

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def subprocess_run(cmd, *args, **kwargs):
    """subprocess.run with logging."""
    logger().debug('subprocess.run:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    if kwargs.pop('dry_run', None):
        return None
    return subprocess.run(cmd, *args, **kwargs, text=True)


def unchecked_call(cmd, *args, **kwargs):
    """subprocess.call with logging."""
    return subprocess_run(cmd, *args, **kwargs).returncode


def check_call(cmd, *args, **kwargs):
    """subprocess.check_call with logging."""
    return subprocess_run(cmd, *args, **kwargs, check=True)


def check_output(cmd, *args, **kwargs):
    """subprocess.check_output with logging."""
    return subprocess_run(cmd, *args, **kwargs, check=True, stdout=subprocess.PIPE).stdout

def list2cmdline(args: List[str]) -> str:
    """Joins arguments into a Bourne-shell cmdline.

    Like shlex.join from Python 3.8, but is flexible about the argument type.
    Each argument can be a str, a bytes, or a path-like object. (subprocess.call
    is similarly flexible.)

    Similar to the undocumented subprocess.list2cmdline, but does Bourne-style
    escaping rather than MSVCRT escaping.
    """
    return ' '.join([shlex.quote(os.fsdecode(arg)) for arg in args])


def create_script(script_path: Path, cmd: List[str], env: Dict[str, str]) -> None:
    with script_path.open('w') as outf:
        outf.write('#!/bin/sh\n')
        for k, v in env.items():
            if v != ORIG_ENV.get(k):
                outf.write(f'export {k}="{v}"\n')
        outf.write(list2cmdline(cmd) + ' $@\n')
    script_path.chmod(0o755)

@contextlib.contextmanager
def chdir_context(directory):
    prev_dir = os.getcwd()
    try:
        os.chdir(directory)
        yield
    finally:
        os.chdir(prev_dir)
