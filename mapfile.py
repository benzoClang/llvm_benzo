#!/usr/bin/env python3
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
"""Util to create a mapfile."""

from base_builders import Builder
from pathlib import Path
import sys
import subprocess

def create_map_file(lib_file: Path, map_file: Path, section_name: str) -> None:
    """Creates a map_file for lib_file."""
    runtime_nm = Builder.toolchain.nm
    symbols = subprocess.check_output([runtime_nm, '--extern-only', '--defined-only',
                                      str(lib_file)], text=True)
    with map_file.open('w') as output:
        output.write('# AUTO-GENERATED by mapfile.py. DO NOT EDIT.\n')
        output.write(f'LIBCLANG_RT_{section_name} {{\n')
        output.write('  global:\n')
        for line in symbols.splitlines():
            _, symbol_type, symbol_name = line.split(' ', 2)
            if symbol_type in ['T', 'W', 'B', 'i']:
                output.write(f'    {symbol_name};\n')
        output.write('  local:\n')
        output.write('    *;\n')
        output.write('};\n')

# for testing and standalone usage.
if __name__ == '__main__':
    create_map_file(Path(sys.argv[1]), Path(sys.argv[2]), str(sys.argv[3]))
