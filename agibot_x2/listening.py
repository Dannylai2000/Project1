"""Listening (microphone) on/off control for the AgiBot X2.

By default the robot keeps its microphones open so it can respond to the
audience. This module provides a thread-safe controller to disable and
re-enable that listening, so the robot only captures audio when you allow it.

The controller talks to the hardware through a small backend interface.
``MicrophoneBackend`` is an abstract base — plug in the real AgiBot audio
SDK by subclassing it. A simulated backend is included so the toggle can be
developed and tested without the robot.

State is persisted to disk so the robot stays muted across restarts.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path.home() / ".agibot_x2" / "listening_state.json"


class MicrophoneBackend(ABC):
    """Hardware abstraction for the robot's microphone array.

    Subclass this and implement the three methods using the real AgiBot X2
    audio SDK (e.g. its ROS2 audio topic, ALSA device, or vendor API).
    """

    @abstractmethod
    def start_capture(self) -> None:
        """Open the microphone array and begin streaming audio."""

    @abstractmethod
    def stop_capture(self) -> None:
        """Close the microphone array. No audio may be captured after this."""

    @abstractmethod
    def is_capturing(self) -> bool:
        """Return True if audio is currently being captured."""


class SimulatedMicrophone(MicrophoneBackend):
    """In-memory stand-in for the robot's microphone, for development."""

    def __init__(self) -> None:
        self._capturing = False

    def start_capture(self) -> None:
        self._capturing = True
        logger.info("[sim] microphone capture started")

    def stop_capture(self) -> None:
        self._capturing = False
        logger.info("[sim] microphone capture stopped")

    def is_capturing(self) -> bool:
        return self._capturing


class ListeningController:
    """Turn the AgiBot X2's audience listening on and off.

    Usage::

        controller = ListeningController(backend=MyAgiBotBackend())
        controller.stop_listening()   # robot stops capturing audio
        controller.start_listening()  # robot resumes listening
        controller.toggle()           # flip the current state
    """

    def __init__(
        self,
        backend: Optional[MicrophoneBackend] = None,
        state_file: Path = DEFAULT_STATE_FILE,
        on_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self._backend = backend or SimulatedMicrophone()
        self._state_file = state_file
        self._on_change = on_change
        self._lock = threading.Lock()
        # Apply the persisted state so a mute survives a restart.
        self._apply(self._load_persisted_state())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start_listening(self) -> None:
        """Enable listening: the robot resumes capturing audience audio."""
        with self._lock:
            self._apply(True)

    def stop_listening(self) -> None:
        """Disable listening: the robot stops capturing any audio."""
        with self._lock:
            self._apply(False)

    def toggle(self) -> bool:
        """Flip listening state. Returns the new state (True = listening)."""
        with self._lock:
            new_state = not self._backend.is_capturing()
            self._apply(new_state)
            return new_state

    @property
    def is_listening(self) -> bool:
        """True if the robot is currently capturing audio."""
        return self._backend.is_capturing()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _apply(self, listen: bool) -> None:
        if listen:
            self._backend.start_capture()
        else:
            self._backend.stop_capture()
        self._persist_state(listen)
        logger.info("Listening %s", "ENABLED" if listen else "DISABLED")
        if self._on_change is not None:
            self._on_change(listen)

    def _load_persisted_state(self) -> bool:
        try:
            data = json.loads(self._state_file.read_text())
            return bool(data.get("listening", False))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            # Default to NOT listening: fail private, not open.
            return False

    def _persist_state(self, listening: bool) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({"listening": listening}))
        except OSError:
            logger.warning("Could not persist listening state to %s", self._state_file)
