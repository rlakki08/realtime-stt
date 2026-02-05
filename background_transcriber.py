#!/usr/bin/env python3
"""
Background Real-time Transcriber with Push-to-Talk

This script runs in the background and listens for a specific key press (e.g., F8).
When the key is held down, it captures audio and streams it to ElevenLabs Scribe v2.
When the key is released, it stops capturing and types the transcription into the active window.

Features:
- Global hotkey support (Push-to-Talk)
- Types directly into active application
- Audio cues for start/stop recording

Usage:
    python background_transcriber.py [--key F8]
"""

import argparse
import asyncio
import base64
import os
import subprocess
import sys
import threading
import queue
import time
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    import pyaudio
    from pynput import keyboard
    from pynput.keyboard import Controller, Key
    from elevenlabs.realtime.scribe import (
        ScribeRealtime,
        AudioFormat,
        CommitStrategy,
    )
    from elevenlabs.realtime.connection import RealtimeEvents
except ImportError as e:
    print(f"Error: Missing dependency: {e}")
    print("Install dependencies with: pip install -r requirements.txt")
    sys.exit(1)

# ============================================================================
# Configuration
# ============================================================================

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 2048


# ============================================================================
# Audio Capture & Streaming
# ============================================================================


class AudioStreamer:
    def __init__(self, api_key: str, type_text: bool = True):
        self.api_key = api_key
        self.should_type = type_text
        self.is_recording = False
        self.audio_queue = queue.Queue()
        self.keyboard_controller = Controller()

        # Thread management
        self.session_thread = None

    def start_recording(self):
        """Start capturing audio."""
        if self.is_recording:
            return

        print("🔴 Recording...")
        self.is_recording = True
        self.audio_queue = queue.Queue()

        # Start the async session in a new thread
        self.session_thread = threading.Thread(target=self._run_session, daemon=True)
        self.session_thread.start()

    def stop_recording(self):
        """Stop capturing audio."""
        if not self.is_recording:
            return

        print("⚫ Stopped recording...")
        self.is_recording = False
        # The session thread will finish when is_recording is False and queue is empty(ish)

    def _run_session(self):
        """Entry point for the session thread."""
        try:
            asyncio.run(self._async_session())
        except Exception as e:
            print(f"Session error: {e}")

    def _capture_audio(self):
        """Capture audio from microphone (runs in a thread)."""
        p = pyaudio.PyAudio()
        stream = None
        try:
            stream = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
            )

            while self.is_recording:
                data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                self.audio_queue.put(data)

        except Exception as e:
            print(f"Audio capture error: {e}")
        finally:
            # Signal end of stream
            self.audio_queue.put(None)
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except:
                    pass
            p.terminate()

    async def _async_session(self):
        """Async session logic."""
        scribe = ScribeRealtime(api_key=self.api_key)
        transcript_parts = []

        try:
            # Connect to Scribe v2
            connection = await scribe.connect(
                {
                    "model_id": "scribe_v2_realtime",
                    "audio_format": AudioFormat.PCM_16000,
                    "sample_rate": SAMPLE_RATE,
                    "commit_strategy": CommitStrategy.MANUAL,  # Manual commit on key release
                }
            )

            # Event Handlers
            def on_committed(data):
                text = data.get("text", "")
                if text:
                    print(f"📝 Transcribed: {text}")
                    transcript_parts.append(text)

            def on_error(data):
                print(f"Error: {data}")

            connection.on(RealtimeEvents.COMMITTED_TRANSCRIPT, on_committed)
            connection.on(RealtimeEvents.ERROR, on_error)

            # Start mic capture in a background thread
            mic_thread = threading.Thread(target=self._capture_audio, daemon=True)
            mic_thread.start()

            # Stream audio loop
            while True:
                try:
                    # Non-blocking check
                    data = self.audio_queue.get(timeout=0.05)

                    if data is None:  # Sentinel for end of recording
                        break

                    audio_b64 = base64.b64encode(data).decode("utf-8")
                    await connection.send({"audio_base_64": audio_b64})

                except queue.Empty:
                    # Check if we should stop
                    if not self.is_recording and not mic_thread.is_alive():
                        break
                    continue

            # Key released: Commit and Close
            print("Committing...")
            await connection.commit()

            # Give a moment for final transcript to arrive
            # Wait until we get the response (or timeout)
            # Since Scribe is fast, a short sleep or wait loop is usually enough.
            # Ideally we wait for a specific event, but "Committed" comes after commit().
            await asyncio.sleep(0.5)

            await connection.close()

            # Type the result
            if self.should_type and transcript_parts:
                full_text = " ".join(transcript_parts) + " "
                self._type_text(full_text)

        except Exception as e:
            print(f"Connection/Streaming failed: {e}")

    def _type_text(self, text):
        """Simulate keyboard typing."""
        print(f"⌨️  Typing: {text.strip()}")

        # On macOS, pynput often has issues sending keystrokes to other applications
        # due to sandbox/accessibility restrictions. AppleScript is more reliable.
        if sys.platform == "darwin":
            try:
                # Use osascript to type text
                # We need to escape double quotes and backslashes for AppleScript
                safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
                script = f'tell application "System Events" to keystroke "{safe_text}"'

                # Run the AppleScript
                result = subprocess.run(
                    ["osascript", "-e", script], capture_output=True, text=True
                )

                if result.returncode != 0:
                    print(f"⚠️  AppleScript typing failed: {result.stderr.strip()}")
                    if "not allowed to send keystrokes" in result.stderr:
                        print(
                            "\n🛑 PERMISSION ERROR: Your terminal needs 'Accessibility' permission to type."
                        )
                        print(
                            "1. Open System Settings -> Privacy & Security -> Accessibility"
                        )
                        print(
                            "2. Click '+' and add your terminal application (VS Code, iTerm, or Terminal)"
                        )
                        print("3. Toggle the switch ON.")
                        print(
                            "4. IMPORTANT: Restart your terminal app for changes to apply.\n"
                        )
                    return  # Don't fallback to pynput if we know it's a permission issue likely affecting both
                return
            except Exception as e:
                print(f"⚠️  AppleScript typing failed: {e}. Falling back to pynput.")

        # Fallback for other platforms or if AppleScript fails
        for char in text:
            self.keyboard_controller.type(char)
            # time.sleep(0.005) # Tiny delay if needed


# ============================================================================
# Keyboard Listener
# ============================================================================


class PushToTalkListener:
    def __init__(self, streamer: AudioStreamer, trigger_key: str = "f8"):
        self.streamer = streamer
        self.trigger_key = trigger_key.lower()
        self.is_pressed = False

    def on_press(self, key):
        try:
            # Check if key matches trigger (handle special keys vs chars)
            k = getattr(key, "name", None) or getattr(key, "char", None)
            if str(k).lower() == self.trigger_key:
                if not self.is_pressed:
                    self.is_pressed = True
                    self.streamer.start_recording()
        except AttributeError:
            pass

    def on_release(self, key):
        try:
            k = getattr(key, "name", None) or getattr(key, "char", None)
            if str(k).lower() == self.trigger_key:
                if self.is_pressed:
                    self.is_pressed = False
                    self.streamer.stop_recording()
        except AttributeError:
            pass

    def start(self):
        print(f"✨ Background Transcriber Ready")
        print(f"🎤 Hold '{self.trigger_key.upper()}' to speak")
        print("⌨️  Releasing key will stop recording and type text")

        if self.trigger_key == "fn":
            print(
                "⚠️  NOTE: The 'Fn' key is not detectable by standard Python libraries on macOS."
            )
            print("    Please use a different key (like 'alt_r' or 'cmd_r').")

        if not self.streamer.should_type:
            print("🚫 Direct typing disabled (testing mode)")

        print("\nNOTE: If you see a 'trusted process' error, enable Input Monitoring:")
        print(
            "      System Settings -> Privacy & Security -> Input Monitoring -> Add your Terminal"
        )
        print("❌ Press Ctrl+C to exit")

        with keyboard.Listener(
            on_press=self.on_press, on_release=self.on_release
        ) as listener:
            listener.join()


# ============================================================================
# Main
# ============================================================================


def get_api_key() -> str:
    """Get API key from environment variable."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print("Error: ELEVENLABS_API_KEY environment variable not set.")
        sys.exit(1)
    return api_key


def main():
    parser = argparse.ArgumentParser(description="Push-to-Talk Transcriber")
    parser.add_argument(
        "--key",
        default="alt_r",
        help="Key to hold for recording (default: alt_r). Options: f1-f12, alt_r, ctrl_r, shift_r, cmd_r",
    )
    parser.add_argument(
        "--no-type",
        action="store_true",
        help="Disable direct typing (print to console only)",
    )
    args = parser.parse_args()

    api_key = get_api_key()
    streamer = AudioStreamer(api_key, type_text=not args.no_type)
    listener = PushToTalkListener(streamer, trigger_key=args.key)

    try:
        listener.start()
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
