#!/usr/bin/env python3
"""
Transcription Tool using ElevenLabs Scribe v2 Realtime

A command-line tool for transcribing audio/video files AND real-time speech
using ElevenLabs' state-of-the-art Scribe v2 speech-to-text model.

Features:
- Real-time microphone transcription with WebSocket streaming
- File-based transcription for audio/video files
- Speaker diarization (identify who is speaking)
- Audio event tagging (laughter, applause, etc.)
- Multiple output formats (txt, srt, json)
- Automatic language detection (99+ languages supported)
- Voice Activity Detection (VAD) for automatic speech segmentation

Usage:
    # Real-time transcription from microphone
    python transcribe.py --realtime

    # File-based transcription
    python transcribe.py <audio_file> [options]

Examples:
    python transcribe.py --realtime
    python transcribe.py --realtime --language en
    python transcribe.py interview.mp3
    python transcribe.py podcast.wav --diarize --output transcript.txt
"""

import argparse
import asyncio
import base64
import json
import os
import signal
import sys
import threading
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    from elevenlabs import ElevenLabs
    from elevenlabs.realtime.scribe import (
        ScribeRealtime,
        AudioFormat,
        CommitStrategy,
    )
    from elevenlabs.realtime.connection import RealtimeEvents
except ImportError:
    print("Error: elevenlabs package not found or outdated.")
    print("Install it with: pip install elevenlabs>=1.0.0 websockets")
    sys.exit(1)

try:
    import pyaudio
except ImportError:
    pyaudio = None


# ============================================================================
# Configuration
# ============================================================================

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 2048  # Slightly larger chunk for efficient network sending


# ============================================================================
# Utility Functions
# ============================================================================


def get_api_key() -> str:
    """Get API key from environment variable."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print("Error: ELEVENLABS_API_KEY environment variable not set.")
        print("\nTo get your API key:")
        print("1. Go to https://elevenlabs.io/app/settings/api-keys")
        print("2. Create a new API key (free tier: 10k credits/month)")
        print("3. Set it: export ELEVENLABS_API_KEY='your-key-here'")
        sys.exit(1)
    return api_key


def format_timestamp(seconds: float) -> str:
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_as_srt(transcription) -> str:
    """Format transcription as SRT subtitles."""
    lines = []
    counter = 1

    words = getattr(transcription, "words", [])
    if not words:
        return f"1\n00:00:00,000 --> 00:00:30,000\n{transcription.text}\n"

    segments = []
    current_segment = []

    for word in words:
        word_type = getattr(word, "type", "word")
        if word_type == "spacing":
            continue

        current_segment.append(word)

        text = getattr(word, "text", "")
        if text.rstrip().endswith((".", "!", "?", ",")) or len(current_segment) >= 10:
            if current_segment:
                segments.append(current_segment)
                current_segment = []

    if current_segment:
        segments.append(current_segment)

    for segment in segments:
        if not segment:
            continue

        start = getattr(segment[0], "start", 0) or 0
        end = getattr(segment[-1], "end", start + 2) or start + 2

        text_parts = []
        for w in segment:
            word_text = getattr(w, "text", "")
            if word_text:
                text_parts.append(word_text.strip())

        text = " ".join(text_parts)
        if text:
            lines.append(f"{counter}")
            lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
            lines.append(text)
            lines.append("")
            counter += 1

    return "\n".join(lines)


def format_with_speakers(transcription) -> str:
    """Format transcription with speaker labels."""
    words = getattr(transcription, "words", [])
    if not words:
        return transcription.text

    lines = []
    current_speaker = None
    current_text = []

    for word in words:
        word_type = getattr(word, "type", "word")
        if word_type == "spacing":
            continue

        speaker_id = getattr(word, "speaker_id", None)
        word_text = getattr(word, "text", "").strip()

        if not word_text:
            continue

        if speaker_id != current_speaker:
            if current_speaker and current_text:
                speaker_label = current_speaker.upper().replace("_", " ")
                lines.append(f"[{speaker_label}]: {' '.join(current_text)}")

            current_speaker = speaker_id
            current_text = [word_text]
        else:
            current_text.append(word_text)

    if current_speaker and current_text:
        speaker_label = current_speaker.upper().replace("_", " ")
        lines.append(f"[{speaker_label}]: {' '.join(current_text)}")

    return "\n\n".join(lines) if lines else transcription.text


def format_as_json(transcription) -> str:
    """Format transcription as JSON with full details."""
    result = {
        "text": transcription.text,
        "language_code": getattr(transcription, "language_code", None),
        "language_probability": getattr(transcription, "language_probability", None),
    }

    words = getattr(transcription, "words", [])
    if words:
        result["words"] = []
        for word in words:
            word_dict = {
                "text": getattr(word, "text", ""),
                "start": getattr(word, "start", None),
                "end": getattr(word, "end", None),
                "type": getattr(word, "type", "word"),
            }
            speaker_id = getattr(word, "speaker_id", None)
            if speaker_id:
                word_dict["speaker_id"] = speaker_id
            result["words"].append(word_dict)

    return json.dumps(result, indent=2, ensure_ascii=False)


# ============================================================================
# Real-time Transcription
# ============================================================================


class RealtimeTranscriber:
    """Real-time speech-to-text transcription using Scribe v2."""

    def __init__(
        self,
        api_key: str,
        language_code: Optional[str] = None,
        use_vad: bool = True,
        vad_silence_threshold: float = 1.0,
        include_timestamps: bool = False,
        output_file: Optional[str] = None,
    ):
        self.api_key = api_key
        self.language_code = language_code
        self.use_vad = use_vad
        self.vad_silence_threshold = vad_silence_threshold
        self.include_timestamps = include_timestamps
        self.output_file = output_file

        self.audio_queue = queue.Queue()
        self.is_running = False
        self.transcript_lines = []
        self.partial_text = ""

    def _check_dependencies(self):
        """Check if required dependencies are installed."""
        if pyaudio is None:
            print("Error: pyaudio package not found.")
            print("Install it with: pip install pyaudio")
            print("\nOn macOS, you may need: brew install portaudio")
            print("On Ubuntu/Debian: sudo apt-get install portaudio19-dev")
            sys.exit(1)

    def _start_audio_capture(self):
        """Start capturing audio from microphone in a separate thread."""
        p = pyaudio.PyAudio()

        try:
            default_device = p.get_default_input_device_info()
            print(f"Using audio device: {default_device['name']}")
        except IOError:
            print("Error: No audio input device found.")
            sys.exit(1)

        stream = p.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )

        print("Microphone active. Speak now...")

        try:
            while self.is_running:
                try:
                    audio_data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    self.audio_queue.put(audio_data)
                except IOError as e:
                    print(f"Audio read error: {e}")
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

    async def _stream_audio_to_socket(self, connection):
        """Consume audio from queue and send to WebSocket."""
        while self.is_running:
            try:
                # Non-blocking get from queue to allow checking is_running
                try:
                    audio_data = self.audio_queue.get(block=False)
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                audio_b64 = base64.b64encode(audio_data).decode("utf-8")

                # Send to ElevenLabs
                await connection.send({"audio_base_64": audio_b64})

            except Exception as e:
                print(f"Streaming error: {e}")
                break

    async def run(self):
        """Main async loop."""
        self._check_dependencies()
        self.is_running = True

        scribe = ScribeRealtime(api_key=self.api_key)

        # Setup connection options
        options = {
            "model_id": "scribe_v2_realtime",
            "audio_format": AudioFormat.PCM_16000,
            "sample_rate": SAMPLE_RATE,
            "commit_strategy": CommitStrategy.VAD
            if self.use_vad
            else CommitStrategy.MANUAL,
            "vad_silence_threshold_secs": self.vad_silence_threshold
            if self.use_vad
            else None,
            "include_timestamps": self.include_timestamps,
        }

        if self.language_code:
            options["language_code"] = self.language_code

        try:
            connection = await scribe.connect(options)

            # Event Handlers
            def on_open():
                print("Connected to Scribe v2 Realtime")

            def on_partial(data):
                text = data.get("text", "")
                if text:
                    self.partial_text = text
                    print(f"\r[...] {text}", end="", flush=True)

            def on_committed(data):
                text = data.get("text", "")
                if text:
                    # Clear partial line
                    print(f"\r{' ' * (len(self.partial_text) + 10)}", end="")
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    print(f"\r[{timestamp}] {text}")
                    self.transcript_lines.append(text)
                    self.partial_text = ""

            def on_error(data):
                print(f"\nError: {data}")

            connection.on(RealtimeEvents.OPEN, on_open)
            connection.on(RealtimeEvents.PARTIAL_TRANSCRIPT, on_partial)
            connection.on(RealtimeEvents.COMMITTED_TRANSCRIPT, on_committed)
            if self.include_timestamps:
                connection.on(
                    RealtimeEvents.COMMITTED_TRANSCRIPT_WITH_TIMESTAMPS, on_committed
                )
            connection.on(RealtimeEvents.ERROR, on_error)

            # Start Audio Capture (Thread)
            audio_thread = threading.Thread(
                target=self._start_audio_capture, daemon=True
            )
            audio_thread.start()

            print("=" * 60)
            print("REAL-TIME TRANSCRIPTION")
            print("=" * 60)
            print(
                f"Language: {'auto-detect' if not self.language_code else self.language_code}"
            )
            print(f"VAD: {'enabled' if self.use_vad else 'disabled (manual commit)'}")
            print("Press Ctrl+C to stop")
            print("=" * 60)
            print()

            # Start Streaming (Async)
            await self._stream_audio_to_socket(connection)

        except Exception as e:
            print(f"\nConnection error: {e}")
        finally:
            self.is_running = False
            # Save transcript
            if self.output_file and self.transcript_lines:
                full_transcript = "\n".join(self.transcript_lines)
                Path(self.output_file).write_text(full_transcript, encoding="utf-8")
                print(f"\nTranscript saved to: {self.output_file}")


def realtime_transcribe(
    language_code: Optional[str] = None,
    use_vad: bool = True,
    vad_silence_threshold: float = 1.0,
    include_timestamps: bool = False,
    output_file: Optional[str] = None,
):
    """Start real-time transcription."""
    api_key = get_api_key()
    transcriber = RealtimeTranscriber(
        api_key=api_key,
        language_code=language_code,
        use_vad=use_vad,
        vad_silence_threshold=vad_silence_threshold,
        include_timestamps=include_timestamps,
        output_file=output_file,
    )

    try:
        asyncio.run(transcriber.run())
    except KeyboardInterrupt:
        print("\nStopping...")


# ============================================================================
# File-based Transcription
# ============================================================================


def transcribe_file(
    file_path: str,
    language_code: Optional[str] = None,
    diarize: bool = False,
    tag_audio_events: bool = True,
    output_format: str = "txt",
    num_speakers: Optional[int] = None,
) -> str:
    """Transcribe an audio/video file using ElevenLabs Scribe v2."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    client = ElevenLabs(api_key=get_api_key())

    print(f"Reading file: {file_path}")
    with open(path, "rb") as f:
        audio_bytes = f.read()

    file_size_mb = len(audio_bytes) / (1024 * 1024)
    print(f"File size: {file_size_mb:.2f} MB")

    print("Transcribing with Scribe v2...")
    print(f"  - Language: {'auto-detect' if not language_code else language_code}")
    print(f"  - Diarization: {'enabled' if diarize else 'disabled'}")
    print(f"  - Audio events: {'enabled' if tag_audio_events else 'disabled'}")

    transcription = client.speech_to_text.convert(
        model_id="scribe_v2",
        file=audio_bytes,
        language_code=language_code if language_code else None,
        diarize=diarize,
        tag_audio_events=tag_audio_events,
        num_speakers=num_speakers,
        timestamps_granularity="word",
    )

    detected_lang = getattr(transcription, "language_code", "unknown")
    lang_prob = getattr(transcription, "language_probability", 0)
    print(f"Detected language: {detected_lang} (confidence: {lang_prob:.1%})")

    if output_format == "srt":
        return format_as_srt(transcription)
    elif output_format == "json":
        return format_as_json(transcription)
    elif diarize:
        return format_with_speakers(transcription)
    else:
        return transcription.text


# ============================================================================
# CLI Entry Point
# ============================================================================


def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Transcribe audio/video files or real-time speech using ElevenLabs Scribe v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode selection
    parser.add_argument(
        "--realtime",
        "-r",
        action="store_true",
        help="Enable real-time transcription from microphone",
    )

    # File argument (optional if --realtime)
    parser.add_argument(
        "file",
        nargs="?",
        help="Path to the audio/video file to transcribe (not needed with --realtime)",
    )

    # Common options
    parser.add_argument(
        "-o", "--output", help="Output file path (prints to stdout if not specified)"
    )

    parser.add_argument(
        "-l",
        "--language",
        help="Language code (e.g., 'en', 'es', 'fr'). Auto-detected if not specified.",
    )

    # File-only options
    parser.add_argument(
        "-f",
        "--format",
        choices=["txt", "srt", "json"],
        default="txt",
        help="Output format for file transcription (default: txt)",
    )

    parser.add_argument(
        "-d",
        "--diarize",
        action="store_true",
        help="Enable speaker diarization (file mode only)",
    )

    parser.add_argument(
        "-s",
        "--speakers",
        type=int,
        help="Expected number of speakers for diarization (file mode only)",
    )

    parser.add_argument(
        "--no-events",
        action="store_true",
        help="Disable audio event tagging (file mode only)",
    )

    # Real-time options
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="Disable Voice Activity Detection (realtime mode)",
    )

    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=1.0,
        help="Silence threshold in seconds for VAD (default: 1.0)",
    )

    parser.add_argument(
        "--timestamps",
        action="store_true",
        help="Include word-level timestamps (realtime mode)",
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="%(prog)s 2.0.0 (using ElevenLabs Scribe v2)",
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.realtime and not args.file:
        parser.error("Either --realtime or a file path is required")

    try:
        if args.realtime:
            # Real-time transcription mode
            realtime_transcribe(
                language_code=args.language,
                use_vad=not args.no_vad,
                vad_silence_threshold=args.vad_threshold,
                include_timestamps=args.timestamps,
                output_file=args.output,
            )
        else:
            # File-based transcription mode
            result = transcribe_file(
                file_path=args.file,
                language_code=args.language,
                diarize=args.diarize,
                tag_audio_events=not args.no_events,
                output_format=args.format,
                num_speakers=args.speakers,
            )

            # Output for file mode
            if args.output:
                output_path = Path(args.output)
                output_path.write_text(result, encoding="utf-8")
                print(f"\nTranscription saved to: {args.output}")
            else:
                print("\n" + "=" * 60)
                print("TRANSCRIPTION")
                print("=" * 60)
                print(result)

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
