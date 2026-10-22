# ZapStream

Paste a video link, download the full video or only one part, and turn a tutorial into a study PDF with a local AI (Ollama).

## Features
- Video details: title, channel, duration, views, likes
- Download full video, a time range, or MP3
- AI study PDF with concepts, glossary and quiz

## Requirements
- Python 3.10+
- ffmpeg (`winget install ffmpeg`)
- Ollama with a model (`ollama pull llama3.2`)

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`
3. Open the folder in VS Code and click **Allow** on the automatic task prompt
4. Click **Go Live** (Live Server extension)

Manual start: `python start.py`

## Note
Use ZapStream only for videos you own or have permission to save.