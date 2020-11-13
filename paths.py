#
# Copyright (C) 2020 The Android Open Source Project
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
"""Helpers for paths."""

import os
from pathlib import Path
import string
from typing import Optional

import benzo_version
import constants
import hosts

SCRIPTS_DIR: Path = Path(__file__).resolve().parent
ANDROID_DIR: Path = SCRIPTS_DIR.parents[1]
OUT_DIR: Path = Path(os.environ.get('OUT_DIR', ANDROID_DIR / 'out')).resolve()
SYSROOTS: Path = OUT_DIR / 'sysroots'
LLVM_PATH: Path = ANDROID_DIR / 'toolchain' / 'llvm-project'
PREBUILTS_DIR: Path = ANDROID_DIR / 'prebuilts'
EXTERNAL_DIR: Path = ANDROID_DIR / 'external'
TOOLCHAIN_DIR: Path = ANDROID_DIR / 'toolchain'
TOOLCHAIN_UTILS_DIR: Path = EXTERNAL_DIR / 'toolchain-utils'
TOOLCHAIN_LLVM_PATH: Path = TOOLCHAIN_DIR / 'llvm-project'

CLANG_PREBUILT_DIR: Path = (PREBUILTS_DIR / 'clang' / 'host' / hosts.build_host().os_tag
                            / constants.CLANG_PREBUILT_VERSION)
CLANG_PREBUILT_LIBCXX_HEADERS: Path = CLANG_PREBUILT_DIR / 'include' / 'c++' / 'v1'
BIONIC_HEADERS: Path = ANDROID_DIR / 'bionic' / 'libc' / 'include'

GO_BIN_PATH: Path = PREBUILTS_DIR / 'go' / hosts.build_host().os_tag / 'bin'
CMAKE_BIN_PATH: Path = PREBUILTS_DIR / 'cmake' / hosts.build_host().os_tag / 'bin' / 'cmake'
NINJA_BIN_PATH: Path = PREBUILTS_DIR / 'cmake' / hosts.build_host().os_tag / 'bin' / 'ninja'

NDK_BASE: Path = TOOLCHAIN_DIR / 'prebuilts' /'ndk' / constants.NDK_VERSION
NDK_LIBCXX_HEADERS: Path = NDK_BASE / 'sources' / 'cxx-stl' / 'llvm-libc++'/ 'include'
NDK_LIBCXXABI_HEADERS: Path = NDK_BASE / 'sources' / 'cxx-stl' / 'llvm-libc++abi' / 'include'
NDK_SUPPORT_HEADERS: Path = NDK_BASE / 'sources' / 'android' / 'support' / 'include'

GCC_ROOT: Path = PREBUILTS_DIR / 'gcc' / hosts.build_host().os_tag

def pgo_profdata_filename() -> str:
    svn_revision = benzo_version.svn_revision
    base_revision = svn_revision.rstrip(string.ascii_lowercase)
    return f'{base_revision}.profdata'

def pgo_profdata_file(profdata_file) -> Optional[Path]:
    profile = (PREBUILTS_DIR / 'clang' / 'host' / 'linux-x86' / 'profiles' /
               profdata_file)
    return profile if profile.exists() else None

def get_package_install_path(host: hosts.Host, package_name) -> Path:
    return OUT_DIR / 'install' / host.os_tag / package_name

def get_python_dir(host: hosts.Host) -> Path:
    """Returns the path to python for a host."""
    return PREBUILTS_DIR / 'python' / host.os_tag

def get_python_executable(host: hosts.Host) -> Path:
    """Returns the path to python executable for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'bin' / 'python3.8',
    }[host]

def get_python_include_dir(host: hosts.Host) -> Path:
    """Returns the path to python include dir for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'include' / 'python3.8',
    }[host]

def get_python_lib(host: hosts.Host) -> Path:
    """Returns the path to python lib for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'lib' / 'libpython3.8.so',
    }[host]

def get_python_dynamic_lib(host: hosts.Host) -> Path:
    """Returns the path to python runtime dynamic lib for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'lib' / 'libpython3.8.so.1.0',
    }[host]
