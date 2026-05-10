# OpenWebUI Editor

A lightweight Flask web app for editing Open WebUI messages with a simple chat-style UI.

### Reasons

This app exists purely because Open WebUI chat editing has been broken for a while, and despite claiming it was fixed in a recent version, it still didn't seem to be fully fixed.

## Features

- Flask backend
- HTML templates for chat, editing, and index pages
- Docker Compose support

## Quick start

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the app:
   ```bash
   python app.py
   ```
3. Open `http://localhost:5000` in your browser.

## Docker

Use Docker Compose to run the app with:
```bash
docker compose up --build
```
