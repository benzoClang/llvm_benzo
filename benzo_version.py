#!/usr/bin/env python
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

import paths
import re
import utils

_patch_level = '0'
_svn_revision = ''

def get_svn_revision():
    if _svn_revision != '':
        return _svn_revision
    rev_script = str(paths.SCRIPTS_DIR / 'get-llvm-revision.sh')
    revision = utils.check_output(['sh', rev_script]).strip()
    return revision

def get_patch_level():
    if _patch_level != '':
        return _patch_level
    return 0

# Get the numeric portion of the version number we are working with.
# Strip the leading 'r' and possible letter (and number) suffix,
# e.g., r383902b1 => 383902
def get_svn_revision_number():
    svn_version = get_svn_revision()
    found = re.match(r'r(\d+)([a-z]\d*)?$', svn_version)
    if not found:
        raise RuntimeError(f'Invalid svn revision: {svn_version}')
    return found.group(1)
