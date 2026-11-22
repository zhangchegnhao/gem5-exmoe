# Offline reproduction

Requires Python3.10+, PyYAML, CMake and GNU C++12 on a compatible Linux system.

Run `python3 reproduce_release.py /absolute/path/to/this/bundle /absolute/path/to/new-workspace`.
The destination MUST NOT exist. Archives and reconstructed sources are hash-checked;
then a fresh Release build and the full engineering acceptance matrix run offline.
Dependencies and their licenses are included; no Git clone or network fetch is needed.

This bundle implements a declared engineering profile. Paper-fidelity acceptance
remains blocked by Q1-Q5; a successful build does not resolve those questions.
