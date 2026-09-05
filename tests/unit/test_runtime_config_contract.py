from pathlib import Path

import pytest

from tests.support.makefiles import read_make_variable_defaults


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_config_path_is_derived_from_runtime_root() -> None:
    values = read_make_variable_defaults(ROOT)

    relative_config_path = Path(values["RUNTIME_CONFIG_RELATIVE_PATH"])

    assert not relative_config_path.is_absolute()
    assert values["CONFIG_PRIVATE_PATH"] == "$(RUNTIME_ROOT)/$(RUNTIME_CONFIG_RELATIVE_PATH)"
    assert values["CONFIG_PATH"] == "$(CONFIG_PRIVATE_PATH)"
