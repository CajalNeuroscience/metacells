#!/usr/bin/env python

import os
import platform
from setuptools import Extension, setup, find_packages

if os.name == "nt":
    raise NotImplementedError(
        "Python metacells does not support native windows.\nInstead, "
        "install Windows Subsystem for Linux, and metacells within it."
    )

DEFINE_MACROS = [("ASSERT_LEVEL", 1)]  # 0 for none, 1 for fast, 2 for slow.
COMPILE_ARGS = [f"-I{os.getcwd()}", "-std=c++14"]
# COMPILE_ARGS += ["-fopt-info-vec-all", "-fopt-info-loop-optimized"]

with open("metacells/should_check_avx2.py", "w") as file:
    file.write("# file generated by setup\n")
    file.write("# don't change, don't track in version control\n")
    if str(os.getenv("WHEEL", "")) == "":
        COMPILE_ARGS += ["-march=native", "-mtune=native"]
        file.write("SHOULD_CHECK_AVX2 = False\n")
    elif platform.processor() == "x86_64":
        COMPILE_ARGS += ["-march=haswell", "-mtune=broadwell"]
        DEFINE_MACROS.append(("USE_AVX2", 1))
        file.write("SHOULD_CHECK_AVX2 = True\n")
    else:
        file.write("SHOULD_CHECK_AVX2 = False\n")

with open("README.rst") as readme_file:
    readme = readme_file.read()

with open("HISTORY.rst") as history_file:
    history = history_file.read()

requirements = open("requirements.txt").read().split()
test_requirements = open("requirements_test.txt").read().split()
dev_requirements = open("requirements_dev.txt").read().split()

setup(
    author="Oren Ben-Kiki",
    author_email="oren@ben-kiki.org",
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Topic :: Software Development :: Libraries",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
    ],
    description="Single-cell RNA Sequencing Analysis",
    headers=["metacells/extensions.h"],
    ext_modules=[
        Extension(  #
            "metacells.extensions",
            include_dirs=["pybind11/include"],
            sources=[
                "metacells/auroc.cpp",
                "metacells/choose_seeds.cpp",
                "metacells/correlate.cpp",
                "metacells/cover.cpp",
                "metacells/downsample.cpp",
                "metacells/extensions.cpp",
                "metacells/folds.cpp",
                "metacells/logistics.cpp",
                "metacells/partitions.cpp",
                "metacells/prune_per.cpp",
                "metacells/rank.cpp",
                "metacells/relayout.cpp",
                "metacells/shuffle.cpp",
                "metacells/top_per.cpp",
            ],
            define_macros=DEFINE_MACROS,
            extra_compile_args=COMPILE_ARGS,
        ),
    ],
    entry_points={
        "console_scripts": [
            "metacells_timing=metacells.scripts.timing:main",
        ]
    },
    install_requires=requirements,
    license="MIT license",
    long_description=readme + "\n\n" + history,
    long_description_content_type="text/x-rst",
    include_package_data=True,
    keywords="metacells",
    name="metacells",
    packages=find_packages(include=["metacells"]),
    test_suite="tests",
    tests_require=test_requirements,
    extras_require={"dev": dev_requirements},
    url="https://github.com/tanaylab/metacells.git",
    version="0.9.0-dev.1",
)
