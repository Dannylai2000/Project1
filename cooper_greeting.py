#!/usr/bin/env python3
"""Cooper's greeting for the AgiBot X2 Ultra.

Makes the robot wave and greet everyone while introducing itself as
Cooper, the One Comcentre Ambassador. The wave runs concurrently with
the speech so Cooper gestures while he talks.

The robot backend is pluggable because the exact speech/motion API
depends on which SDK/firmware your X2 Ultra runs:

  * AgiBotRobot  - fill in the calls to AgiBot's SDK once you have it
                   (check the vendor docs / examples shipped with the robot).
  * Ros2Robot    - publishes text and gesture commands on ROS 2 topics,
                   for robots exposed through ROS 2 (requires rclpy).
  * LocalRobot   - speaks on the local machine and prints the wave,
                   handy for testing on a laptop first. Prefers the
                   natural-sounding Piper neural voice when available,
                   falling back to pyttsx3/eSpeak.

Local Piper voice setup (natural, friendly, fully offline):
    pip install piper-tts
    curl -sSL -o ryan.tar.gz \\
      https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-en-us-ryan-high.tar.gz
    mkdir -p voices && tar xzf ryan.tar.gz -C voices

Usage:
    python3 cooper_greeting.py                 # auto-pick a backend
    python3 cooper_greeting.py --backend local # force local TTS test
    python3 cooper_greeting.py --voice-model voices/en-us-ryan-high.onnx
    python3 cooper_greeting.py --backend ros2 --tts-topic /x2/tts_request \\
                               --gesture-topic /x2/gesture_request
"""

import argparse
import sys
import threading

GREETING = (
    "Hello everyone! "
    "My name is Cooper, and I am the One Comcentre Ambassador. "
    "It's wonderful to meet you all. "
    "I'm here to welcome you and help make your visit a great one!"
)

WAVE_GESTURE = "wave"


class AgiBotRobot:
    """Drives the robot through AgiBot's own SDK.

    AgiBot has not published a public API reference for the X2 Ultra, so
    replace the bodies of speak() and wave() with the calls from the SDK
    bundle that shipped with your robot (look for tts / audio and
    motion / gesture modules in the vendor examples).
    """

    def available(self) -> bool:
        try:
            import agibot_sdk  # noqa: F401  (name may differ in your bundle)
            return True
        except ImportError:
            return False

    def connect(self):
        import agibot_sdk

        return agibot_sdk.Robot.connect()

    def speak(self, text: str) -> None:
        robot = self.connect()
        robot.tts.speak(text)  # adjust to the real method in your SDK

    def wave(self) -> None:
        robot = self.connect()
        # Most humanoid SDKs ship a library of named motions; adjust to
        # the real call in your SDK, e.g. robot.motion.play("wave") or a
        # joint-trajectory command for the right arm.
        robot.motion.play(WAVE_GESTURE)


class Ros2Robot:
    """Publishes the greeting and gesture on ROS 2 topics as std_msgs/String.

    Point --tts-topic / --gesture-topic at whatever topics your robot's
    speech and motion nodes subscribe to.
    """

    def __init__(self, tts_topic: str = "/tts_request",
                 gesture_topic: str = "/gesture_request"):
        self.tts_topic = tts_topic
        self.gesture_topic = gesture_topic

    def available(self) -> bool:
        try:
            import rclpy  # noqa: F401
            return True
        except ImportError:
            return False

    def _publish(self, node, topic: str, text: str) -> None:
        import rclpy
        from std_msgs.msg import String

        pub = node.create_publisher(String, topic, 10)
        # Give the publisher a moment to match with subscribers.
        rclpy.spin_once(node, timeout_sec=1.0)
        pub.publish(String(data=text))
        rclpy.spin_once(node, timeout_sec=1.0)

    def speak(self, text: str) -> None:
        self._run(lambda node: self._publish(node, self.tts_topic, text))

    def wave(self) -> None:
        self._run(lambda node: self._publish(node, self.gesture_topic, WAVE_GESTURE))

    def _run(self, action) -> None:
        import rclpy

        rclpy.init()
        node = rclpy.create_node("cooper_greeting")
        try:
            action(node)
        finally:
            node.destroy_node()
            rclpy.shutdown()


class LocalRobot:
    """Speaks on the local machine, preferring a natural neural voice.

    Uses Piper TTS (pip install piper-tts) with a downloaded .onnx voice
    model when available - see the module docstring for setup. Falls
    back to pyttsx3/eSpeak if Piper or the model is missing.

    There is no arm to wave on a laptop, so the wave is simulated with a
    console message - useful for checking the concurrent flow.
    """

    def __init__(self, voice_model: str = ""):
        self.voice_model = voice_model or self._find_voice_model()

    @staticmethod
    def _find_voice_model() -> str:
        import glob
        import os

        here = os.path.dirname(os.path.abspath(__file__))
        models = sorted(glob.glob(os.path.join(here, "voices", "*.onnx")))
        return models[0] if models else ""

    def _piper_ready(self) -> bool:
        import os

        if not (self.voice_model and os.path.exists(self.voice_model)):
            return False
        try:
            import piper  # noqa: F401
            return True
        except ImportError:
            return False

    def available(self) -> bool:
        if self._piper_ready():
            return True
        try:
            import pyttsx3  # noqa: F401
            return True
        except ImportError:
            return False

    def speak(self, text: str) -> None:
        if self._piper_ready():
            self._speak_piper(text)
        else:
            self._speak_pyttsx3(text)

    def _speak_piper(self, text: str) -> None:
        """Synthesize with Piper's neural voice and play it back.

        Playback goes through the first audio player found on the
        system; with none available (e.g. a headless box) the audio is
        kept as cooper_greeting_output.wav next to this script.
        """
        import os
        import shutil
        import subprocess
        import sys

        out_wav = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "cooper_greeting_output.wav"
        )
        subprocess.run(
            [sys.executable, "-m", "piper", "-m", self.voice_model, "-f", out_wav],
            input=text.encode(),
            check=True,
        )
        for player in ("paplay", "aplay", "ffplay"):
            exe = shutil.which(player)
            if exe:
                cmd = [exe, "-nodisp", "-autoexit", out_wav] if player == "ffplay" \
                    else [exe, out_wav]
                subprocess.run(cmd, check=False)
                return
        print(f"(no audio player found - synthesized speech saved to {out_wav})")

    def _speak_pyttsx3(self, text: str) -> None:
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        engine.say(text)
        engine.runAndWait()

    def wave(self) -> None:
        print("(Cooper waves) o/")


BACKENDS = {
    "agibot": lambda args: AgiBotRobot(),
    "ros2": lambda args: Ros2Robot(tts_topic=args.tts_topic,
                                   gesture_topic=args.gesture_topic),
    "local": lambda args: LocalRobot(voice_model=args.voice_model),
}


def pick_backend(args):
    if args.backend != "auto":
        return BACKENDS[args.backend](args)
    for name in ("agibot", "ros2", "local"):
        robot = BACKENDS[name](args)
        if robot.available():
            print(f"Using '{name}' backend.")
            return robot
    return None


def greet_with_wave(robot) -> None:
    """Start the wave, then speak while the arm is moving."""
    wave_thread = threading.Thread(target=robot.wave, name="cooper-wave")
    wave_thread.start()
    robot.speak(GREETING)
    wave_thread.join()


def main() -> int:
    parser = argparse.ArgumentParser(description="Cooper's greeting for the AgiBot X2 Ultra")
    parser.add_argument(
        "--backend",
        choices=["auto", "agibot", "ros2", "local"],
        default="auto",
        help="robot backend to use (default: auto-detect)",
    )
    parser.add_argument(
        "--tts-topic",
        default="/tts_request",
        help="ROS 2 topic for speech (default: /tts_request)",
    )
    parser.add_argument(
        "--gesture-topic",
        default="/gesture_request",
        help="ROS 2 topic for gestures (default: /gesture_request)",
    )
    parser.add_argument(
        "--voice-model",
        default="",
        help="path to a Piper .onnx voice model for the local backend "
             "(default: first model found in ./voices/)",
    )
    args = parser.parse_args()

    robot = pick_backend(args)
    if robot is None:
        print("No robot backend found. Install pyttsx3 to test locally,")
        print("or run this on the robot with its SDK / ROS 2 environment sourced.")
        print(f"\nCooper would have waved and said:\n  {GREETING}")
        return 1

    print(f"Cooper waves and says: {GREETING}")
    greet_with_wave(robot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
