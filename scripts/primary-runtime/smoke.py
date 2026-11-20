"""Exercise the relocated medical calculation payload with its own isolated interpreter."""

import csv
import importlib.metadata
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import numpy
# NumPy testing helpers require this native extension even without upstream test directories.
import numpy._core._multiarray_tests
import pandas
from PIL import Image


def check_interpreters():
    """Exercise command aliases, child execution and a pip-enabled virtual environment."""
    executable = Path(sys.executable)
    names = [executable.name] if sys.platform == "win32" else ["python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}"]
    for name in names:
        interpreter = executable.with_name(name)
        subprocess.run([
            str(interpreter), "-I", "-B", "-c",
            "import subprocess, sys; from pathlib import Path; "
            "assert Path(sys.executable) == Path(sys.argv[1]); "
            "subprocess.run([sys.executable, '-I', '-B', '-c', 'import ssl, sqlite3, ctypes'], check=True, timeout=30)",
            str(interpreter),
        ], check=True, timeout=60)
    with tempfile.TemporaryDirectory(prefix="dsh python venv ") as directory:
        environment = Path(directory) / "environment"
        subprocess.run([str(executable.with_name(names[0])), "-I", "-B", "-m", "venv", str(environment)], check=True, timeout=60)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run([str(python), "-I", "-B", "-c", "import sys; assert sys.prefix != sys.base_prefix"], check=True, timeout=30)
        subprocess.run([str(python), "-I", "-B", "-m", "pip", "check"], check=True, timeout=30)


def main():
    """Check installed versions and exercise numerical analysis and CSV data."""
    assert sys.version_info[:3] == tuple(map(int, sys.argv[2].split("."))), sys.version
    check_interpreters()
    versions = json.loads(sys.argv[1])
    # Only pip belongs to the interpreter baseline; all other distributions must be declared.
    # Each target's native release-host smoke must confirm this baseline.
    expected_names = {re.sub(r"[-_.]+", "-", name).lower() for name in versions} | {"pip"}
    installed_names = {
        re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        for distribution in importlib.metadata.distributions()
    }
    assert installed_names == expected_names, {"unexpected": sorted(installed_names - expected_names), "missing": sorted(expected_names - installed_names)}
    for name, expected in versions.items():
        actual = importlib.metadata.version(name)
        assert actual == expected, (name, actual, expected)
    assert numpy.arange(4).sum() == 6
    assert pandas.DataFrame({"n": [1, 2]}).n.sum() == 3
    numpy.testing.assert_allclose(numpy.arange(4), [0, 1, 2, 3])
    pandas.testing.assert_frame_equal(pandas.DataFrame({"n": [1, 2]}), pandas.DataFrame({"n": [1, 2]}))
    for library in (numpy, pandas):
        root = Path(library.__file__).parent
        assert not any(path.is_dir() for path in root.rglob("tests")), root
        distribution = importlib.metadata.distribution(library.__name__)
        record = distribution.read_text("RECORD")
        assert record, library.__name__
        for file, _hash, _size in csv.reader(io.StringIO(record)):
            assert distribution.locate_file(file).is_file(), file

    with tempfile.TemporaryDirectory(prefix="dsh-medical-smoke-") as directory:
        image = Path(directory) / "sample.png"
        Image.new("RGB", (16, 16), "white").save(image)
        with Image.open(image) as loaded:
            assert loaded.size == (16, 16)
    print("Python, NumPy, Pandas, CSV and image runtime checks passed.")


if __name__ == "__main__":
    main()
