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

import datetime
import logging
import os
import shlex
import shutil
import stat
import subprocess
from typing import List

import constants

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def unchecked_call(cmd, *args, **kwargs):
    """subprocess.call with logging."""
    logger().info('call:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    subprocess.call(cmd, *args, **kwargs)


def subprocess_run(cmd, *args, **kwargs):
    """subprocess.run with logging."""
    logger().info('subprocess.run:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    return subprocess.run(cmd, *args, **kwargs, text=True)


def check_call(cmd, *args, **kwargs):
    """subprocess.check_call with logging."""
    logger().info('check_call:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    subprocess.check_call(cmd, *args, **kwargs)


def check_output(cmd, *args, **kwargs):
    """subprocess.check_output with logging."""
    logger().info('check_output:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    return subprocess.check_output(cmd, *args, **kwargs, text=True)

def list2cmdline(args: List[str]) -> str:
    """Joins arguments into a Bourne-shell cmdline.

    Like shlex.join from Python 3.8, but is flexible about the argument type.
    Each argument can be a str, a bytes, or a path-like object. (subprocess.call
    is similarly flexible.)

    Similar to the undocumented subprocess.list2cmdline, but does Bourne-style
    escaping rather than MSVCRT escaping.
    """
    return ' '.join([shlex.quote(os.fsdecode(arg)) for arg in args])
