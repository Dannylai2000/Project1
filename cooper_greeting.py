#!/usr/bin/env python3
"""Cooper's greeting for the AgiBot X2 Ultra.

Makes the robot greet everyone and introduce itself as Cooper,
the One Comcentre Ambassador.

The speech backend is pluggable because the exact speech API depends on
which SDK/firmware your X2 Ultra runs:

  * AgiBotSpeaker  - fill in the call to AgiBot's SDK once you have it
                     (check the vendor docs / examples shipped with the robot).
  * Ros2Speaker    - publishes the text on a ROS 2 topic, for robots exposed
                     through ROS 2 (requires rclpy on the robot's onboard PC).
  * LocalTtsSpeaker- speaks through the local sound card with pyttsx3,
                     handy for testing the script on a laptop first.

Usage:
    python3 cooper_greeting.py                 # auto-pick a backend
    python3 cooper_greeting.py --backend local # force local TTS test
    python3 cooper_greeting.py --backend ros2 --topic /x2/tts_request
"""

import argparse
import sys

GREETING = (
    "Hello everyone! "
    "My name is Cooper, and I am the One Comcentre Ambassador. "
    "It's wonderful to meet you all. "
    "I'm here to welcome you and help make your visit a great one!"
)


class AgiBotSpeaker:
    """Speaks through AgiBot's own SDK.

    AgiBot has not published a public API reference for the X2 Ultra, so
    replace the body of speak() with the speech call from the SDK bundle
    that shipped with your robot (look for a tts / audio / interaction
    module in the vendor examples).
    """

    def available(self) -> bool:
        try:
            import agibot_sdk  # noqa: F401  (name may differ in your bundle)
            return True
        except ImportError:
            return False

    def speak(self, text: str) -> None:
        import agibot_sdk

        robot = agibot_sdk.Robot.connect()
        robot.tts.speak(text)  # adjust to the real method in your SDK


class Ros2Speaker:
    """Publishes the greeting on a ROS 2 topic as std_msgs/String.

    Point --topic at whatever topic your robot's speech node subscribes to.
    """

    def __init__(self, topic: str = "/tts_request"):
        self.topic = topic

    def available(self) -> bool:
        try:
            import rclpy  # noqa: F401
            return True
        except ImportError:
            return False

    def speak(self, text: str) -> None:
        import rclpy
        from std_msgs.msg import String

        rclpy.init()
        node = rclpy.create_node("cooper_greeting")
        pub = node.create_publisher(String, self.topic, 10)
        # Give the publisher a moment to match with subscribers.
        rclpy.spin_once(node, timeout_sec=1.0)
        pub.publish(String(data=text))
        rclpy.spin_once(node, timeout_sec=1.0)
        node.destroy_node()
        rclpy.shutdown()


class LocalTtsSpeaker:
    """Speaks through the local sound card (pip install pyttsx3)."""

    def available(self) -> bool:
        try:
            import pyttsx3  # noqa: F401
            return True
        except ImportError:
            return False

    def speak(self, text: str) -> None:
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        engine.say(text)
        engine.runAndWait()


BACKENDS = {
    "agibot": lambda args: AgiBotSpeaker(),
    "ros2": lambda args: Ros2Speaker(topic=args.topic),
    "local": lambda args: LocalTtsSpeaker(),
}


def pick_backend(args):
    if args.backend != "auto":
        return BACKENDS[args.backend](args)
    for name in ("agibot", "ros2", "local"):
        speaker = BACKENDS[name](args)
        if speaker.available():
            print(f"Using '{name}' speech backend.")
            return speaker
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Cooper's greeting for the AgiBot X2 Ultra")
    parser.add_argument(
        "--backend",
        choices=["auto", "agibot", "ros2", "local"],
        default="auto",
        help="speech backend to use (default: auto-detect)",
    )
    parser.add_argument(
        "--topic",
        default="/tts_request",
        help="ROS 2 topic for the ros2 backend (default: /tts_request)",
    )
    args = parser.parse_args()

    speaker = pick_backend(args)
    if speaker is None:
        print("No speech backend found. Install pyttsx3 to test locally,")
        print("or run this on the robot with its SDK / ROS 2 environment sourced.")
        print(f"\nCooper would have said:\n  {GREETING}")
        return 1

    print(f"Cooper says: {GREETING}")
    speaker.speak(GREETING)
    return 0


if __name__ == "__main__":
    sys.exit(main())
