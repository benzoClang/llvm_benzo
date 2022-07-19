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
import logging
from pathlib import Path
import os
import shutil
import sys
import textwrap
from typing import List, NamedTuple, Optional, Set, Tuple

import benzo_version
from base_builders import Builder, LLVMBuilder
import builders
from builder_registry import BuilderRegistry
import configs
import hosts
import paths
import toolchains
import utils
from version import Version

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def set_default_toolchain(toolchain: toolchains.Toolchain) -> None:
    """Sets the toolchain to use for builders who don't specify a toolchain in constructor."""
    Builder.toolchain = toolchain


class Profile(NamedTuple):
    """ Optimization profiles including PGO and BOLT. """
    PgoProfile: Optional[Path]
    ClangBoltProfile: Optional[Path]


def extract_profiles() -> Profile:
    pgo_profdata_tar = paths.pgo_profdata_tar()
    if not pgo_profdata_tar:
        return Profile(None, None)
    utils.check_call(['tar', '-jxC', str(paths.OUT_DIR), '-f', str(pgo_profdata_tar)])
    profdata_file = paths.OUT_DIR / paths.pgo_profdata_filename()
    if not profdata_file.exists():
        logger().info('PGO profdata missing')
        return Profile(None, None)

    bolt_fdata_tar = paths.bolt_fdata_tar()
    if not bolt_fdata_tar:
        return Profile(profdata_file, None)
    utils.check_call(['tar', '-jxC', str(paths.OUT_DIR), '-f', str(bolt_fdata_tar)])
    clang_bolt_fdata_file = paths.OUT_DIR / 'clang.fdata'
    if not clang_bolt_fdata_file.exists():
        logger().info('Clang BOLT profile missing')
        return Profile(profdata_file, None)

    return Profile(profdata_file, clang_bolt_fdata_file)


def build_runtimes(build_lldb_server: bool):
    builders.DeviceSysrootsBuilder().build()
    builders.BuiltinsBuilder().build()
    builders.LibUnwindBuilder().build()
    builders.PlatformLibcxxAbiBuilder().build()
    builders.CompilerRTBuilder().build()
    builders.TsanBuilder().build()
    builders.CompilerRTHostI386Builder().build()
    builders.MuslHostRuntimeBuilder().build()
    builders.LibOMPBuilder().build()
    if build_lldb_server:
        builders.LldbServerBuilder().build()
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(toolchain, version)
    builders.SanitizerMapFileBuilder().build()


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
    clang_tidy_sh_path = paths.SCRIPTS_DIR / 'clang-tidy.sh'
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
    shutil.copy2(clang_tidy_sh_path, bin_path)

    # point clang-cl to clang.real instead of clang (which is the wrapper)
    clangcl_path = bin_path / 'clang-cl'
    clangcl_path.unlink()
    clangcl_path.symlink_to('clang.real')


# Normalize host libraries (libLLVM, libclang, libc++, libc++abi) so that there
# is just one library, whose SONAME entry matches the actual name.
def normalize_llvm_host_libs(install_dir: Path, host: hosts.Host, version: Version) -> None:
    if host.is_linux:
        libs = {'libLLVM': 'libLLVM-{version}git.so',
                'libclang': 'libclang.so.{version}git',
                'libclang-cpp': 'libclang-cpp.so.{version}git',
                'libc++': 'libc++.so.{version}',
                'libc++abi': 'libc++abi.so.{version}'
               }
    else:
        libs = {'libc++': 'libc++.{version}.dylib',
                'libc++abi': 'libc++abi.{version}.dylib'
               }

    def getVersions(libname: str) -> Tuple[str, str]:
        if libname == 'libclang-cpp':
            return version.major, version.major
        if not libname.startswith('libc++'):
            return version.long_version(), version.major
        else:
            return '1.0', '1'

    libdir = os.path.join(install_dir, 'lib64')
    for libname, libformat in libs.items():
        short_version, major = getVersions(libname)

        soname_version = '13' if libname == 'libclang' else major
        soname_lib = os.path.join(libdir, libformat.format(version=soname_version))
        if libname.startswith('libclang') and libname != 'libclang-cpp':
            soname_lib = soname_lib[:-3]
        real_lib = os.path.join(libdir, libformat.format(version=short_version))

        preserved_libnames = ('libLLVM', 'libclang-cpp')
        if libname not in preserved_libnames:
            # Rename the library to match its SONAME
            if not os.path.isfile(real_lib):
                raise RuntimeError(real_lib + ' must be a regular file')
            if not os.path.islink(soname_lib):
                raise RuntimeError(soname_lib + ' must be a symlink')

            shutil.move(real_lib, soname_lib)

        # Retain only soname_lib and delete other files for this library.  We
        # still need libc++.so or libc++.dylib symlinks for a subsequent stage1
        # build using these prebuilts (where CMake tries to find C++ atomics
        # support) to succeed.  We also need a few checks to ensure libclang-cpp
        # is not deleted when cleaning up libclang.so* and libc++abi is not
        # deleted when cleaning up libc++.so*.
        libcxx_name = 'libc++.so' if host.is_linux else 'libc++.dylib'
        all_libs = [lib for lib in os.listdir(libdir) if
                    lib != libcxx_name and
                    not lib.startswith('libclang-cpp') and # retain libclang-cpp
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


def bolt_optimize(toolchain_builder: LLVMBuilder, clang_fdata: Path):
    """ Optimize using llvm-bolt. """
    major_version = toolchain_builder.installed_toolchain.version.major_version()
    bin_dir = toolchain_builder.install_dir / 'bin'
    llvm_bolt_bin = bin_dir / 'llvm-bolt'

    clang_bin = bin_dir / ('clang-' + major_version)
    clang_bin_orig = bin_dir / ('clang-' + major_version + '.orig')
    shutil.move(clang_bin, clang_bin_orig)
    args = [
        llvm_bolt_bin, '-data=' + str(clang_fdata), '-o', clang_bin,
        '-reorder-blocks=cache+', '-reorder-functions=hfsort+',
        '-split-functions=3', '-split-all-cold', '-split-eh', '-dyno-stats',
        '-icf=1', '--use-gnu-stack', clang_bin_orig
    ]
    utils.check_call(args)


def bolt_instrument(toolchain_builder: LLVMBuilder):
    """ Instrument binary using llvm-bolt """
    major_version = toolchain_builder.installed_toolchain.version.major_version()
    bin_dir = toolchain_builder.install_dir / 'bin'
    llvm_bolt_bin = bin_dir / 'llvm-bolt'

    clang_bin = bin_dir / ('clang-' + major_version)
    clang_bin_orig = bin_dir / ('clang-' + major_version + '.orig')
    clang_afdo_path = paths.OUT_DIR / 'bolt_collection' / 'clang' / 'clang'
    shutil.move(clang_bin, clang_bin_orig)
    args = [
        llvm_bolt_bin, '-instrument', '--instrumentation-file=' + str(clang_afdo_path),
        '--instrumentation-file-append-pid', '-o', clang_bin,
        clang_bin_orig
    ]
    utils.check_call(args)

    # Need to create the profile output directory for BOLT.
    # TODO: Let BOLT instrumented library to create it on itself.
    os.makedirs(clang_afdo_path, exist_ok=True)


def package_toolchain(toolchain_builder: LLVMBuilder,
                      necessary_bin_files: Optional[Set[str]]=None,
                      strip=True, create_tar=True):
    dist_dir = Path(utils.ORIG_ENV.get('DIST_DIR', paths.OUT_DIR))
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
        'llvm-bolt',
        'llvm-cfi-verify',
        'llvm-config',
        'llvm-cov',
        'llvm-cxxfilt',
        'llvm-dis',
        'llvm-dwarfdump',
        'llvm-dwp',
        'llvm-ifs',
        'llvm-lib',
        'llvm-link',
        'llvm-lipo',
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
        'merge-fdata',
        'sancov',
        'sanstats',
        'scan-build',
        'scan-view',
    }

    if toolchain_builder.build_lldb:
        necessary_bin_files.update({
            'lldb-argdumper',
            'lldb',
            'lldb.sh',
        })

    # scripts that should not be stripped
    script_bins = {
        'git-clang-format',
        'lldb.sh',
        # merge-fdata is built with relocation, strip -S would fail. Treat it as
        # a script and do not strip as a workaround.
        'merge-fdata',
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
                    utils.check_call([strip_cmd, binary])

    # FIXME: check that all libs under lib64/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if not (bin_dir / necessary_bin_file).is_file():
            raise RuntimeError(f'Did not find {necessary_bin_file} in {bin_dir}')

    necessary_lib_files = set()
    necessary_lib_files |= {
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

    # Next, we copy over stdatomic.h and bits/stdatomic.h from bionic.
    libc_include_path = paths.ANDROID_DIR / 'bionic' / 'libc' / 'include'
    header_path = lib_dir / 'clang' / version.long_version() / 'include'

    shutil.copy2(libc_include_path / 'stdatomic.h', header_path)

    bits_install_path = header_path / 'bits'
    bits_install_path.mkdir(parents=True, exist_ok=True)
    bits_stdatomic_path = libc_include_path / 'bits' / 'stdatomic.h'
    shutil.copy2(bits_stdatomic_path, bits_install_path)

    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an AndroidVersion.txt file.
    version_file_path = install_dir / 'VERSION'
    with version_file_path.open('w') as version_file:
        svn_revision = benzo_version.get_svn_revision()
        version_file.write(f'{version.long_version()}-{svn_revision}-benzoClang\n')

    # Remove optrecord.py to avoid auto-filed bugs about call to yaml.load_all
    os.remove(install_dir / 'share/opt-viewer/optrecord.py')

    # Add BUILD.bazel file.
    with (install_dir / 'BUILD.bazel').open('w') as bazel_file:
        bazel_file.write(
            textwrap.dedent("""\
                package(default_visibility = ["//visibility:public"])

                filegroup(
                    name = "binaries",
                    srcs = glob([
                        "bin/*",
                        "lib64/*",
                    ]),
                )"""))

    # Package up the resulting trimmed install/ directory.
    if create_tar:
        tarball_name = package_name + '-' + host.os_tag + '.tar.bz2'
        package_path = dist_dir / tarball_name
        logger().info(f'Packaging {package_path}')
        args = ['tar', '-cjC', install_host_dir, '-f', package_path, package_name]
        utils.check_call(args)


def parse_args():
    known_components = ('linux', 'lldb')
    known_components_str = ', '.join(known_components)

    # Simple argparse.Action to allow comma-separated values (e.g.
    # --option=val1,val2)
    class CommaSeparatedListAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string):
            for value in values.split(','):
                if value not in known_components:
                    error = '\'{}\' invalid.  Choose from {}'.format(
                        value, known_components)
                    raise argparse.ArgumentError(self, error)
            setattr(namespace, self.dest, values.split(','))


    # Parses and returns command line arguments.
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--build-name', default='benzo', help='Release name for the package.')

    parser.add_argument(
        '--jobs',
        '-j',
        type=int,
        action='store',
        help='Number of threads to use. Default: nproc will give number of available processing units.')

    parser.add_argument(
        '--link-jobs',
        type=int,
        action='store',
        help='Number of (stage2) Thin-LTO link jobs to run simultaneously. Default: 8.')

    parser.add_argument(
        '--enable-assertions',
        action='store_true',
        default=False,
        help='Enable assertions (only affects stage2)')

    lto_group = parser.add_mutually_exclusive_group()
    lto_group.add_argument(
        '--lto',
        action='store_true',
        default=False,
        help='Enable LTO (only affects stage2).  This option increases build time.')
    lto_group.add_argument(
        '--no-lto',
        action='store_false',
        default=False,
        dest='lto',
        help='Disable LTO to speed up build (only affects stage2)')

    bolt_group = parser.add_mutually_exclusive_group()
    bolt_group.add_argument(
        '--bolt',
        action='store_true',
        default=False,
        help='Enable BOLT optimization (only affects stage2).  This option increases build time.')
    bolt_group.add_argument(
        '--no-bolt',
        action='store_false',
        default=False,
        dest='bolt',
        help='Disable BOLT optimization to speed up build (only affects stage2)')
    bolt_group.add_argument(
        '--bolt-instrument',
        action='store_true',
        default=False,
        help='Enable BOLT instrumentation (only affects stage2).')

    pgo_group = parser.add_mutually_exclusive_group()
    pgo_group.add_argument(
        '--pgo',
        action='store_true',
        default=False,
        help='Enable PGO (only affects stage2)')
    pgo_group.add_argument(
        '--no-pgo',
        action='store_false',
        default=False,
        dest='pgo',
        help='Disable PGO (only affects stage2)')

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
    do_bolt = args.bolt and not args.debug and not args.build_instrumented
    do_bolt_instrument = args.bolt_instrument and not args.debug and not args.build_instrumented
    do_runtimes = not args.skip_runtimes
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug
    build_lldb = 'lldb' not in args.no_build

    host_configs = [configs.host_config()]

    need_host = ('linux' not in args.no_build)

    logging.basicConfig(level=logging.DEBUG)
    if not hosts.build_host().is_linux:
        raise RuntimeError('Only building on Linux is supported')

    logger().info('do_build=%r do_stage1=%r do_stage2=%r do_runtimes=%r do_package=%r lto=%r bolt=%r' %
                  (not args.skip_build, BuilderRegistry.should_build('stage1'), BuilderRegistry.should_build('stage2'),
                  do_runtimes, do_package, args.lto, args.bolt))

    # Build the stage1 Clang for the build host
    instrumented = args.build_instrumented

    stage1 = builders.Stage1Builder(host_configs)
    stage1.build_name = 'stage1'
    stage1.svn_revision = benzo_version.get_svn_revision()
    stage1.clang_vendor = 'benzoClang'
    # Build lldb for lldb-tblgen. It will be used to build lldb-server.
    stage1.build_lldb = build_lldb
    stage1.build_android_targets = args.debug or instrumented
    stage1.num_jobs = args.jobs
    stage1.build()
    set_default_toolchain(stage1.installed_toolchain)

    if build_lldb:
        # Swig is needed for host lldb.
        swig_builder = builders.SwigBuilder(host_configs)
        swig_builder.build()
    else:
        swig_builder = None

    if need_host:
        if args.pgo:
            profdata, clang_bolt_fdata = extract_profiles()
        else:
            profdata, clang_bolt_fdata = None, None

        stage2 = builders.Stage2Builder(host_configs)
        stage2.build_name = args.build_name
        stage2.svn_revision = benzo_version.get_svn_revision()
        stage2.clang_vendor = 'benzoClang'
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = args.lto
        stage2.build_instrumented = instrumented
        stage2.bolt_optimize = args.bolt
        stage2.bolt_instrument = args.bolt_instrument
        stage2.num_jobs = args.jobs
        stage2.num_link_jobs = args.link_jobs
        stage2.profdata_file = profdata if profdata else None

        libxml2_builder = builders.LibXml2Builder(host_configs)
        libxml2_builder.build()
        stage2.libxml2 = libxml2_builder

        stage2.build_lldb = build_lldb
        if build_lldb:
            stage2.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

            xz_builder = builders.XzBuilder(host_configs)
            xz_builder.build()
            stage2.liblzma = xz_builder

            libncurses = builders.LibNcursesBuilder(host_configs)
            libncurses.build()
            stage2.libncurses = libncurses

            libedit_builder = builders.LibEditBuilder(host_configs)
            libedit_builder.libncurses = libncurses
            libedit_builder.build()
            stage2.libedit = libedit_builder

        stage2_tags = []
        # Annotate the version string if there is no profdata.
        if profdata is None:
            stage2_tags.append('NO PGO PROFILE')
        if clang_bolt_fdata is None:
            stage2_tags.append('NO BOLT PROFILE')
        stage2.build_tags = stage2_tags

        stage2.build()

        if do_bolt and clang_bolt_fdata is not None:
            bolt_optimize(stage2, clang_bolt_fdata)

        if not (stage2.build_instrumented or stage2.debug_build):
            set_default_toolchain(stage2.installed_toolchain)

        Builder.output_toolchain = stage2.installed_toolchain
        if hosts.build_host().is_linux and do_runtimes:
            build_runtimes(build_lldb_server=build_lldb)

    # Instrument with llvm-bolt. Must be the last build step to prevent other
    # build steps generating BOLT profiles.
    if need_host:
        if do_bolt_instrument:
            bolt_instrument(stage2)

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
