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
"""Builder instances for various targets."""

import contextlib
from pathlib import Path
import os
import shutil
import textwrap
from typing import cast, Dict, Iterator, List, Optional, Set

import base_builders
import configs
import constants
import hosts
import mapfile
import paths
import toolchains
import utils

class AsanMapFileBuilder(base_builders.Builder):
    name: str = 'asan-mapfile'
    config_list: List[configs.Config] = configs.android_configs()

    def _build_config(self) -> None:
        arch = self._config.target_arch
        # We can not build asan_test using current CMake building system. Since
        # those files are not used to build AOSP, we just simply touch them so that
        # we can pass the build checks.
        asan_test_path = self.output_toolchain.path / 'test' / arch.llvm_arch / 'bin'
        asan_test_path.mkdir(parents=True, exist_ok=True)
        asan_test_bin_path = asan_test_path / 'asan_test'
        asan_test_bin_path.touch(exist_ok=True)

        lib_dir = self.output_toolchain.resource_dir
        self._build_sanitizer_map_file('asan', arch, lib_dir)
        self._build_sanitizer_map_file('ubsan_standalone', arch, lib_dir)

        if arch == hosts.Arch.AARCH64:
            self._build_sanitizer_map_file('hwasan', arch, lib_dir)

    @staticmethod
    def _build_sanitizer_map_file(san: str, arch: hosts.Arch, lib_dir: Path) -> None:
        lib_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.so'
        map_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.map.txt'
        mapfile.create_map_file(lib_file, map_file)


class Stage1Builder(base_builders.LLVMBuilder):
    name: str = 'stage1'
    install_dir: Path = paths.OUT_DIR / 'stage1-install'
    build_android_targets: bool = False
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def llvm_targets(self) -> Set[str]:
        if self.build_android_targets:
            return constants.HOST_TARGETS | constants.ANDROID_TARGETS
        else:
            return constants.HOST_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt'}
        return proj

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Use -static-libstdc++ to statically link the c++ runtime [1].  This
        # avoids specifying self.toolchain.lib_dir in rpath to find libc++ at
        # runtime.
        # [1] libc++ in our case, despite the flag saying -static-libstdc++.
        ldflags.append('-static-libstdc++')
        return ldflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CLANG_ENABLE_ARCMT'] = 'OFF'
        defines['CLANG_ENABLE_STATIC_ANALYZER'] = 'OFF'

        defines['LLVM_BUILD_TOOLS'] = 'ON'

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.
        defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
        defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

        # Don't build libfuzzer as part of the first stage build.
        defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

        return defines


class Stage2Builder(base_builders.LLVMBuilder):
    name: str = 'stage2'
    install_dir: Path = paths.OUT_DIR / 'stage2-install'
    config_list: List[configs.Config] = [configs.host_config()]
    remove_install_dir: bool = True
    debug_build: bool = False
    build_instrumented: bool = False
    profdata_file: Optional[Path] = None
    lto: bool = True

    @property
    def llvm_targets(self) -> Set[str]:
        return constants.ANDROID_TARGETS

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
        defines['CLANG_PYTHON_BINDINGS_VERSIONS'] = '3'

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

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.
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


class CompilerRTBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    @property
    def install_dir(self) -> Path:
        if self._config.platform:
            return self.output_toolchain.clang_lib_dir
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
        defines['COMPILER_RT_DEFAULT_TARGET_TRIPLE'] = arch.llvm_triple
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
        cflags.append('-Wno-incompatible-pointer-types')
        cflags.append('-Wno-unused-command-line-argument')
        cflags.append('-Wno-unused-variable')
        cflags.append('-Wno-visibility')
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
            dst_dir = self.output_toolchain.path / 'runtimes_ndk_cxx'
            shutil.copytree(lib_dir, dst_dir, dirs_exist_ok=True)

    def install(self) -> None:
        # Install libfuzzer headers once for all configs.
        header_src = self.src_dir / 'lib' / 'fuzzer'
        header_dst = self.output_toolchain.path / 'prebuilt_include' / 'llvm' / 'lib' / 'Fuzzer'
        header_dst.mkdir(parents=True, exist_ok=True)
        for f in header_src.iterdir():
            if f.suffix in ('.h', '.def'):
                shutil.copy2(f, header_dst)

        symlink_path = self.output_toolchain.resource_dir / 'libclang_rt.hwasan_static-aarch64-android.a'
        symlink_path.unlink(missing_ok=True)
        os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)


class CompilerRTHostI386Builder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt-i386-host'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = [configs.LinuxConfig(is_32_bit=True)]

    @property
    def install_dir(self) -> Path:
        return self.output_toolchain.clang_lib_dir

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # Due to CMake and Clang oddities, we need to explicitly set
        # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
        # relying on auto-detection from the Compiler-rt CMake files.
        defines['CMAKE_C_COMPILER_TARGET'] = 'i386-linux-gnu'
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
        defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['SANITIZER_CXX_ABI'] = 'libstdc++'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # compiler-rt/lib/gwp_asan uses PRIu64 and similar format-specifier macros.
        # Add __STDC_FORMAT_MACROS so their definition gets included from
        # inttypes.h.  This explicit flag is only needed here.  64-bit host runtimes
        # are built in stage1/stage2 and get it from the LLVM CMake configuration.
        # These are defined unconditionaly in bionic and newer glibc
        # (https://sourceware.org/git/gitweb.cgi?p=glibc.git;h=1ef74943ce2f114c78b215af57c2ccc72ccdb0b7)
        cflags.append('-D__STDC_FORMAT_MACROS')
        cflags.append('--target=i386-linux-gnu')
        cflags.append('-march=i686')
        cflags.append('-Wno-incompatible-pointer-types')
        cflags.append('-Wno-unused-command-line-argument')
        cflags.append('-Wno-unused-variable')
        cflags.append('-Wno-visibility')
        return cflags

    def _build_config(self) -> None:
        # Also remove the "stamps" created for the libcxx included in libfuzzer so
        # CMake runs the configure again (after the cmake caches are deleted).
        stamp_path = self.output_dir / 'lib' / 'fuzzer' / 'libcxx_fuzzer_i386-stamps'
        if stamp_path.exists():
            shutil.rmtree(stamp_path)
        super()._build_config()


class LibOMPBuilder(base_builders.LLVMRuntimeBuilder):
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
        cflags.append('-Wno-unused-variable')
        return cflags

    def install_config(self) -> None:
        # We need to install libomp manually.
        libname = 'libomp.' + ('so' if self.is_shared else 'a')
        src_lib = self.output_dir / 'runtime' / 'src' / libname
        dst_dir = self.install_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_lib, dst_dir / libname)

class SysrootsBuilder(base_builders.Builder):
    name: str = 'sysroots'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    def _build_config(self) -> None:
        config: configs.AndroidConfig = cast(configs.AndroidConfig, self._config)
        arch = config.target_arch
        platform = config.platform
        sysroot = config.sysroot
        if sysroot.exists():
            shutil.rmtree(sysroot)
        sysroot.mkdir(parents=True, exist_ok=True)

        base_header_path = paths.NDK_BASE / 'sysroot' / 'usr' / 'include'
        base_lib_path = paths.NDK_BASE / 'platforms' / f'android-{config.api_level}'
        dest_usr = sysroot / 'usr'

        # Copy over usr/include.
        dest_usr_include = dest_usr / 'include'
        shutil.copytree(base_header_path, dest_usr_include, symlinks=True)

        # Copy over usr/include/asm.
        asm_headers = base_header_path / arch.ndk_triple / 'asm'
        dest_usr_include_asm = dest_usr_include / 'asm'
        shutil.copytree(asm_headers, dest_usr_include_asm, symlinks=True)

        # Copy over usr/lib.
        arch_lib_path = base_lib_path / f'arch-{arch.ndk_arch}' / 'usr' / 'lib'
        dest_usr_lib = dest_usr / 'lib'
        shutil.copytree(arch_lib_path, dest_usr_lib, symlinks=True)

        # For only x86_64, we also need to copy over usr/lib64
        if arch == hosts.Arch.X86_64:
            arch_lib64_path = base_lib_path / f'arch-{arch.ndk_arch}' / 'usr' / 'lib64'
            dest_usr_lib64 = dest_usr / 'lib64'
            shutil.copytree(arch_lib64_path, dest_usr_lib64, symlinks=True)

        if platform:
            # Create a stub library for the platform's libc++.
            platform_stubs = paths.OUT_DIR / 'platform_stubs' / arch.ndk_arch
            platform_stubs.mkdir(parents=True, exist_ok=True)
            libdir = dest_usr_lib64 if arch == hosts.Arch.X86_64 else dest_usr_lib
            with (platform_stubs / 'libc++.c').open('w') as f:
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

            utils.check_call([self.toolchain.cc,
                              f'--target={arch.llvm_triple}',
                              '-fuse-ld=lld', '-nostdlib', '-shared',
                              '-Wl,-soname,libc++.so',
                              '-o{}'.format(libdir / 'libc++.so'),
                              str(platform_stubs / 'libc++.c')])


class PlatformLibcxxAbiBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'platform-libcxxabi'
    src_dir: Path = paths.LLVM_PATH / 'libcxxabi'
    config_list: List[configs.Config] = configs.android_configs(
        platform=True, suppress_libcxx_headers=True)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = str(paths.LLVM_PATH / 'libcxx' / 'include')
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        return defines

    def _is_64bit(self) -> bool:
        return self._config.target_arch in (hosts.Arch.AARCH64, hosts.Arch.X86_64)

    def _build_config(self) -> None:
        if self._is_64bit():
            # For arm64 and x86_64, build static cxxabi library from
            # toolchain/libcxxabi and use it when building runtimes.  This
            # should affect all compiler-rt runtimes that use libcxxabi
            # (e.g. asan, hwasan, scudo, tsan, ubsan, xray).
            super()._build_config()
        else:
            self.install_config()

    def install_config(self) -> None:
        arch = self._config.target_arch
        lib_name = 'lib64' if arch == hosts.Arch.X86_64 else 'lib'
        install_dir = self._config.sysroot / 'usr' / lib_name

        if self._is_64bit():
            src_path = self.output_dir / 'lib64' / 'libc++abi.a'
            shutil.copy2(src_path, install_dir / 'libc++abi.a')
        else:
            with (install_dir / 'libc++abi.so').open('w') as f:
                f.write('INPUT(-lc++)')
