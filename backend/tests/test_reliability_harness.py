"""The live harness's AST allowlist must reject code beyond arithmetic."""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('reliability_live', Path(__file__).resolve().parents[2] / 'scripts' / 'reliability_live.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_fixed_arithmetic_harness_executes_only_its_five_cases():
    assert module.check_arithmetic_fixture(b'def add(a,b):\n return a+b\n', 'add')['cases'] == 5


@pytest.mark.parametrize('source', [
    b'import os\ndef add(a,b): return a+b',
    b'def add(a,b): return __import__("os").getcwd()',
    b'@print("unsafe")\ndef add(a,b): return a+b',
    b'def add(a,b=print("unsafe")): return a+b',
])
def test_harness_rejects_imports_calls_decorators_and_defaults(source):
    with pytest.raises(AssertionError):
        module.check_arithmetic_fixture(source, 'add')
