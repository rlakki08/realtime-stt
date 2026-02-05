# Agent Guide (`AGENTS.md`)

This file contains guidelines for AI agents and developers working on the ElevenLabs Scribe v2 Transcription Tool codebase.

## 1. Environment & Build

### Dependencies
This project is a Python application. It does not use a complex build system.
- **Python Version**: Python 3.
- **Dependencies**: Managed via `requirements.txt`.
- **Installation**:
  ```bash
  # Install uv (if not already installed)
  curl -LsSf https://astral.sh/uv/install.sh | sh
  
  # Create virtual environment and install dependencies
  uv venv
  source .venv/bin/activate
  uv pip install -r requirements.txt
  ```
- **System Dependencies**:
  - `pyaudio` may require system-level libraries:
    - macOS: `brew install portaudio`
    - Ubuntu/Debian: `sudo apt-get install portaudio19-dev`

### Environment Variables
- `ELEVENLABS_API_KEY`: Required for all operations. Get it from [ElevenLabs](https://elevenlabs.io/app/settings/api-keys).
- This project supports `.env` files. Copy `.env` and add your key: `ELEVENLABS_API_KEY=your_key`.

### Commands (Manual Testing & Usage)
Since there are no automated tests, verify changes by running the tools directly:

1.  **Transcribe Real-time (Mic)**:
    ```bash
    python transcribe.py --realtime
    ```
2.  **Transcribe File**:
    ```bash
    python transcribe.py path/to/audio.mp3
    ```
3.  **Background Transcriber**:
    ```bash
    python background_transcriber.py --key f8
    ```

### Linting & Formatting
- **Linter**: `ruff` (implied by `.ruff_cache`).
- **Command**:
  ```bash
  ruff check .
  ```
- **Formatter**: `ruff format .` (or `black` compatible).

## 2. Code Style & Conventions

### General
- **Language**: Python 3.
- **Indentation**: 4 spaces.
- **Line Length**: Follow standard PEP 8 (approx 88-100 chars).
- **Shebang**: Include `#!/usr/bin/env python3` at the top of executable scripts.

### Imports
- Sort imports alphabetically.
- Group imports:
  1.  Standard library (`os`, `sys`, `json`, `asyncio`, etc.)
  2.  Third-party libraries (`websockets`, `pyaudio`, `pynput`, `elevenlabs`)
  3.  Local application imports.
- Use `try...except ImportError` blocks for optional dependencies or to provide helpful installation instructions.

### Naming Conventions
- **Classes**: `PascalCase` (e.g., `RealtimeTranscriber`, `AudioStreamer`).
- **Functions/Methods**: `snake_case` (e.g., `get_api_key`, `start_recording`).
- **Variables**: `snake_case`.
- **Constants**: `UPPER_CASE` (e.g., `SAMPLE_RATE`, `WEBSOCKET_URL`).
- **Private Members**: Prefix with underscore `_` (e.g., `_stream_audio`, `_capture_audio`).

### Type Hinting
- Use type hints for function arguments and return values.
- Use `typing` module (`Optional`, `List` (or `list` in newer Python), etc.).
- Example:
  ```python
  def format_timestamp(seconds: float) -> str:
      ...
  ```

### Documentation
- **Module Docstrings**: Include a high-level description, features, and usage examples at the top of the file.
- **Function/Method Docstrings**: Concise summary of what the function does.
  ```python
  def start_recording(self):
      """Start capturing audio."""
  ```

### Error Handling
- Use specific exception handling where possible (e.g., `websockets.exceptions.ConnectionClosed`).
- Catch `KeyboardInterrupt` in `main()` loops for graceful exits.
- Validate environment variables (API keys) early and exit with a helpful message if missing.
- Use `try...finally` to ensure resources (streams, connections) are closed.

### Async/Threading
- The project uses a mix of `threading` (for blocking audio I/O via `pyaudio`) and `asyncio` (for WebSocket communication via `elevenlabs` SDK).
- `AudioStreamer` / `RealtimeTranscriber` uses a separate thread for microphone input.
- Audio data is passed to the asyncio loop via a thread-safe `queue.Queue`.
- The `elevenlabs.realtime.scribe` SDK is used for the WebSocket connection management.

## 3. Architecture Overview

### `transcribe.py`
- **Purpose**: General-purpose CLI for transcription.
- **Modes**:
  - `RealtimeTranscriber`: Captures audio -> Queue -> SDK Send -> Print/Save.
  - `transcribe_file`: Uploads file -> ElevenLabs API -> Poll/Wait -> Format Output.
- **Key Classes**: `RealtimeTranscriber`.
- **Features**: VAD (Voice Activity Detection), Speaker Diarization, timestamps.

### `background_transcriber.py`
- **Purpose**: "Push-to-Talk" background tool.
- **Mechanism**: Listens for global keypress (via `pynput`).
- **Flow**:
  1.  Key Press -> Start Recording (`pyaudio` stream in thread).
  2.  Launch Async Session -> Connect to Scribe v2 Realtime.
  3.  Audio -> Queue -> SDK Send.
  4.  Key Release -> Stop Recording -> `connection.commit()` -> Wait for final transcript.
  5.  Transcript -> Type into active window (`pynput.keyboard`).

## 4. Agent Operational Rules
- **Safety**: Do not commit API keys.
- **Dependencies**: Check `requirements.txt` before adding new imports.
- **Verification**: Since there are no unit tests, ensure code changes are verified by running the script locally if possible, or by careful static analysis.
- **Refactoring**: Maintain the existing pattern of separating audio capture (threading) from network streaming (asyncio). Use the `elevenlabs` SDK for API interactions.
