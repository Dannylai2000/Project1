"""Tests for the AgiBot X2 listening toggle."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agibot_x2.listening import ListeningController, SimulatedMicrophone


def make_controller(tmp_path, **kwargs):
    return ListeningController(
        backend=SimulatedMicrophone(),
        state_file=tmp_path / "state.json",
        **kwargs,
    )


def test_defaults_to_not_listening(tmp_path):
    controller = make_controller(tmp_path)
    assert not controller.is_listening


def test_on_and_off(tmp_path):
    controller = make_controller(tmp_path)
    controller.start_listening()
    assert controller.is_listening
    controller.stop_listening()
    assert not controller.is_listening


def test_toggle(tmp_path):
    controller = make_controller(tmp_path)
    assert controller.toggle() is True
    assert controller.is_listening
    assert controller.toggle() is False
    assert not controller.is_listening


def test_state_persists_across_restart(tmp_path):
    state_file = tmp_path / "state.json"
    first = ListeningController(backend=SimulatedMicrophone(), state_file=state_file)
    first.start_listening()

    second = ListeningController(backend=SimulatedMicrophone(), state_file=state_file)
    assert second.is_listening

    second.stop_listening()
    third = ListeningController(backend=SimulatedMicrophone(), state_file=state_file)
    assert not third.is_listening


def test_on_change_callback(tmp_path):
    events = []
    controller = make_controller(tmp_path, on_change=events.append)
    controller.start_listening()
    controller.stop_listening()
    assert events[-2:] == [True, False]
