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
import subprocess
import sys
import textwrap
from typing import cast, Dict, List, Optional, Set

import benzo_version
import builders
from builder_registry import BuilderRegistry
import configs
import constants
import hosts
import paths
import toolchains
import utils
from version import Version

import mapfile

ORIG_ENV = dict(os.environ)

# Remove GOMA from our environment for building anything from stage2 onwards,
# since it is using a non-GOMA compiler (from stage1) to do the compilation.
USE_GOMA_FOR_STAGE1 = False
if ('USE_GOMA' in ORIG_ENV) and (ORIG_ENV['USE_GOMA'] == 'true'):
    USE_GOMA_FOR_STAGE1 = True
    del ORIG_ENV['USE_GOMA']

BASE_TARGETS = 'X86'
ANDROID_TARGETS = 'AArch64;ARM;BPF;X86'


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def install_file(src, dst):
    """Proxy for shutil.copy2 with logging and dry-run support."""
    logger().info('copy %s %s', src, dst)
    shutil.copy2(src, dst)


def remove(path):
    """Proxy for os.remove with logging."""
    logger().debug('remove %s', path)
    os.remove(path)


def extract_clang_version(clang_install) -> Version:
    version_file = (Path(clang_install) / 'include' / 'clang' / 'Basic' /
                    'Version.inc')
    return Version(version_file)


def pgo_profdata_filename():
    base_revision = benzo_version.svn_revision.rstrip(string.ascii_lowercase)
    return '%s.profdata' % base_revision

def pgo_profdata_file(profdata_file):
    profile = utils.android_path('prebuilts', 'clang', 'host', 'linux-x86',
                                 'profiles', profdata_file)
    return profile if os.path.exists(profile) else None


def ndk_base():
    ndk_version = 'r20'
    return utils.android_path('toolchain/prebuilts/ndk', ndk_version)


def android_api(arch: hosts.Arch, platform=False):
    if platform:
        return 29
    elif arch in [hosts.Arch.ARM, hosts.Arch.I386]:
        return 16
    else:
        return 21


def ndk_libcxx_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++',
                        'include')


def ndk_libcxxabi_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++abi',
                        'include')


def ndk_toolchain_lib(arch: hosts.Arch, toolchain_root, host_tag):
    toolchain_lib = os.path.join(ndk_base(), 'toolchains', toolchain_root,
                                 'prebuilt', 'linux-x86_64', host_tag)
    if arch in [hosts.Arch.ARM, hosts.Arch.I386]:
        toolchain_lib = os.path.join(toolchain_lib, 'lib')
    else:
        toolchain_lib = os.path.join(toolchain_lib, 'lib64')
    return toolchain_lib


def support_headers():
    return os.path.join(ndk_base(), 'sources', 'android', 'support', 'include')


def clang_prebuilt_bin_dir():
    return utils.android_path(paths.CLANG_PREBUILT_DIR, 'bin')


def clang_resource_dir(version, arch: Optional[hosts.Arch] = None):
    arch_str = arch.value if arch else ''
    return os.path.join('lib64', 'clang', version, 'lib', 'linux', arch_str)


def clang_prebuilt_libcxx_headers():
    return utils.android_path(paths.CLANG_PREBUILT_DIR, 'include', 'c++', 'v1')


def libcxx_header_dirs(ndk_cxx):
    if ndk_cxx:
        return [
            ndk_libcxx_headers(),
            ndk_libcxxabi_headers(),
            support_headers()
        ]
    else:
        # <prebuilts>/include/c++/v1 includes the cxxabi headers
        return [
            clang_prebuilt_libcxx_headers(),
            utils.android_path('bionic', 'libc', 'include')
        ]


def cmake_bin_path():
    return utils.android_path(paths.CMAKE_BIN_PATH)


def ninja_bin_path():
    return utils.android_path(paths.NINJA_BIN_PATH)


def go_bin_dir():
    return utils.android_path(paths.GO_BIN_PATH)


def check_create_path(path):
    if not os.path.exists(path):
        os.makedirs(path)


def get_sysroot(arch: hosts.Arch, platform=False):
    sysroots = utils.out_path('sysroots')
    platform_or_ndk = 'platform' if platform else 'ndk'
    return os.path.join(sysroots, platform_or_ndk, arch.ndk_arch)


def debug_prefix_flag():
    return '-fdebug-prefix-map={}='.format(utils.android_path())


def create_sysroots():
    # Construct the sysroots from scratch, since symlinks can't nest within
    # the right places (without altering source prebuilts).
    configs = [
        (hosts.Arch.ARM, 'arm-linux-androideabi'),
        (hosts.Arch.AARCH64, 'aarch64-linux-android'),
        (hosts.Arch.X86_64, 'x86_64-linux-android'),
        (hosts.Arch.I386, 'i686-linux-android'),
    ]

    # TODO(srhines): We destroy and recreate the sysroots each time, but this
    # could check for differences and only replace files if needed.
    sysroots_out = utils.out_path('sysroots')
    if os.path.exists(sysroots_out):
        shutil.rmtree(sysroots_out)
    check_create_path(sysroots_out)

    base_header_path = os.path.join(ndk_base(), 'sysroot', 'usr', 'include')
    for (arch, target) in configs:
        # Also create sysroots for each of platform and the NDK.
        for platform_or_ndk in ['platform', 'ndk']:
            platform = platform_or_ndk == 'platform'
            base_lib_path = \
                utils.android_path(ndk_base(), 'platforms',
                                   'android-' + str(android_api(arch, platform)))
            dest_usr = os.path.join(get_sysroot(arch, platform), 'usr')

            # Copy over usr/include.
            dest_usr_include = os.path.join(dest_usr, 'include')
            shutil.copytree(base_header_path, dest_usr_include, symlinks=True)

            # Copy over usr/include/asm.
            asm_headers = os.path.join(base_header_path, target, 'asm')
            dest_usr_include_asm = os.path.join(dest_usr_include, 'asm')
            shutil.copytree(asm_headers, dest_usr_include_asm, symlinks=True)

            # Copy over usr/lib.
            arch_lib_path = os.path.join(base_lib_path, 'arch-' + arch.ndk_arch,
                                         'usr', 'lib')
            dest_usr_lib = os.path.join(dest_usr, 'lib')
            shutil.copytree(arch_lib_path, dest_usr_lib, symlinks=True)

            # For only x86_64, we also need to copy over usr/lib64
            if arch == hosts.Arch.X86_64:
                arch_lib64_path = os.path.join(base_lib_path, 'arch-' + arch.ndk_arch,
                                               'usr', 'lib64')
                dest_usr_lib64 = os.path.join(dest_usr, 'lib64')
                shutil.copytree(arch_lib64_path, dest_usr_lib64, symlinks=True)

            if platform:
                # Create a stub library for the platform's libc++.
                platform_stubs = utils.out_path('platform_stubs', arch.ndk_arch)
                check_create_path(platform_stubs)
                libdir = dest_usr_lib64 if arch == hosts.Arch.X86_64 else dest_usr_lib
                with open(os.path.join(platform_stubs, 'libc++.c'), 'w') as f:
                    f.write(textwrap.dedent("""\
                        void __cxa_atexit() {}
                        void __cxa_demangle() {}
                        void __cxa_finalize() {}
                        void __dynamic_cast() {}
                        void _ZTIN10__cxxabiv117__class_type_infoE() {}
                        void _ZTIN10__cxxabiv120__si_class_type_infoE() {}
                        void _ZTIN10__cxxabiv121__vmi_class_type_infoE() {}
                        void _ZTISt9type_info() {}
                    """))
                utils.check_call([utils.out_path('stage2-install', 'bin', 'clang'),
                                  '--target=' + target,
                                  '-fuse-ld=lld', '-nostdlib', '-shared',
                                  '-Wl,-soname,libc++.so',
                                  '-o', os.path.join(libdir, 'libc++.so'),
                                  os.path.join(platform_stubs, 'libc++.c')])

                # For arm64 and x86_64, build static cxxabi library from
                # toolchain/libcxxabi and use it when building runtimes.  This
                # should affect all compiler-rt runtimes that use libcxxabi
                # (e.g. asan, hwasan, scudo, tsan, ubsan, xray).
                if arch not in (hosts.Arch.AARCH64, hosts.Arch.X86_64):
                    with open(os.path.join(libdir, 'libc++abi.so'), 'w') as f:
                        f.write('INPUT(-lc++)')
                else:
                    # We can build libcxxabi only after the sysroots are
                    # created.  Build it for the current arch and copy it to
                    # <libdir>.
                    out_dir = build_libcxxabi(utils.out_path('stage2-install'), arch)
                    out_path = utils.out_path(out_dir, 'lib64', 'libc++abi.a')
                    shutil.copy2(out_path, os.path.join(libdir))


def update_cmake_sysroot_flags(defines, sysroot):
    defines['CMAKE_SYSROOT'] = sysroot
    defines['CMAKE_FIND_ROOT_PATH_MODE_INCLUDE'] = 'ONLY'
    defines['CMAKE_FIND_ROOT_PATH_MODE_LIBRARY'] = 'ONLY'
    defines['CMAKE_FIND_ROOT_PATH_MODE_PACKAGE'] = 'ONLY'
    defines['CMAKE_FIND_ROOT_PATH_MODE_PROGRAM'] = 'NEVER'


def rm_cmake_cache(cacheDir):
    for dirpath, dirs, files in os.walk(cacheDir): # pylint: disable=not-an-iterable
        if 'CMakeCache.txt' in files:
            os.remove(os.path.join(dirpath, 'CMakeCache.txt'))
        if 'CMakeFiles' in dirs:
            utils.rm_tree(os.path.join(dirpath, 'CMakeFiles'))


# Base cmake options such as build type that are common across all invocations
def base_cmake_defines():
    defines = {}

    defines['CMAKE_BUILD_TYPE'] = 'Release'
    defines['LLVM_ENABLE_ASSERTIONS'] = 'OFF'
    # https://github.com/android-ndk/ndk/issues/574 - Don't depend on libtinfo.
    defines['LLVM_ENABLE_TERMINFO'] = 'OFF'
    defines['LLVM_ENABLE_THREADS'] = 'ON'
    defines['LLVM_OPTIMIZED_TABLEGEN'] = 'ON'
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
    return defines


def invoke_cmake(out_path, defines, env, cmake_path, target=None, install=True):
    flags = ['-G', 'Ninja']

    flags += ['-DCMAKE_MAKE_PROGRAM=' + ninja_bin_path()]

    for key in defines:
        newdef = '-D' + key + '=' + defines[key]
        flags += [newdef]
    flags += [cmake_path]

    check_create_path(out_path)
    # TODO(srhines): Enable this with a flag, because it forces clean builds
    # due to the updated cmake generated files.
    #rm_cmake_cache(out_path)

    if target:
        ninja_target = [target]
    else:
        ninja_target = []

    utils.check_call([cmake_bin_path()] + flags, cwd=out_path, env=env)
    utils.check_call([ninja_bin_path()] + ninja_target, cwd=out_path, env=env)
    if install:
        utils.check_call([ninja_bin_path(), 'install'], cwd=out_path, env=env)


def cross_compile_configs(toolchain, platform=False, static=False):
    configs = [
        (hosts.Arch.ARM, 'arm/arm-linux-androideabi-4.9/arm-linux-androideabi',
         'arm-linux-android', '-march=armv7-a'),
        (hosts.Arch.AARCH64,
         'aarch64/aarch64-linux-android-4.9/aarch64-linux-android',
         'aarch64-linux-android', ''),
        (hosts.Arch.X86_64,
         'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'x86_64-linux-android', ''),
        (hosts.Arch.I386, 'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'i686-linux-android', '-m32'),
    ]

    cc = os.path.join(toolchain, 'bin', 'clang')
    cxx = os.path.join(toolchain, 'bin', 'clang++')
    llvm_config = os.path.join(toolchain, 'bin', 'llvm-config')

    for (arch, toolchain_path, llvm_triple, extra_flags) in configs:
        if static:
            api_level = android_api(arch, platform=True)
        else:
            api_level = android_api(arch, platform)
        toolchain_root = utils.android_path('prebuilts/gcc',
                                            hosts.build_host().os_tag)
        toolchain_bin = os.path.join(toolchain_root, toolchain_path, 'bin')
        sysroot = get_sysroot(arch, platform)

        defines = {}
        defines['CMAKE_C_COMPILER'] = cc
        defines['CMAKE_CXX_COMPILER'] = cxx
        defines['LLVM_CONFIG_PATH'] = llvm_config

        # Include the directory with libgcc.a to the linker search path.
        toolchain_builtins = os.path.join(
            toolchain_root, toolchain_path, '..', 'lib', 'gcc',
            os.path.basename(toolchain_path), '4.9.x')
        # The 32-bit libgcc.a is sometimes in a separate subdir
        if arch == hosts.Arch.I386:
            toolchain_builtins = os.path.join(toolchain_builtins, '32')

        if arch == hosts.Arch.ARM:
            toolchain_lib = ndk_toolchain_lib(arch, 'arm-linux-androideabi-4.9',
                                              'arm-linux-androideabi')
        elif arch in [hosts.Arch.I386, hosts.Arch.X86_64]:
            toolchain_lib = ndk_toolchain_lib(arch, arch.ndk_arch + '-4.9',
                                              llvm_triple)
        else:
            toolchain_lib = ndk_toolchain_lib(arch, llvm_triple + '-4.9',
                                              llvm_triple)

        ldflags = [
            '-L' + toolchain_builtins, '-Wl,-z,defs',
            '-L' + toolchain_lib,
            '-fuse-ld=lld',
            '-Wl,--gc-sections',
            '-Wl,--build-id=sha1',
            '-pie',
        ]
        if static:
            ldflags.append('-static')
        if not platform:
            triple = 'arm-linux-androideabi' if arch == hosts.Arch.ARM else llvm_triple
            libcxx_libs = os.path.join(ndk_base(), 'toolchains', 'llvm',
                                       'prebuilt', 'linux-x86_64', 'sysroot',
                                       'usr', 'lib', triple)
            ldflags += ['-L', os.path.join(libcxx_libs, str(api_level))]
            ldflags += ['-L', libcxx_libs]

        defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)
        update_cmake_sysroot_flags(defines, sysroot)

        macro_api_level = 10000 if platform else api_level

        cflags = [
            debug_prefix_flag(),
            '--target=%s' % llvm_triple,
            '-B%s' % toolchain_bin,
            '-D__ANDROID_API__=' + str(macro_api_level),
            '-ffunction-sections',
            '-fdata-sections',
            extra_flags,
        ]
        yield (arch, llvm_triple, defines, cflags)


def build_asan_test(toolchain):
    # We can not build asan_test using current CMake building system. Since
    # those files are not used to build AOSP, we just simply touch them so that
    # we can pass the build checks.
    for arch in ('aarch64', 'arm', 'i686'):
        asan_test_path = os.path.join(toolchain, 'test', arch, 'bin')
        check_create_path(asan_test_path)
        asan_test_bin_path = os.path.join(asan_test_path, 'asan_test')
        open(asan_test_bin_path, 'w+').close()

def build_sanitizer_map_file(san, arch, lib_dir):
    lib_file = os.path.join(lib_dir, 'libclang_rt.{}-{}-android.so'.format(san, arch))
    map_file = os.path.join(lib_dir, 'libclang_rt.{}-{}-android.map.txt'.format(san, arch))
    mapfile.create_map_file(lib_file, map_file)

def build_sanitizer_map_files(toolchain, clang_version):
    lib_dir = os.path.join(toolchain,
                           clang_resource_dir(clang_version.long_version()))
    for arch in ('aarch64', 'arm', 'i686', 'x86_64'):
        build_sanitizer_map_file('asan', arch, lib_dir)
        build_sanitizer_map_file('ubsan_standalone', arch, lib_dir)
    build_sanitizer_map_file('hwasan', 'aarch64', lib_dir)

def create_hwasan_symlink(toolchain, clang_version):
    lib_dir = os.path.join(toolchain,
                           clang_resource_dir(clang_version.long_version()))
    symlink_path = lib_dir + 'libclang_rt.hwasan_static-aarch64-android.a'
    utils.remove(symlink_path)
    os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)

def build_libcxx(toolchain, clang_version):
    for (arch, llvm_triple, libcxx_defines,
         cflags) in cross_compile_configs(toolchain): # pylint: disable=not-an-iterable
        logger().info('Building libcxx for %s', arch.value)
        libcxx_path = utils.out_path('lib', 'libcxx-' + arch.value)

        libcxx_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_BUILD_TYPE'] = 'Release'

        libcxx_env = dict(ORIG_ENV)

        libcxx_cmake_path = utils.llvm_path('libcxx')
        rm_cmake_cache(libcxx_path)

        invoke_cmake(
            out_path=libcxx_path,
            defines=libcxx_defines,
            env=libcxx_env,
            cmake_path=libcxx_cmake_path,
            install=False)
        # We need to install libcxx manually.
        install_subdir = clang_resource_dir(clang_version.long_version(),
                                            hosts.Arch.from_triple(llvm_triple))
        libcxx_install = os.path.join(toolchain, install_subdir)

        libcxx_libs = os.path.join(libcxx_path, 'lib')
        check_create_path(libcxx_install)
        for f in os.listdir(libcxx_libs):
            if f.startswith('libc++'):
                shutil.copy2(os.path.join(libcxx_libs, f), libcxx_install)


def build_libcxxabi(toolchain, build_arch: hosts.Arch):
    # TODO: Refactor cross_compile_configs to support per-arch queries in
    # addition to being a generator.
    for (arch, llvm_triple, defines, cflags) in \
         cross_compile_configs(toolchain, platform=True): # pylint: disable=not-an-iterable

        # Build only the requested arch.
        if arch != build_arch:
            continue

        logger().info('Building libcxxabi for %s', arch.value)
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = utils.llvm_path('libcxx', 'include')
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

        out_path = utils.out_path('lib', 'libcxxabi-' + arch.value)
        if os.path.exists(out_path):
            utils.rm_tree(out_path)

        invoke_cmake(out_path=out_path,
                     defines=defines,
                     env=dict(ORIG_ENV),
                     cmake_path=utils.llvm_path('libcxxabi'),
                     install=False)
        return out_path


def build_crts_host_i686(toolchain, clang_version):
    logger().info('Building compiler-rt for host-i686')

    llvm_config = os.path.join(toolchain, 'bin', 'llvm-config')

    crt_install = os.path.join(toolchain, 'lib64', 'clang',
                               clang_version.long_version())
    crt_cmake_path = utils.llvm_path('compiler-rt')

    cflags, ldflags = host_gcc_toolchain_flags(hosts.build_host(), is_32_bit=True)

    crt_defines = base_cmake_defines()
    crt_defines['CMAKE_C_COMPILER'] = os.path.join(toolchain, 'bin',
                                                   'clang')
    crt_defines['CMAKE_CXX_COMPILER'] = os.path.join(toolchain, 'bin',
                                                     'clang++')

    # compiler-rt/lib/gwp_asan uses PRIu64 and similar format-specifier macros.
    # Add __STDC_FORMAT_MACROS so their definition gets included from
    # inttypes.h.  This explicit flag is only needed here.  64-bit host runtimes
    # are built in stage1/stage2 and get it from the LLVM CMake configuration.
    # These are defined unconditionaly in bionic and newer glibc
    # (https://sourceware.org/git/gitweb.cgi?p=glibc.git;h=1ef74943ce2f114c78b215af57c2ccc72ccdb0b7)
    cflags.append('-D__STDC_FORMAT_MACROS')

    # Due to CMake and Clang oddities, we need to explicitly set
    # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
    # relying on auto-detection from the Compiler-rt CMake files.
    crt_defines['CMAKE_C_COMPILER_TARGET'] = 'i386-linux-gnu'

    crt_defines['CMAKE_SYSROOT'] = host_sysroot()

    cflags.append('--target=i386-linux-gnu')
    cflags.append('-march=i686')
    cflags.append('-Wno-unused-command-line-argument')

    crt_defines['LLVM_CONFIG_PATH'] = llvm_config
    crt_defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
    crt_defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
    crt_defines['CMAKE_INSTALL_PREFIX'] = crt_install
    crt_defines['SANITIZER_CXX_ABI'] = 'libstdc++'

    # Set the compiler and linker flags
    crt_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
    crt_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    crt_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

    crt_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    crt_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    crt_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    crt_env = dict(ORIG_ENV)

    crt_path = utils.out_path('lib', 'clangrt-i386-host')
    rm_cmake_cache(crt_path)

    # Also remove the "stamps" created for the libcxx included in libfuzzer so
    # CMake runs the configure again (after the cmake caches are deleted in the
    # line above).
    utils.remove(os.path.join(crt_path, 'lib', 'fuzzer', 'libcxx_fuzzer_i386-stamps'))

    invoke_cmake(
        out_path=crt_path,
        defines=crt_defines,
        env=crt_env,
        cmake_path=crt_cmake_path)


def host_sysroot():
    return utils.android_path('prebuilts/gcc', hosts.build_host().os_tag,
                              'host/x86_64-linux-glibc2.17-4.8/sysroot')


def host_gcc_toolchain_flags(host: hosts.Host, is_32_bit=False):
    cflags: List[str] = [debug_prefix_flag()]
    ldflags: List[str] = []

    gccRoot = utils.android_path('prebuilts/gcc', hosts.build_host().os_tag,
                                     'host/x86_64-linux-glibc2.17-4.8')
    gccTriple = 'x86_64-linux'
    gccVersion = '4.8.3'

    # gcc-toolchain is only needed for Linux
    cflags.append(f'--gcc-toolchain={gccRoot}')

    cflags.append(f'-B{gccRoot}/{gccTriple}/bin')

    gccLibDir = f'{gccRoot}/lib/gcc/{gccTriple}/{gccVersion}'
    gccBuiltinDir = f'{gccRoot}/{gccTriple}/lib64'
    if is_32_bit:
        gccLibDir += '/32'
        gccBuiltinDir = gccBuiltinDir.replace('lib64', 'lib32')

    ldflags.extend(('-B' + gccLibDir,
                    '-L' + gccLibDir,
                    '-B' + gccBuiltinDir,
                    '-L' + gccBuiltinDir,
                    '-fuse-ld=lld',
                   ))

    return cflags, ldflags


class Stage1Builder(builders.LLVMBuilder):
    name: str = 'stage1'
    toolchain_name: str = 'prebuilt'
    install_dir: Path = paths.OUT_DIR / 'stage1-install'
    build_llvm_tools: bool = False
    build_all_targets: bool = False
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def llvm_targets(self) -> Set[str]:
        if self.build_all_targets:
            return set(ANDROID_TARGETS.split(';'))
        else:
            return set(BASE_TARGETS.split(';'))

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt'}
        return proj

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Point CMake to the libc++.so from the prebuilts.  Install an rpath
        # to prevent linking with the newly-built libc++.so
        ldflags.append(f'-Wl,-rpath,{self.toolchain.lib_dir}')
        return ldflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CLANG_ENABLE_ARCMT'] = 'OFF'
        defines['CLANG_ENABLE_STATIC_ANALYZER'] = 'OFF'

        if self.build_llvm_tools:
            defines['LLVM_BUILD_TOOLS'] = 'ON'
        else:
            defines['LLVM_BUILD_TOOLS'] = 'OFF'

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.
        defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
        defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

        # Don't build libfuzzer as part of the first stage build.
        defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

        return defines

    @property
    def env(self) -> Dict[str, str]:
        env = super().env
        if USE_GOMA_FOR_STAGE1:
            env['USE_GOMA'] = 'true'
        return env


class Stage2Builder(builders.LLVMBuilder):
    name: str = 'stage2'
    toolchain_name: str = 'stage1'
    install_dir: Path = paths.OUT_DIR / 'stage2-install'
    config_list: List[configs.Config] = [configs.host_config()]
    remove_install_dir: bool = True
    debug_build: bool = False
    build_instrumented: bool = False
    profdata_file: Optional[Path] = None
    lto: bool = True

    @property
    def llvm_targets(self) -> Set[str]:
        return set(ANDROID_TARGETS.split(';'))

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt',
                'clang-tools-extra', 'openmp', 'polly'}
        return proj

    @property
    def env(self) -> Dict[str, str]:
        env = super().env
        # Point CMake to the libc++ from stage1.  It is possible that once built,
        # the newly-built libc++ may override this because of the rpath pointing to
        # $ORIGIN/../lib64.  That'd be fine because both libraries are built from
        # the same sources.
        env['LD_LIBRARY_PATH'] = str(self.toolchain.lib_dir)
        return env

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if self.build_instrumented:
            # Building libcxx, libcxxabi with instrumentation causes linker errors
            # because these are built with -nodefaultlibs and prevent libc symbols
            # needed by libclang_rt.profile from being resolved.  Manually adding
            # the libclang_rt.profile to linker flags fixes the issue.
            resource_dir = self.toolchain.resource_dir
            ldflags.append(str(resource_dir / 'libclang_rt.profile-x86_64.a'))
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        if self.profdata_file:
            cflags.append('-Wno-profile-instr-out-of-date')
            cflags.append('-Wno-profile-instr-unprofiled')
        return cflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['SANITIZER_ALLOW_CXXABI'] = 'OFF'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'FALSE'
        defines['LLVM_POLLY_LINK_INTO_TOOLS'] = 'ON'
        defines['CLANG_DEFAULT_LINKER'] = 'lld'

        if (self.lto and
                not self.build_instrumented and
                not self.debug_build):
            defines['LLVM_ENABLE_LTO'] = 'Thin'

        # Build libFuzzer here to be exported for the host fuzzer builds.
        defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'ON'

        if self.debug_build:
            defines['CMAKE_BUILD_TYPE'] = 'Debug'

        if self.build_instrumented:
            defines['LLVM_BUILD_INSTRUMENTED'] = 'ON'

            # llvm-profdata is only needed to finish CMake configuration
            # (tools/clang/utils/perf-training/CMakeLists.txt) and not needed for
            # build
            llvm_profdata = self.toolchain.path / 'bin' / 'llvm-profdata'
            defines['LLVM_PROFDATA'] = str(llvm_profdata)
        elif self.profdata_file:
            defines['LLVM_PROFDATA_FILE'] = str(self.profdata_file)

        # Disable some warnings for openmp
        openmp_cflags = (
            '-Wno-c99-extensions',
            '-Wno-deprecated-copy',
            '-Wno-gnu-anonymous-struct',
            '-Wno-missing-field-initializers',
            '-Wno-non-c-typedef-for-linkage',
            '-Wno-vla-extension')
        defines['LIBOMP_CXXFLAGS'] = ' '.join(openmp_cflags)

        defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'

        # Disable a bunch of unused tools
        defines['LLVM_INCLUDE_TESTS'] = 'OFF'
        defines['LLVM_TOOL_LLVM_LIPO_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_JITLINK_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_AS_FUZZER_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_BCANALYZER_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_CAT_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_CVTRES_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_CXXDUMP_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_CXXFILT_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_CXXMAP_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_C_TEST_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_DIFF_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_DWP_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_ELFABI_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_EXEGESIS_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_EXTRACT_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_GO_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_GSYMUTIL_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_IFS_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_ISEL_FUZZER_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_ITANIUM_DEMANGLE_FUZZER_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_JITLINK_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_LIPO_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_LTO2_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_LTO_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_MCA_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_MC_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_ML_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_MT_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_OPT_FUZZER_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_OPT_REPORT_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_PDBUTIL_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_REDUCE_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_RTDYLD_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_SPLIT_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_STRESS_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_UNDNAME_BUILD'] = 'OFF'
        defines['LLVM_TOOL_LLVM_XRAY_BUILD'] = 'OFF'

        return defines


class CompilerRTBuilder(builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    @property
    def install_dir(self) -> Path:
        if self._config.platform:
            return self.toolchain.clang_lib_dir
        # Installs to a temporary dir and copies to runtimes_ndk_cxx manually.
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        arch = self._config.target_arch
        # FIXME: Disable WError build until upstream fixed the compiler-rt
        # personality routine warnings caused by r309226.
        # defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = defines['CMAKE_C_FLAGS']
        defines['COMPILER_RT_TEST_TARGET_TRIPLE'] = arch.llvm_triple
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        # With CMAKE_SYSTEM_NAME='Android', compiler-rt will be installed to
        # lib/android instead of lib/linux.
        del defines['CMAKE_SYSTEM_NAME']
        libs: List[str] = []
        if arch == 'arm':
            libs += ['-latomic']
        if self._config.api_level < 21:
            libs += ['-landroid_support']
        defines['SANITIZER_COMMON_LINK_LIBS'] = ' '.join(libs)
        if self._config.platform:
            defines['COMPILER_RT_HWASAN_WITH_INTERCEPTORS'] = 'OFF'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-funwind-tables')
        cflags.append('-Wno-unused-command-line-argument')
        return cflags

    def install_config(self) -> None:
        # Still run `ninja install`.
        super().install_config()

        # Install the fuzzer library to the old {arch}/libFuzzer.a path for
        # backwards compatibility.
        arch = self._config.target_arch
        sarch = 'i686' if arch == hosts.Arch.I386 else arch.value
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'

        lib_dir = self.install_dir / 'lib' / 'linux'
        arch_dir = lib_dir / arch.value
        arch_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib_dir / static_lib_filename, arch_dir / 'libFuzzer.a')

        if not self._config.platform:
            dst_dir = self.toolchain.path / 'runtimes_ndk_cxx'
            shutil.copytree(lib_dir, dst_dir, dirs_exist_ok=True)

    def install(self) -> None:
        # Install libfuzzer headers once for all configs.
        header_src = self.src_dir / 'lib' / 'fuzzer'
        header_dst = self.toolchain.path / 'prebuilt_include' / 'llvm' / 'lib' / 'Fuzzer'
        header_dst.mkdir(parents=True, exist_ok=True)
        for f in header_src.iterdir():
            if f.suffix in ('.h', '.def'):
                shutil.copy2(f, header_dst)


class LibOMPBuilder(builders.LLVMRuntimeBuilder):
    name: str = 'libomp'
    src_dir: Path = paths.LLVM_PATH / 'openmp'

    config_list: List[configs.Config] = (
        configs.android_configs(platform=True, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': True})
    )

    @property
    def is_shared(self) -> bool:
        return cast(Dict[str, bool], self._config.extra_config)['is_shared']

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-shared' if self.is_shared else '-static'
        return old_path.parent / (old_path.name + suffix)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CMAKE_POSITION_INDEPENDENT_CODE'] = 'ON'
        defines['OPENMP_ENABLE_LIBOMPTARGET'] = 'FALSE'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'TRUE' if self.is_shared else 'FALSE'
        # Minimum version for OpenMP's CMake is too low for the CMP0056 policy
        # to be ON by default.
        defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-Wno-unused-command-line-argument')
        cflags.append('-Wno-non-c-typedef-for-linkage')
        return cflags

    def install_config(self) -> None:
        # We need to install libomp manually.
        libname = 'libomp.' + ('so' if self.is_shared else 'a')
        src_lib = self.output_dir / 'runtime' / 'src' / libname
        dst_dir = self.install_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_lib, dst_dir / libname)


def build_runtimes(toolchain, args=None):
    if not BuilderRegistry.should_build('sysroot'):
        logger().info('Skip libcxxabi and other sysroot libraries')
    else:
        create_sysroots()
    version = extract_clang_version(toolchain)
    CompilerRTBuilder().build()
    if BuilderRegistry.should_build('compiler-rt'):
        build_crts_host_i686(toolchain, version)
    LibOMPBuilder().build()
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(toolchain, version)
    if not BuilderRegistry.should_build('asan'):
        logger().info('Skip asan test, map, symlink')
    else:
        build_asan_test(toolchain)
        build_sanitizer_map_files(toolchain, version)
        create_hwasan_symlink(toolchain, version)

def install_wrappers(llvm_install_path):
    wrapper_path = utils.out_path('llvm_android_wrapper')
    wrapper_build_script = utils.android_path('external', 'toolchain-utils',
                                              'compiler_wrapper', 'build.py')
    # Note: The build script automatically determines the architecture
    # based on the host.
    go_env = dict(os.environ)
    go_env['PATH'] = go_bin_dir() + ':' + go_env['PATH']
    utils.check_call([sys.executable, wrapper_build_script,
                '--config=android',
                '--use_ccache=false',
                '--use_llvm_next=true',
                '--output_file=' + wrapper_path], env=go_env)

    bisect_path = utils.android_path('toolchain', 'llvm_benzo',
                                     'bisect_driver.py')
    bin_path = os.path.join(llvm_install_path, 'bin')
    clang_path = os.path.join(bin_path, 'clang')
    clangxx_path = os.path.join(bin_path, 'clang++')
    clang_tidy_path = os.path.join(bin_path, 'clang-tidy')

    # Rename clang and clang++ to clang.real and clang++.real.
    # clang and clang-tidy may already be moved by this script if we use a
    # prebuilt clang. So we only move them if clang.real and clang-tidy.real
    # doesn't exist.
    if not os.path.exists(clang_path + '.real'):
        shutil.move(clang_path, clang_path + '.real')
    if not os.path.exists(clang_tidy_path + '.real'):
        shutil.move(clang_tidy_path, clang_tidy_path + '.real')
    utils.remove(clang_path)
    utils.remove(clangxx_path)
    utils.remove(clang_tidy_path)
    utils.remove(clangxx_path + '.real')
    os.symlink('clang.real', clangxx_path + '.real')

    shutil.copy2(wrapper_path, clang_path)
    shutil.copy2(wrapper_path, clangxx_path)
    shutil.copy2(wrapper_path, clang_tidy_path)
    install_file(bisect_path, bin_path)


# Normalize host libraries (libLLVM, libclang, libc++, libc++abi) so that there
# is just one library, whose SONAME entry matches the actual name.
def normalize_llvm_host_libs(install_dir, host: hosts.Host, version):
    libs = {'libLLVM': 'libLLVM-{version}git.so',
            'libclang': 'libclang.so.{version}git',
            'libclang_cxx': 'libclang_cxx.so.{version}git',
            'libc++': 'libc++.so.{version}',
            'libc++abi': 'libc++abi.so.{version}'
           }

    def getVersions(libname):
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
                remove(lib)


def install_license_files(install_dir):
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
        license_pattern = utils.llvm_path(project, 'LICENSE.*')
        for license_file in glob.glob(license_pattern):
            with open(license_file) as notice_file:
                notices.append(notice_file.read())
    with open(os.path.join(install_dir, 'NOTICE'), 'w') as notice_file:
        notice_file.write('\n'.join(notices))


def remove_static_libraries(static_lib_dir, necessary_libs=None):
    if not necessary_libs:
        necessary_libs = {}
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a') and lib_file not in necessary_libs:
                static_library = os.path.join(static_lib_dir, lib_file)
                remove(static_library)


def get_package_install_path(host: hosts.Host, package_name):
    return utils.out_path('install', host.os_tag, package_name)


def package_toolchain(build_dir, build_name, host: hosts.Host, dist_dir, strip=True, create_tar=True):
    package_name = 'clang-' + build_name
    version = extract_clang_version(build_dir)

    install_dir = get_package_install_path(host, package_name)
    install_host_dir = os.path.realpath(os.path.join(install_dir, '../'))

    # Remove any previously installed toolchain so it doesn't pollute the
    # build.
    if os.path.exists(install_host_dir):
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

    bin_dir = os.path.join(install_dir, 'bin')
    lib_dir = os.path.join(install_dir, 'lib64')

    for bin_filename in os.listdir(bin_dir):
        binary = os.path.join(bin_dir, bin_filename)
        if os.path.isfile(binary):
            if bin_filename not in necessary_bin_files:
                remove(binary)
            elif strip and bin_filename not in script_bins:
                utils.check_call(['strip', binary])

    # FIXME: check that all libs under lib64/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if not os.path.isfile(os.path.join(bin_dir, necessary_bin_file)):
            raise RuntimeError('Did not find %s in %s' % (necessary_bin_file, bin_dir))

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
        if not os.path.isfile(os.path.join(lib_dir, necessary_lib_file)):
            raise RuntimeError('Did not find %s in %s' % (necessary_lib_file, lib_dir))

    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an VERSION file.
    version_file_path = os.path.join(install_dir, 'VERSION')
    svn_revision = benzo_version.svn_revision
    with open(version_file_path, 'w') as version_file:
        version_file.write('11.0.0-{}-benzoClang\n'.format(svn_revision))

    # Package up the resulting trimmed install/ directory.
    if create_tar:
        tarball_name = package_name + '-' + host.os_tag
        package_path = os.path.join(dist_dir, tarball_name) + '.tar.bz2'
        logger().info('Packaging %s', package_path)
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
        '--check-pgo-profile',
        action='store_true',
        default=False,
        help='Fail if expected PGO profile doesn\'t exist')

    parser.add_argument(
        '--ccache',
        action='store_true',
        default=False,
        help='Enable the use of ccache during build')

    return parser.parse_args()


def main():
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

    stage2_install = utils.out_path('stage2-install')

    # Build the stage1 Clang for the build host
    instrumented = args.build_instrumented

    # llvm-config is required.
    stage1_build_llvm_tools = instrumented or \
                              args.debug

    stage1 = Stage1Builder()
    stage1.build_name = args.build_name
    stage1.clang_vendor = 'benzoClang'
    stage1.ccache = args.ccache
    stage1.build_llvm_tools = stage1_build_llvm_tools
    stage1.build_all_targets = args.debug or instrumented
    stage1.build()
    stage1_install = str(stage1.install_dir)

    if need_host:
        profdata_filename = pgo_profdata_filename()
        profdata = pgo_profdata_file(profdata_filename)
        # Do not use PGO profiles if profdata file doesn't exist unless failure
        # is explicitly requested via --check-pgo-profile.
        if profdata is None and args.check_pgo_profile:
            raise RuntimeError('Profdata file does not exist for ' +
                               profdata_filename)

        stage2 = Stage2Builder()
        stage2.build_name = args.build_name
        stage2.clang_vendor = 'benzoClang'
        stage2.ccache = args.ccache
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = not args.no_lto
        stage2.build_instrumented = instrumented
        stage2.profdata_file = Path(profdata) if profdata else None
        stage2.build()
        stage2_install = str(stage2.install_dir)

        if do_runtimes:
            runtimes_toolchain = stage2_install
            if args.debug or instrumented:
                runtimes_toolchain = stage1_install
            build_runtimes(runtimes_toolchain, args)

    dist_dir = ORIG_ENV.get('DIST_DIR', utils.out_path())
    if do_package and need_host:
        package_toolchain(
            stage2_install,
            args.build_name,
            hosts.build_host(),
            dist_dir,
            strip=do_strip_host_package)

    return 0


if __name__ == '__main__':
    main()
