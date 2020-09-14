#!/usr/bin/env python3
#
# Copyright (C) 2016 The Android Open Source Project
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
# pylint: disable=not-callable, line-too-long, no-else-return

import argparse
import datetime
import glob
import logging
from pathlib import Path
import os
import shutil
import string
import sys
import textwrap
from typing import cast, List, Optional, Set, Tuple

import benzo_version
from base_builders import Builder, LLVMBuilder
import builders
from builder_registry import BuilderRegistry
import configs
import constants
import hosts
import paths
import toolchains
import utils
from version import Version

ORIG_ENV = dict(os.environ)

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def set_default_toolchain(toolchain: toolchains.Toolchain) -> None:
    """Sets the toolchain to use for builders who don't specify a toolchain in constructor."""
    Builder.toolchain = toolchain


def build_runtimes():
    builders.SysrootsBuilder().build()

    builders.PlatformLibcxxAbiBuilder().build()
    builders.CompilerRTBuilder().build()
    builders.CompilerRTHostI386Builder().build()
    builders.LibOMPBuilder().build()
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(toolchain, version)
    builders.AsanMapFileBuilder().build()


def install_wrappers(llvm_install_path: Path) -> None:
    wrapper_path = paths.OUT_DIR / 'llvm_android_wrapper'
    wrapper_build_script = paths.TOOLCHAIN_UTILS_DIR / 'compiler_wrapper' / 'build.py'
    # Note: The build script automatically determines the architecture
    # based on the host.
    go_env = dict(os.environ)
    go_env['PATH'] = str(paths.GO_BIN_PATH) + os.pathsep + go_env['PATH']
    utils.check_call([sys.executable, wrapper_build_script,
                      '--config=android',
                      '--use_ccache=false',
                      '--use_llvm_next=true',
                      f'--output_file={wrapper_path}'], env=go_env)

    bisect_path = paths.SCRIPTS_DIR / 'bisect_driver.py'
    bin_path = llvm_install_path / 'bin'
    clang_path = bin_path / 'clang'
    clang_real_path = bin_path / 'clang.real'
    clangxx_path = bin_path / 'clang++'
    clangxx_real_path = bin_path / 'clang++.real'
    clang_tidy_path = bin_path / 'clang-tidy'
    clang_tidy_real_path = bin_path / 'clang-tidy.real'

    # Rename clang and clang++ to clang.real and clang++.real.
    # clang and clang-tidy may already be moved by this script if we use a
    # prebuilt clang. So we only move them if clang.real and clang-tidy.real
    # doesn't exist.
    if not clang_real_path.exists():
        clang_path.rename(clang_real_path)
    clang_tidy_real_path = clang_tidy_path.parent / (clang_tidy_path.name + '.real')
    if not clang_tidy_real_path.exists():
        clang_tidy_path.rename(clang_tidy_real_path)
    clang_path.unlink(missing_ok=True)
    clangxx_path.unlink(missing_ok=True)
    clang_tidy_path.unlink(missing_ok=True)
    clangxx_real_path.unlink(missing_ok=True)
    clangxx_real_path.symlink_to('clang.real')

    shutil.copy2(wrapper_path, clang_path)
    shutil.copy2(wrapper_path, clangxx_path)
    shutil.copy2(wrapper_path, clang_tidy_path)
    shutil.copy2(bisect_path, bin_path)


# Normalize host libraries (libLLVM, libclang, libc++, libc++abi) so that there
# is just one library, whose SONAME entry matches the actual name.
def normalize_llvm_host_libs(install_dir: Path, host: hosts.Host, version: Version):
    if host.is_linux:
        libs = {'libLLVM': 'libLLVM-{version}git.so',
                'libclang': 'libclang.so.{version}git',
                'libclang_cxx': 'libclang_cxx.so.{version}git',
                'libc++': 'libc++.so.{version}',
                'libc++abi': 'libc++abi.so.{version}'
               }
    else:
        libs = {'libc++': 'libc++.{version}.dylib',
                'libc++abi': 'libc++abi.{version}.dylib'
               }

    def getVersions(libname: str) -> Tuple[str, str]:
        if not libname.startswith('libc++'):
            return version.short_version(), version.major
        else:
            return '1.0', '1'

    libdir = os.path.join(install_dir, 'lib64')
    for libname, libformat in libs.items():
        short_version, major = getVersions(libname)

        soname_lib = os.path.join(libdir, libformat.format(version=major))
        if libname.startswith('libclang'):
            real_lib = soname_lib[:-3]
        else:
            real_lib = os.path.join(libdir, libformat.format(version=short_version))

        if libname not in ('libLLVM',):
            # Rename the library to match its SONAME
            if not os.path.isfile(real_lib):
                raise RuntimeError(real_lib + ' must be a regular file')
            if not os.path.islink(soname_lib):
                raise RuntimeError(soname_lib + ' must be a symlink')

            shutil.move(real_lib, soname_lib)

        # Retain only soname_lib and delete other files for this library.  We
        # still need libc++.so or libc++.dylib symlinks for a subsequent stage1
        # build using these prebuilts (where CMake tries to find C++ atomics
        # support) to succeed.
        libcxx_name = 'libc++.so' if host.is_linux else 'libc++.dylib'
        all_libs = [lib for lib in os.listdir(libdir) if
                    lib != libcxx_name and
                    not lib.endswith('.a') and # skip static host libraries
                    (lib.startswith(libname + '.') or # so libc++abi is ignored
                     lib.startswith(libname + '-'))]

        for lib in all_libs:
            lib = os.path.join(libdir, lib)
            if lib != soname_lib:
                os.remove(lib)


def install_license_files(install_dir: Path) -> None:
    projects = (
        'llvm',
        'compiler-rt',
        'libcxx',
        'libcxxabi',
        'openmp',
        'clang',
        'clang-tools-extra',
        'lld',
        'polly',
    )

    # Fetch all the LICENSE.* files under our projects and append them into a
    # single NOTICE file for the resulting prebuilts.
    notices = []
    for project in projects:
        for license_file in (paths.LLVM_PATH / project).glob('LICENSE.*'):
            with license_file.open() as notice_file:
                notices.append(notice_file.read())
    with (install_dir / 'NOTICE').open('w') as notice_file:
        notice_file.write('\n'.join(notices))


def remove_static_libraries(static_lib_dir, necessary_libs=None):
    if not necessary_libs:
        necessary_libs = {}
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a') and lib_file not in necessary_libs:
                static_library = os.path.join(static_lib_dir, lib_file)
                os.remove(static_library)


def package_toolchain(toolchain_builder: LLVMBuilder,
                      necessary_bin_files: Optional[Set[str]]=None,
                      strip=True, create_tar=True):
    dist_dir = Path(ORIG_ENV.get('DIST_DIR', paths.OUT_DIR))
    build_dir = toolchain_builder.install_dir
    host = toolchain_builder.config_list[0].target_os
    build_name = toolchain_builder.build_name
    version = toolchain_builder.installed_toolchain.version

    package_name = 'clang-' + build_name

    install_dir = paths.get_package_install_path(host, package_name)
    install_host_dir = install_dir.parent

    # Remove any previously installed toolchain so it doesn't pollute the
    # build.
    if install_host_dir.exists():
        shutil.rmtree(install_host_dir)

    # First copy over the entire set of output objects.
    shutil.copytree(build_dir, install_dir, symlinks=True)

    # Next, we remove unnecessary binaries.
    necessary_bin_files = {
        'clang',
        'clang++',
        'clang-' + version.major_version(),
        'clang-check',
        'clang-cl',
        'clang-format',
        'clang-tidy',
        'clangd',
        'dsymutil',
        'git-clang-format',
        'ld.lld',
        'ld64.lld',
        'lld',
        'lld-link',
        'llvm-addr2line',
        'llvm-ar',
        'llvm-as',
        'llvm-cfi-verify',
        'llvm-config',
        'llvm-cov',
        'llvm-dis',
        'llvm-dwarfdump',
        'llvm-lib',
        'llvm-link',
        'llvm-modextract',
        'llvm-nm',
        'llvm-objcopy',
        'llvm-objdump',
        'llvm-profdata',
        'llvm-ranlib',
        'llvm-rc',
        'llvm-readelf',
        'llvm-readobj',
        'llvm-size',
        'llvm-strings',
        'llvm-strip',
        'llvm-symbolizer',
        'sancov',
        'sanstats',
        'scan-build',
        'scan-view',
    }

    # scripts that should not be stripped
    script_bins = {
        'git-clang-format',
        'scan-build',
        'scan-view',
    }

    bin_dir = install_dir / 'bin'
    lib_dir = install_dir / 'lib64'
    strip_cmd = Builder.toolchain.strip

    for binary in bin_dir.iterdir():
        if binary.is_file():
            if binary.name not in necessary_bin_files:
                binary.unlink()
            elif binary.is_symlink():
                continue
            elif strip and binary.name not in script_bins:
                # Strip all non-global symbols and debug info.
                # These specific flags prevent Darwin executables from being
                # stripped of additional global symbols that might be used
                # by plugins.
                utils.check_call([strip_cmd, '-S', '-x', binary])

    # FIXME: check that all libs under lib64/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if not (bin_dir / necessary_bin_file).is_file():
            raise RuntimeError(f'Did not find {necessary_bin_file} in {bin_dir}')

    necessary_lib_files = {
        'libc++.a',
        'libc++abi.a',
    }

    # Remove unnecessary static libraries.
    remove_static_libraries(lib_dir, necessary_lib_files)

    install_wrappers(install_dir)
    normalize_llvm_host_libs(install_dir, host, version)

    # Check necessary lib files exist.
    for necessary_lib_file in necessary_lib_files:
        if not (lib_dir / necessary_lib_file).is_file():
            raise RuntimeError(f'Did not find {necessary_lib_file} in {lib_dir}')

    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an AndroidVersion.txt file.
    version_file_path = install_dir / 'VERSION'
    with version_file_path.open('w') as version_file:
        svn_revision = benzo_version.svn_revision
        version_file.write(f'{version.long_version()}-{svn_revision}-benzoClang\n')

    # Package up the resulting trimmed install/ directory.
    if create_tar:
        tarball_name = package_name + '-' + host.os_tag + '.tar.bz2'
        package_path = dist_dir / tarball_name
        logger().info(f'Packaging {package_path}')
        args = ['tar', '-cjC', install_host_dir, '-f', package_path, package_name]
        utils.check_call(args)


def parse_args():
    known_components = ('linux')
    known_components_str = ', '.join(known_components)

    # Simple argparse.Action to allow comma-separated values (e.g.
    # --option=val1,val2)
    class CommaSeparatedListAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string):
            for value in values.split(','):
                if value not in known_components:
                    error = '\'{}\' invalid.  Choose from {}'.format(
                        value, known_platforms)
                    raise argparse.ArgumentError(self, error)
            setattr(namespace, self.dest, values.split(','))


    # Parses and returns command line arguments.
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=0,
        help='Increase log level. Defaults to logging.INFO.')
    parser.add_argument(
        '--build-name', default='benzo', help='Release name for the package.')

    parser.add_argument(
        '--enable-assertions',
        action='store_true',
        default=False,
        help='Enable assertions (only affects stage2)')

    parser.add_argument(
        '--no-lto',
        action='store_true',
        default=False,
        help='Disable LTO to speed up build (only affects stage2)')

    parser.add_argument(
        '--debug',
        action='store_true',
        default=False,
        help='Build debuggable Clang and LLVM tools (only affects stage2)')

    parser.add_argument(
        '--build-instrumented',
        action='store_true',
        default=False,
        help='Build LLVM tools with PGO instrumentation')

    # Options to skip build or packaging (can't skip both, or the script does
    # nothing).
    build_package_group = parser.add_mutually_exclusive_group()
    build_package_group.add_argument(
        '--skip-build',
        '-sb',
        action='store_true',
        default=False,
        help='Skip the build, and only do the packaging step')
    build_package_group.add_argument(
        '--skip-package',
        '-sp',
        action='store_true',
        default=False,
        help='Skip the packaging, and only do the build step')

    parser.add_argument(
        '--create-tar',
        action='store_true',
        default=False,
        help='Create a tar archive of the toolchains')

    parser.add_argument(
        '--no-strip',
        action='store_true',
        default=False,
        help='Don\'t strip binaries/libraries')

    build_group = parser.add_mutually_exclusive_group()
    build_group.add_argument(
        '--build',
        nargs='+',
        help='A list of builders to build. All builders not listed will be skipped.')
    build_group.add_argument(
        '--skip',
        nargs='+',
        help='A list of builders to skip. All builders not listed will be built.')

    # skip_runtimes is set to skip recompilation of libraries
    parser.add_argument(
        '--skip-runtimes',
        action='store_true',
        default=False,
        help='Skip the runtime libraries')

    parser.add_argument(
        '--no-build',
        action=CommaSeparatedListAction,
        default=list(),
        help='Don\'t build toolchain components or platforms.  Choices: ' + \
            known_components_str)

    parser.add_argument(
        '--ccache',
        action='store_true',
        default=False,
        help='Enable the use of ccache during build')
    parser.add_argument(
        '--ccache-dir',
        action='store',
        help='Use custom path for cache instead of out_dir/.ccache')

    return parser.parse_args()


def main():
    start_time = datetime.datetime.now()
    args = parse_args()
    if args.skip_build:
        # Skips all builds
        BuilderRegistry.add_filter(lambda name: False)
    elif args.skip:
        BuilderRegistry.add_skips(args.skip)
    elif args.build:
        BuilderRegistry.add_builds(args.build)
    do_runtimes = not args.skip_runtimes
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug
    do_thinlto = not args.no_lto
    do_ccache = args.ccache

    need_host = ('linux' not in args.no_build)

    log_levels = [logging.INFO, logging.DEBUG]
    verbosity = min(args.verbose, len(log_levels) - 1)
    log_level = log_levels[verbosity]
    logging.basicConfig(level=log_level)

    if not hosts.build_host().is_linux:
        raise RuntimeError('Only building on Linux is supported')

    logger().info('do_build=%r do_stage1=%r do_stage2=%r do_runtimes=%r do_package=%r do_thinlto=%r do_ccache=%r' %
                  (not args.skip_build, BuilderRegistry.should_build('stage1'), BuilderRegistry.should_build('stage2'),
                  do_runtimes, do_package, do_thinlto, do_ccache))

    # Build the stage1 Clang for the build host
    instrumented = args.build_instrumented

    stage1 = builders.Stage1Builder()
    stage1.build_name = 'stage1'
    stage1.clang_vendor = 'benzoClang'
    stage1.ccache = args.ccache
    stage1.ccache_dir = args.ccache_dir
    stage1.build_android_targets = args.debug or instrumented
    stage1.build()
    set_default_toolchain(stage1.installed_toolchain)

    if need_host:
        profdata_filename = paths.pgo_profdata_filename(False)
        profdata = paths.pgo_profdata_file(profdata_filename)

        stage2 = builders.Stage2Builder()
        stage2.build_name = args.build_name
        stage2.clang_vendor = 'benzoClang'
        stage2.ccache = args.ccache
        stage2.ccache_dir = args.ccache_dir
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = not args.no_lto
        stage2.build_instrumented = instrumented
        stage2.profdata_file = profdata if profdata else None

        stage2_tags = []
        # Annotate the version string if there is no profdata.
        if profdata is None:
            stage2_tags.append('NO PGO PROFILE')
        stage2.build_tags = stage2_tags

        stage2.build()
        if not (stage2.build_instrumented or stage2.debug_build):
            set_default_toolchain(stage2.installed_toolchain)

        Builder.output_toolchain = stage2.installed_toolchain
        if hosts.build_host().is_linux and do_runtimes:
            build_runtimes()

    if do_package and need_host:
        package_toolchain(
            stage2,
            strip=do_strip_host_package,
            create_tar=args.create_tar)

    print ('')
    print ('Build took {0} to complete.'.format(datetime.datetime.now() - start_time))

    return 0


if __name__ == '__main__':
    main()
