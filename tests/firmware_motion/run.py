"""固件运动算法检查：在电脑上用桩头文件编译 firmware/.../panda_motion.cc，喂模拟的传感器数据
（静止 → 攀爬 → 下落 → 重摔 → 不动），检查算出的下落高度、冲击、攀爬 / 休息时长、晃动和报警。
需要 clang++ 或 g++。运行：python tests/firmware_motion/run.py"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "firmware/main/boards/m5stack/stick-s3"
cxx = shutil.which("clang++") or shutil.which("g++")
if not cxx:
    sys.exit("没有 C++ 编译器（clang++ / g++）")
exe = Path(tempfile.mkdtemp()) / "motion_test"
subprocess.run([cxx, "-std=c++17", "-Wall", "-I", str(HERE / "stub"), "-I", str(SRC), "-include", "cstring",
                str(HERE / "test_motion.cc"), str(SRC / "panda_motion.cc"), "-o", str(exe)], check=True)
sys.exit(subprocess.run([str(exe)]).returncode)
