import math

import pytest

from qemu_mcp.mouse import coordinates, move_events, validate_button


def test_pixel_and_normalized_conversion():
    assert coordinates(99, 49, 100, 50, 'pixels') == (1, 1)
    assert coordinates(0.5, 0.25, 100, 50, 'normalized') == (0.5, 0.25)
    assert coordinates(0, 0, 1, 1, 'pixels') == (0, 0)
    assert move_events(0, 1) == [{'type': 'abs', 'data': {'axis': 'x', 'value': 0}}, {'type': 'abs', 'data': {'axis': 'y', 'value': 32767}}]


@pytest.mark.parametrize('value', [-0.1, 1.1, math.nan, math.inf])
def test_invalid_normalized(value):
    with pytest.raises(ValueError):
        move_events(value, 0)


def test_invalid_button():
    with pytest.raises(ValueError):
        validate_button('bad')
