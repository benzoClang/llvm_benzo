benzoClang - Custom AOSP LLVM with Polly
------------------------

How to build:
```
$ mkdir benzoClang && cd benzoClang
$ repo init -u https://github.com/benzoClang/manifest -b master
$ repo sync -j$(nproc --all) -c
$ python toolchain/llvm_benzo/build.py
```

For a quicker build with ThinLTO disabled run:
```
$ toolchain/llvm_benzo/build.py --no-lto
```
To enable ccache, skip runtimes etc, read the help menu:
```
$ python toolchain/llvm_benzo/build.py --help
```
