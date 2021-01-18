#!/bin/bash
#
# Example: ./get-llvm-revision.sh 54176d1766f25bc03ddb1a8932a380f6543d5150
#
red="\033[1;31m"
cyan="\033[36m"
reset="\033[0m"

error_message() {
  echo -e $red"ERROR: Couln't get current upstream revision."$reset
  echo -e $cyan"Make sure you have remote upstream set as:"$reset
  echo "https://github.com/llvm/llvm-project"
  echo -e $cyan"and have run:"$reset "git fetch"
}

if [ "$1" == "" ]; then
  script_path="$(dirname "$(readlink -f "$0")")"
  python3 $script_path/../../external/toolchain-utils/llvm_tools/git_llvm_rev.py --llvm_dir $script_path/../llvm-project --sha upstream/main
  if [ $? -ne 0 ]; then
    error_message
  fi
else
  script_path="$(dirname "$(readlink -f "$0")")"
  python3 $script_path/../../external/toolchain-utils/llvm_tools/git_llvm_rev.py --llvm_dir $script_path/../llvm-project --upstream upstream --sha $1
  if [ $? -ne 0 ]; then
    error_message
  fi
fi

