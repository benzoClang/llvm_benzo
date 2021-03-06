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
"""Builders for various build tools and build systems."""

import functools
from pathlib import Path
import datetime
import logging
import multiprocessing
import os
import shutil
import subprocess
from typing import Dict, List, Optional, Set, Sequence

import benzo_version
from builder_registry import BuilderRegistry
import configs
import constants
import hosts
import paths
import toolchains
import utils


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)

class Builder:  # pylint: disable=too-few-public-methods
    """Base builder type."""
    name: str = ""
    config_list: List[configs.Config]

    """Use prebuilt toolchain if not specified otherwise in constructor."""
    toolchain: toolchains.Toolchain = toolchains.get_prebuilt_toolchain()

    """The toolchain to install artifacts from this LLVMRuntimeBuilder."""
    output_toolchain: toolchains.Toolchain

    def __init__(self,
                 config_list: Optional[Sequence[configs.Config]]=None,
                 toolchain: Optional[toolchains.Toolchain]=None) -> None:
        if toolchain:
            self.toolchain = toolchain
        if config_list:
            self.config_list = list(config_list)
        self._config: configs.Config = self.config_list[0]

    @BuilderRegistry.register_and_build
    def build(self) -> None:
        """Builds all configs."""
        for config in self.config_list:
            self._config = config

            logger().info('Building %s for %s', self.name, self._config)
            self._build_config()
        self.install()

    def _build_config(self) -> None:
        raise NotImplementedError()

    def _is_cross_compiling(self) -> bool:
        return self._config.target_os != hosts.build_host()

    @property
    def _cc(self) -> Path:
        return self._config.get_c_compiler(self.toolchain)

    @property
    def _cxx(self) -> Path:
        return self._config.get_cxx_compiler(self.toolchain)

    @property
    def cflags(self) -> List[str]:
        """Additional cflags to use."""
        return []

    @property
    def cxxflags(self) -> List[str]:
        """Additional cxxflags to use."""
        return self.cflags

    @property
    def ldflags(self) -> List[str]:
        """Additional ldflags to use."""
        ldflags = []
        # When cross compiling, toolchain libs won't work on target arch.
        if not self._is_cross_compiling():
            ldflags.append(f'-L{self.toolchain.lib_dir}')
        return ldflags

    @property
    def env(self) -> Dict[str, str]:
        """Environment variables used when building."""
        env = dict(utils.ORIG_ENV)
        env.update(self._config.env)
        paths = [self._config.env.get('PATH'), utils.ORIG_ENV.get('PATH')]
        env['PATH'] = os.pathsep.join(p for p in paths if p)
        return env

    def install(self) -> None:
        """Installs built artifacts."""


class CMakeBuilder(Builder):
    """Builder for cmake targets."""
    config: configs.Config
    src_dir: Path
    remove_cmake_cache: bool = False
    remove_install_dir: bool = False
    ninja_targets: List[str] = []

    @property
    def output_dir(self) -> Path:
        """The path for intermediate results."""
        return paths.OUT_DIR / 'lib' / (f'{self.name}{self._config.output_suffix}')

    @property
    def install_dir(self) -> Path:
        """Returns the path this target will be installed to."""
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        """CMake defines."""
        cflags = self._config.cflags + self.cflags
        cxxflags = self._config.cxxflags + self.cxxflags
        ldflags = self._config.ldflags + self.ldflags
        cflags_str = ' '.join(cflags)
        cxxflags_str = ' '.join(cxxflags)
        ldflags_str = ' '.join(ldflags)
        cxx_std_str = '17'
        defines: Dict[str, str] = {
            'CMAKE_C_COMPILER': str(self._cc),
            'CMAKE_CXX_COMPILER': str(self._cxx),
            'CMAKE_CXX_STANDARD':  cxx_std_str,

            'CMAKE_C_COMPILER': str(self.toolchain.cc),
            'CMAKE_CXX_COMPILER': str(self.toolchain.cxx),
            'CMAKE_ADDR2LINE': str(self.toolchain.addr2line),
            'CMAKE_AR': str(self.toolchain.ar),
            'CMAKE_NM': str(self.toolchain.nm),
            'CMAKE_OBJCOPY': str(self.toolchain.objcopy),
            'CMAKE_OBJDUMP': str(self.toolchain.objdump),
            'CMAKE_RANLIB': str(self.toolchain.ranlib),
            'CMAKE_RC_COMPILER': str(self.toolchain.rc),
            'CMAKE_READELF': str(self.toolchain.readelf),
            'CMAKE_STRIP': str(self.toolchain.strip),
            'CMAKE_MT': str(self.toolchain.mt),

            'CMAKE_ASM_FLAGS':  cflags_str,
            'CMAKE_C_FLAGS': cflags_str,
            'CMAKE_CXX_FLAGS': cxxflags_str,

            'CMAKE_EXE_LINKER_FLAGS': ldflags_str,
            'CMAKE_SHARED_LINKER_FLAGS': ldflags_str,
            'CMAKE_MODULE_LINKER_FLAGS': ldflags_str,

            'CMAKE_BUILD_TYPE': 'Release',
            'CMAKE_INSTALL_PREFIX': str(self.install_dir),

            'CMAKE_MAKE_PROGRAM': str(paths.NINJA_BIN_PATH),

            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_PACKAGE': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM': 'NEVER',

            'CMAKE_POSITION_INDEPENDENT_CODE': 'ON',
        }
        linker = self._config.get_linker(self.toolchain)
        if linker:
            defines['CMAKE_LINKER'] = str(linker)
        if self._config.sysroot:
            defines['CMAKE_SYSROOT'] = str(self._config.sysroot)
        if self._config.target_os == hosts.Host.Android:
            defines['ANDROID'] = '1'
            # Inhibit all of CMake's own NDK handling code.
            defines['CMAKE_SYSTEM_VERSION'] = '1'
        if self._is_cross_compiling():
            # Cross compiling
            defines['CMAKE_SYSTEM_NAME'] = self._get_cmake_system_name()
            defines['CMAKE_SYSTEM_PROCESSOR'] = self._get_cmake_system_arch()
        defines.update(self._config.cmake_defines)
        return defines

    def _get_cmake_system_name(self) -> str:
        return self._config.target_os.value.capitalize()

    def _get_cmake_system_arch(self) -> str:
        return self._config.target_arch.value

    @staticmethod
    def _rm_cmake_cache(cache_dir: Path):
        for dirpath, dirs, files in os.walk(cache_dir):
            if 'CMakeCache.txt' in files:
                os.remove(os.path.join(dirpath, 'CMakeCache.txt'))
            if 'CMakeFiles' in dirs:
                shutil.rmtree(os.path.join(dirpath, 'CMakeFiles'))

    def _record_cmake_command(self, cmake_cmd: List[str],
                              env: Dict[str, str]) -> None:
        script_path = self.output_dir / 'cmake_invocation.sh'
        with script_path.open('w') as outf:
            for k, v in env.items():
                if v != utils.ORIG_ENV.get(k):
                    outf.write(f'{k}={v}\n')
            outf.write(utils.list2cmdline(cmake_cmd) + '\n')
        script_path.chmod(0o755)

    def _build_config(self) -> None:
        if self.remove_cmake_cache:
            self._rm_cmake_cache(self.output_dir)

        if self.remove_install_dir and self.install_dir.exists():
            shutil.rmtree(self.install_dir)

        cmake_cmd: List[str] = [str(paths.CMAKE_BIN_PATH), '-G', 'Ninja']

        cmake_cmd.extend(f'-D{key}={val}' for key, val in self.cmake_defines.items())
        cmake_cmd.append(str(self.src_dir))

        self.output_dir.mkdir(parents=True, exist_ok=True)

        env = self.env
        self._record_cmake_command(cmake_cmd, env)
        utils.check_call(cmake_cmd, cwd=self.output_dir, env=env)

        ninja_cmd: List[str] = [str(paths.NINJA_BIN_PATH)]
        ninja_cmd.extend(self.ninja_targets)
        utils.check_call(ninja_cmd, cwd=self.output_dir, env=env)

        self.install_config()

    def install_config(self) -> None:
        """Installs built artifacts for current config."""
        utils.check_call([paths.NINJA_BIN_PATH, 'install'],
                         cwd=self.output_dir, env=self.env)


class LLVMBaseBuilder(CMakeBuilder):  # pylint: disable=abstract-method
    """Base builder for both llvm and individual runtime lib."""

    enable_assertions: bool = False
    ccache: bool = False
    ccache_dir: str = None

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines

        if self.ccache:
            defines['LLVM_CCACHE_BUILD'] = 'ON'
            if self.ccache_dir is None:
                defines['LLVM_CCACHE_DIR'] = str(paths.OUT_DIR / '.ccache')
            else:
                defines['LLVM_CCACHE_DIR'] = str(Path(self.ccache_dir))
        else:
            defines['LLVM_CCACHE_BUILD'] = 'OFF'

        if self.enable_assertions:
            defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'
        else:
            defines['LLVM_ENABLE_ASSERTIONS'] = 'OFF'

        # https://github.com/android-ndk/ndk/issues/574 - Don't depend on libtinfo.
        defines['LLVM_ENABLE_TERMINFO'] = 'OFF'
        defines['LLVM_ENABLE_THREADS'] = 'ON'
        defines['LLVM_PARALLEL_COMPILE_JOBS'] = subprocess.getoutput("nproc")
        defines['LLVM_PARALLEL_LINK_JOBS'] = subprocess.getoutput("nproc")
        defines['LLVM_USE_NEWPM'] = 'ON'
        defines['LLVM_LIBDIR_SUFFIX'] = '64'
        defines['LLVM_VERSION_PATCH'] = benzo_version.patch_level
        defines['CLANG_VERSION_PATCHLEVEL'] = benzo_version.patch_level
        defines['CLANG_REPOSITORY_STRING'] = 'https://github.com/benzoClang/llvm-project'
        defines['CLANG_TC_DATE'] = datetime.datetime.now().strftime("%Y%m%d")
        defines['TOOLCHAIN_REVISION_STRING'] = benzo_version.svn_revision

        # http://b/111885871 - Disable building xray because of MacOS issues.
        defines['COMPILER_RT_BUILD_XRAY'] = 'OFF'

        # To prevent cmake from checking libstdcxx version.
        defines['LLVM_ENABLE_LIBCXX'] = 'ON'

        # Don't depend on the host libatomic library.
        defines['LIBCXX_HAS_ATOMIC_LIB'] = 'NO'

        defines['LLVM_ENABLE_LLD'] = 'ON'

        return defines


class LLVMRuntimeBuilder(LLVMBaseBuilder):  # pylint: disable=abstract-method
    """Base builder for llvm runtime libs."""

    _config: configs.AndroidConfig

    @property
    def install_dir(self) -> Path:
        arch = self._config.target_arch
        if self._config.platform:
            return self.output_toolchain.resource_dir / arch.value
        return self.output_toolchain.path / 'runtimes_ndk_cxx' / arch.value

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LLVM_CONFIG_PATH'] = str(self.toolchain.path /
                                          'bin' / 'llvm-config')
        return defines


class LLVMBuilder(LLVMBaseBuilder):
    """Builder for LLVM project."""

    src_dir: Path = paths.LLVM_PATH / 'llvm'
    config_list: List[configs.Config]
    build_tags: Optional[List[str]] = None
    clang_vendor: str
    enable_assertions: bool = False
    toolchain_name: str

    @property
    def install_dir(self) -> Path:
        return paths.OUT_DIR / f'{self.name}-install'

    @property
    def output_dir(self) -> Path:
        return paths.OUT_DIR / self.name

    @property
    def llvm_projects(self) -> Set[str]:
        """Returns enabled llvm projects."""
        raise NotImplementedError()

    @property
    def llvm_targets(self) -> Set[str]:
        """Returns llvm target archtects to build."""
        raise NotImplementedError()

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines

        defines['LLVM_ENABLE_PROJECTS'] = ';'.join(sorted(self.llvm_projects))

        defines['LLVM_TARGETS_TO_BUILD'] = ';'.join(sorted(self.llvm_targets))
        defines['LLVM_BUILD_LLVM_DYLIB'] = 'ON'

        if self.build_tags:
            tags_str = ''.join(tag + ', ' for tag in self.build_tags)
        else:
            tags_str = ''

        defines['CLANG_VENDOR'] = self.clang_vendor
        defines['LLD_VENDOR'] = self.clang_vendor
        defines['LLVM_BINUTILS_INCDIR'] = str(paths.ANDROID_DIR / 'toolchain' /
                                              'binutils' / 'include')
        defines['LLVM_BUILD_RUNTIME'] = 'ON'

        return defines

    @functools.cached_property
    def installed_toolchain(self) -> toolchains.Toolchain:
        """Gets the built Toolchain."""
        return toolchains.Toolchain(self.install_dir, self.output_dir)
