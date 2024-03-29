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
import re
import shutil
import subprocess
from typing import cast, Dict, List, Optional, Set, Sequence

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


class LibInfo:
    """An interface to get information of a library."""

    name: str
    _config: configs.Config

    static_lib: bool = False
    with_lib_version: bool = True

    @property
    def lib_version(self) -> str:
        target_os = self._config.target_os

        libname = self.name + '.so'
        lib = self.install_dir / 'lib' / libname

        if not lib.exists():
            raise RuntimeError('Lookup of version before library is built')

        objdump_output = utils.check_output([toolchains.get_prebuilt_toolchain().objdump,
                                             '-p', lib])

        regex = f'SONAME\\s*{self.name}.so.([0-9.]*)'
        version = re.findall(regex, objdump_output)
        if not version:
            print('objdump output is')
            print(objdump_output)
            raise RuntimeError(f'Cannot find regex pattern {regex}')
        return version[0]

    @property
    def install_dir(self) -> Path:
        raise NotImplementedError()

    @property
    def _lib_names(self) -> List[str]:
        return [self.name]

    @property
    def include_dir(self) -> Path:
        """Path to headers."""
        return self.install_dir / 'include'

    @property
    def _lib_suffix(self) -> str:
        target_os = self._config.target_os
        if self.static_lib:
            return '.a'
        if target_os.is_linux:
            return f'.so.{self.lib_version}' if self.with_lib_version else '.so'
        raise RuntimeError('Unknown target OS')

    @property
    def link_libraries(self) -> List[Path]:
        """Path to the libraries used when linking."""
        suffix = self._lib_suffix
        return list(self.install_dir / 'lib' / f'{name}{suffix}' for name in self._lib_names)

    @property
    def install_libraries(self) -> List[Path]:
        """Path to the libraries to install."""
        if self.static_lib:
            return []
        return self.link_libraries

    @property
    def install_tools(self) -> List[Path]:
        """Path to tools to install."""
        return []

    @property
    def symlinks(self) -> List[Path]:
        """List of symlinks to the library that may need to be installed."""
        return []

    def update_lib_id(self) -> None:
        """Util function to update lib paths on mac."""
        if self.static_lib:
            return
        if not self._config.target_os.is_darwin:
            return
        for lib in self.link_libraries:
            # Update LC_ID_DYLIB, so that users of the library won't link with absolute path.
            utils.check_call(['install_name_tool', '-id', f'@rpath/{lib.name}', str(lib)])
            # The lib may already reference other libs.
            for other_lib in self.link_libraries:
                utils.check_call(['install_name_tool', '-change', str(other_lib),
                                  f'@rpath/{other_lib.name}', str(lib)])


class Builder:  # pylint: disable=too-few-public-methods
    """Base builder type."""
    name: str = ""
    config_list: List[configs.Config]

    """Use prebuilt toolchain by default. This value will be updated if a new toolchain is built."""
    toolchain: toolchains.Toolchain = toolchains.get_prebuilt_toolchain()

    """The toolchain to install artifacts from this LLVMRuntimeBuilder."""
    output_toolchain: toolchains.Toolchain

    def __init__(self,
                 config_list: Optional[Sequence[configs.Config]] = None,
                 toolchain: Optional[toolchains.Toolchain] = None) -> None:
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

    def _is_64bit(self) -> bool:
        return self._config.target_arch in (hosts.Arch.AARCH64, hosts.Arch.X86_64)

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
        if not self._config.is_cross_compiling and not isinstance(self._config, configs.LinuxMuslConfig):
            # at least swig and libncurses need to link with lib/libc++.so
            for lib_dir in self.toolchain.lib_dirs:
                ldflags.append(f'-L{lib_dir}')
        return ldflags

    @property
    def env(self) -> Dict[str, str]:
        """Environment variables used when building."""
        env = dict(utils.ORIG_ENV)
        env.update(self._config.env)
        path_env = [
            self._config.env.get('PATH'),
            str(paths.get_python_dir(hosts.build_host()) / 'bin'),
            utils.ORIG_ENV.get('PATH')
        ]
        env['PATH'] = os.pathsep.join(p for p in path_env if p)
        return env

    @property
    def resource_dir(self) -> Path:
        return self.toolchain.clang_lib_dir / 'lib' / self._config.target_os.crt_dir

    @property
    def output_resource_dir(self) -> Path:
        return self.output_toolchain.clang_lib_dir / 'lib' / self._config.target_os.crt_dir

    def install(self) -> None:
        """Installs built artifacts."""


class AutoconfBuilder(Builder):
    """Builder for autoconf targets."""
    src_dir: Path
    remove_install_dir: bool = True

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
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-fPIC')
        cflags.append('-Wno-unused-command-line-argument')
        if self._config.sysroot:
            cflags.append(f'--sysroot={self._config.sysroot}')
        return cflags

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags
        cxxflags.append('-stdlib=libc++')
        return cxxflags

    @property
    def config_flags(self) -> List[str]:
        """Parameters to configure."""
        return []

    def _touch_src_dir(self, files) -> None:
        for file in files:
            file_path = self.src_dir / file
            if file_path.is_file():
                file_path.touch(exist_ok=True)

    def _touch_autoconfig_files(self) -> None:
        """Touches configure files to prevent autoreconf."""
        files_to_touch = ["aclocal.m4", "configure", "Makefile.am"]
        self._touch_src_dir(files_to_touch)
        self._touch_src_dir(self.src_dir.glob('**/*.in'))

    def _build_config(self) -> None:
        logger().info('Building %s for %s', self.name, self._config)

        if self.remove_install_dir and self.install_dir.exists():
            shutil.rmtree(self.install_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._touch_autoconfig_files()

        # Write flags to files, to avoid various escaping issues.
        cflags = self._config.cflags + self.cflags
        cxxflags = self._config.cxxflags + self.cxxflags
        ldflags = self._config.ldflags + self.ldflags
        cflags_file = self.output_dir / 'cflags'
        cxxflags_file = self.output_dir / 'cxxflags'
        with cflags_file.open('w') as argfile:
            argfile.write(' '.join(cflags + ldflags))
        with cxxflags_file.open('w') as argfile:
            argfile.write(' '.join(cxxflags + ldflags))

        env = self.env
        # Append CFLAGS after CC since autoconf pre-checks does not use CFLAGS, and we can't pass
        # it without providing -isystem flags.
        env['CC'] = f'{self._cc} @{cflags_file}'
        env['CXX'] = f'{self._cxx} @{cxxflags_file}'

        config_cmd = [str(self.src_dir / 'configure'), f'--prefix={self.install_dir}']
        config_cmd.extend(self.config_flags)
        utils.create_script(self.output_dir / 'config_invocation.sh', config_cmd, env)
        utils.check_call(config_cmd, cwd=self.output_dir, env=env)

        make_cmd = [str(paths.MAKE_BIN_PATH), f'-j{multiprocessing.cpu_count()}']
        utils.check_call(make_cmd, cwd=self.output_dir, env=self.env)

        self.install_config()

    def install_config(self) -> None:
        """Installs built artifacts for current config."""
        install_cmd = [str(paths.MAKE_BIN_PATH), 'install']
        utils.check_call(install_cmd, cwd=self.output_dir, env=self.env)
        if isinstance(self, LibInfo):
            cast(LibInfo, self).update_lib_id()


class CMakeBuilder(Builder):
    """Builder for cmake targets."""
    config: configs.Config
    src_dir: Path
    remove_cmake_cache: bool = False
    remove_install_dir: bool = False
    ninja_targets: List[str] = []
    no_unused_cflag: str = " -Wno-unused-command-line-argument"

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
        if self._config.sysroot:
            cflags.append(f'--sysroot={self._config.sysroot}')
            cxxflags.append(f'--sysroot={self._config.sysroot}')
            ldflags.append(f'--sysroot={self._config.sysroot}')
        cflags_str = ' '.join(cflags)
        cxxflags_str = ' '.join(cxxflags)
        ldflags_str = ' '.join(ldflags)
        defines: Dict[str, str] = {
            'CMAKE_C_COMPILER': str(self._cc),
            'CMAKE_CXX_COMPILER': str(self._cxx),

            'CMAKE_ADDR2LINE': str(self.toolchain.addr2line),
            'CMAKE_AR': str(self.toolchain.ar),
            'CMAKE_LIPO': str(self.toolchain.lipo),
            'CMAKE_NM': str(self.toolchain.nm),
            'CMAKE_OBJCOPY': str(self.toolchain.objcopy),
            'CMAKE_OBJDUMP': str(self.toolchain.objdump),
            'CMAKE_RANLIB': str(self.toolchain.ranlib),
            'CMAKE_RC_COMPILER': str(self.toolchain.rc),
            'CMAKE_READELF': str(self.toolchain.readelf),
            'CMAKE_STRIP': str(self.toolchain.strip),
            'CMAKE_MT': str(self.toolchain.mt),

            'CMAKE_ASM_FLAGS':  cflags_str + self.no_unused_cflag,
            'CMAKE_C_FLAGS': cflags_str + self.no_unused_cflag,
            'CMAKE_CXX_FLAGS': cxxflags_str + self.no_unused_cflag,

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

            'GO_EXECUTABLE': str(paths.GO_BIN_PATH / 'go'),
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
        if self._config.is_cross_compiling:
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

    def _ninja(self, args: list[str]) -> None:
        ninja_cmd = [str(paths.NINJA_BIN_PATH)] + args
        utils.check_call(ninja_cmd, cwd=self.output_dir, env=self.env)

    def _build_config(self) -> None:
        if self.remove_cmake_cache:
            self._rm_cmake_cache(self.output_dir)

        if self.remove_install_dir and self.install_dir.exists():
            shutil.rmtree(self.install_dir)

        cmake_cmd: List[str] = [str(paths.CMAKE_BIN_PATH), '-G', 'Ninja', '-Wno-dev']

        cmake_cmd.extend(f'-D{key}={val}' for key, val in self.cmake_defines.items())
        cmake_cmd.append(str(self.src_dir))

        self.output_dir.mkdir(parents=True, exist_ok=True)

        env = self.env
        utils.create_script(self.output_dir / 'cmake_invocation.sh', cmake_cmd, env)
        utils.check_call(cmake_cmd, cwd=self.output_dir, env=env)

        self._ninja(self.ninja_targets)
        self.install_config()

    def install_config(self) -> None:
        """Installs built artifacts for current config."""
        utils.check_call([paths.NINJA_BIN_PATH, 'install'],
                         cwd=self.output_dir, env=self.env)


class LLVMBaseBuilder(CMakeBuilder):  # pylint: disable=abstract-method
    """Base builder for both llvm and individual runtime lib."""

    enable_assertions: bool = False
    num_jobs: int = None
    num_link_jobs: int = None

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines

        if self.enable_assertions:
            defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'
        else:
            defines['LLVM_ENABLE_ASSERTIONS'] = 'OFF'

        if self.num_jobs is None:
            defines['LLVM_PARALLEL_COMPILE_JOBS'] = subprocess.getoutput("nproc")
        else:
            defines['LLVM_PARALLEL_COMPILE_JOBS'] = self.num_jobs

        # https://github.com/android-ndk/ndk/issues/574 - Don't depend on libtinfo.
        defines['LLVM_ENABLE_TERMINFO'] = 'OFF'
        defines['LLVM_ENABLE_THREADS'] = 'ON'
        if patch_level := benzo_version.get_patch_level():
            defines['LLVM_VERSION_PATCH'] = patch_level
        defines['LLVM_VERSION_SUFFIX'] = ""
        defines['PACKAGE_VENDOR'] = 'benzoClang'
        defines['PACKAGE_REPOSITORY'] = 'https://github.com/benzoClang/llvm-project'
        defines['PACKAGE_REVISION'] = benzo_version.get_svn_revision()

        # http://b/111885871 - Disable building xray because of MacOS issues.
        defines['COMPILER_RT_BUILD_XRAY'] = 'OFF'

        # To prevent cmake from checking libstdcxx version.
        defines['LLVM_ENABLE_LIBCXX'] = 'ON'

        defines['LLVM_ENABLE_LLD'] = 'ON'

        # Disable benchmarks and examples
        defines['LLVM_INCLUDE_BENCHMARKS'] = 'OFF'
        defines['LLVM_INCLUDE_EXAMPLES'] = 'OFF'

        # Use Python for any host build (not Android targets, however)
        target = self._config.target_os
        if target != hosts.Host.Android and target != hosts.Host.Baremetal:
            defines['Python3_LIBRARY'] = str(paths.get_python_lib(target))
            defines['Python3_LIBRARIES'] = str(paths.get_python_lib(target))
            defines['Python3_INCLUDE_DIR'] = str(paths.get_python_include_dir(target))
            defines['Python3_INCLUDE_DIRS'] = str(paths.get_python_include_dir(target))
        defines['Python3_EXECUTABLE'] = str(paths.get_python_executable(hosts.build_host()))

        return defines


class LLVMRuntimeBuilder(LLVMBaseBuilder):  # pylint: disable=abstract-method
    """Base builder for llvm runtime libs."""

    _config: configs.AndroidConfig

    @property
    def install_dir(self) -> Path:
        arch = self._config.target_arch
        if self._config.target_os.is_android and not self._config.platform:
            return self.output_toolchain.path / 'runtimes_ndk_cxx' / arch.value
        return self.output_resource_dir / arch.value

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LLVM_CONFIG_PATH'] = str(self.toolchain.path /
                                          'bin' / 'llvm-config')
        if self._config.target_os.is_android:
            # ANDROID_PLATFORM_LEVEL is checked when enabling TSAN for Android.
            # It's usually set by the NDK's CMake toolchain file, which we don't
            # use.
            defines['ANDROID_PLATFORM_LEVEL'] = self._config.api_level
        return defines


class LLVMBuilder(LLVMBaseBuilder):
    """Builder for LLVM project."""

    src_dir: Path = paths.LLVM_PATH / 'llvm'
    config_list: List[configs.Config]
    build_tags: Optional[List[str]] = None
    svn_revision: str
    enable_assertions: bool = False
    toolchain_name: str
    num_jobs: int = None
    num_link_jobs: int = None
    libzstd: Optional[LibInfo] = None
    # not a singleton because we'd build the 32-bit runtime in the future.
    runtimes_triples: Set[str] = set()

    # lldb options.
    build_lldb: bool = True
    swig_executable: Optional[Path] = None
    libxml2: Optional[LibInfo] = None
    liblzma: Optional[LibInfo] = None
    libedit: Optional[LibInfo] = None
    libncurses: Optional[LibInfo] = None

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
    def llvm_runtime_projects(self) -> Set[str]:
        """Returns enabled llvm runtimes."""
        raise NotImplementedError()

    @property
    def llvm_targets(self) -> Set[str]:
        """Returns llvm target archtects to build."""
        raise NotImplementedError()

    def _set_lldb_flags(self, target: hosts.Host, defines: Dict[str, str]) -> None:
        """Sets cmake defines for lldb."""
        defines['LLDB_ENABLE_LUA'] = 'OFF'

        if self.swig_executable:
            defines['SWIG_EXECUTABLE'] = str(self.swig_executable)
            defines['LLDB_ENABLE_PYTHON'] = 'ON'
            defines['LLDB_EMBED_PYTHON_HOME'] = 'OFF'
        else:
            defines['LLDB_ENABLE_PYTHON'] = 'OFF'

        if self.liblzma:
            defines['LLDB_ENABLE_LZMA'] = 'ON'
            defines['LIBLZMA_INCLUDE_DIR'] = str(self.liblzma.include_dir)
            defines['LIBLZMA_LIBRARY'] = str(self.liblzma.link_libraries[0])
        else:
            defines['LLDB_ENABLE_LZMA'] = 'OFF'

        if self.libedit:
            defines['LLDB_ENABLE_LIBEDIT'] = 'ON'
            defines['LibEdit_INCLUDE_DIRS'] = str(self.libedit.include_dir)
            defines['LibEdit_LIBRARIES'] = str(self.libedit.link_libraries[0])
        else:
            defines['LLDB_ENABLE_LIBEDIT'] = 'OFF'

        if self.libxml2:
            defines['LLDB_ENABLE_LIBXML2'] = 'ON'
        else:
            defines['LLDB_ENABLE_LIBXML2'] = 'OFF'

        if self.libncurses:
            defines['LLDB_ENABLE_CURSES'] = 'ON'
            defines['CURSES_INCLUDE_DIRS'] = ';'.join([
                str(self.libncurses.include_dir),
                str(self.libncurses.include_dir / 'ncurses'),
            ])
            curses_libs = ';'.join(str(lib) for lib in self.libncurses.link_libraries)
            defines['CURSES_LIBRARIES'] = curses_libs
            defines['PANEL_LIBRARIES'] = curses_libs
        else:
            defines['LLDB_ENABLE_CURSES'] = 'OFF'

        if self.libzstd:
            defines['LLVM_ENABLE_ZSTD'] = 'FORCE_ON'
            defines['LLVM_USE_STATIC_ZSTD'] = 'TRUE'
            defines['zstd_LIBRARY'] = self.libzstd.link_libraries[0]
            defines['zstd_STATIC_LIBRARY'] = self.libzstd.link_libraries[1]
            defines['zstd_INCLUDE_DIR'] = self.libzstd.include_dir
        else:
            defines['LLVM_ENABLE_ZSTD'] = 'OFF'

        defines['LLDB_INCLUDE_TESTS'] = 'OFF'

    def _install_lib_deps(self, lib_dir, bin_dir=None) -> None:
        for lib in (self.liblzma, self.libedit, self.libxml2, self.libncurses):
            if lib:
                for lib_file in lib.install_libraries:
                    shutil.copy2(lib_file, lib_dir)
                for link in lib.symlinks:
                    # cannot copy to an existing symlink pointing to the source file
                    dest_file = lib_dir / link.name
                    dest_file.unlink(missing_ok=True)
                    shutil.copy2(link, dest_file, follow_symlinks=False)
                if bin_dir:
                    for tool in lib.install_tools:
                        shutil.copy2(tool, bin_dir)

        if isinstance(self._config, configs.LinuxMuslConfig):
            shutil.copy2(self._config.sysroot / 'lib' / 'libc_musl.so', lib_dir / 'libc_musl.so')

    def _setup_install_dir(self) -> None:
        if self.swig_executable:
            python_prebuilt_dir = paths.get_python_dir(self._config.target_os)
            python_dest_dir = self.install_dir / 'python3'
            shutil.copytree(python_prebuilt_dir, python_dest_dir, symlinks=True, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('*.pyc', '__pycache__', 'Android.bp',
                                                          '.git', '.gitignore'))

        lib_dir = self.install_dir / 'lib'
        lib_dir.mkdir(exist_ok=True, parents=True)
        self._install_lib_deps(lib_dir)

    def _setup_build_dir(self) -> None:
        if self._config.target_os.is_linux:
            # Install dependent libs and tools to self.output_dir.  Just-built
            # tools like clang and lld need libc_musl and libxml2 in their
            # RPATH.  Tool deps (e.g. xmllint, that are needed for running
            # tests) are installed by passing a bin_dir parameter to
            # _install_lib_deps.
            lib_dir = self.output_dir / 'lib'
            bin_dir = self.output_dir / 'bin'
            lib_dir.mkdir(exist_ok=True, parents=True)
            bin_dir.mkdir(exist_ok=True, parents=True)

            self._install_lib_deps(lib_dir, bin_dir)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines

        defines['LLVM_ENABLE_PROJECTS'] = ';'.join(sorted(self.llvm_projects))
        defines['LLVM_ENABLE_RUNTIMES'] = ';'.join(sorted(self.llvm_runtime_projects))

        defines['LLVM_TARGETS_TO_BUILD'] = ';'.join(sorted(self.llvm_targets))
        defines['LLVM_BUILD_LLVM_DYLIB'] = 'ON'

        if self.build_tags:
            tags_str = ''.join(tag + ', ' for tag in self.build_tags)
        else:
            tags_str = ''

        defines['LLVM_BUILD_RUNTIME'] = 'ON'

        defines['LLVM_INCLUDE_GO_TESTS'] = 'OFF'

        # Don't build OCaml bindings
        defines['LLVM_ENABLE_BINDINGS'] = 'OFF'

        # libxml2 is used by lld and lldb.
        if self.libxml2:
            defines['LIBXML2_INCLUDE_DIR'] = str(self.libxml2.include_dir)
            defines['LIBXML2_LIBRARY'] = str(self.libxml2.link_libraries[0])

        if self.build_lldb:
            self._set_lldb_flags(self._config.target_os, defines)

        defines['CLANG_DEFAULT_LINKER'] = 'lld'
        defines['CLANG_DEFAULT_OBJCOPY'] = 'llvm-objcopy'

        if self._config.target_os.is_linux:
            # We need to explicitly propagate some CMake flags to the runtimes
            # CMake invocation that builds compiler-rt, libcxx, and other
            # runtimes for the host.
            triple = self._config.llvm_triple
            runtimes_passthrough_args = [
                    'CMAKE_C_FLAGS',
                    'CMAKE_CXX_FLAGS',
                    'CMAKE_SHARED_LINKER_FLAGS',
                    'CMAKE_EXE_LINKER_FLAGS',
                    'CMAKE_MODULE_LINKER_FLAGS',
                    'LLVM_ENABLE_LIBCXX',
            ]

            self.runtimes_triples.add(triple)
            defines['LLVM_RUNTIME_TARGETS'] = triple
            for arg in runtimes_passthrough_args:
                defines[f'RUNTIMES_{triple}_{arg}'] = defines[arg]

            # Don't depend on the host libatomic library.
            defines[f'RUNTIMES_{triple}_LIBCXX_HAS_ATOMIC_LIB'] = 'NO'

            # Make libc++.so a symlink to libc++.so.x instead of a linker script that
            # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
            # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
            defines[f'RUNTIMES_{triple}_LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
            defines[f'RUNTIMES_{triple}_LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

            # Set LIBCXX variables for compiler and linker flags for tests.
            defines[f'RUNTIMES_{triple}_LIBCXX_TEST_COMPILER_FLAGS'] = defines['CMAKE_CXX_FLAGS']
            defines[f'RUNTIMES_{triple}_LIBCXX_TEST_LINKER_FLAGS'] = defines['CMAKE_EXE_LINKER_FLAGS']

            # Don't let libclang_rt.*_cxx.a depend on libc++abi.
            defines[f'RUNTIMES_{triple}_SANITIZER_ALLOW_CXXABI'] = 'OFF'

        return defines

    def _build_config(self) -> None:
        # LLVM build invokes the just-built tools as part of subsequent steps.
        # We need to setup the build dir (copy libc_musl, libxml2 etc.) before
        # the build starts so these libraries are in the RPATH for these tools.
        self._setup_build_dir()
        super()._build_config()

    def install_config(self) -> None:
        super().install_config()
        self._setup_install_dir()

    @functools.cached_property
    def installed_toolchain(self) -> toolchains.Toolchain:
        """Gets the built Toolchain."""
        return toolchains.Toolchain(self.install_dir, self.output_dir)

    def test(self) -> None:
        # newer test tools like dexp, clang-query, c-index-test
        # need libedit.so.*, libxml2.so.*, etc. in stage2/lib.
        self._install_lib_deps(self.output_dir / 'lib')
        self._ninja(
            ['check-clang', 'check-llvm', 'check-clang-tools'] +
            ['check-cxx-' + triple for triple in sorted(self.runtimes_triples)])
        # Known failed tests:
        #   Clang :: CodeGenCXX/builtins.cpp
        #   Clang :: CodeGenCXX/unknown-anytype.cpp
        #   Clang :: Sema/builtin-setjmp.c
        #   LLVM :: Bindings/Go/go.test (disabled by LLVM_INCLUDE_GO_TESTS=OFF)
        #   LLVM :: CodeGen/X86/extractelement-fp.ll
        #   LLVM :: CodeGen/X86/fp-round.ll
