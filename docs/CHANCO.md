# CHANCO — Second Brain

**A local-first AI assistant for Linux: a voice loop, document capture, an email drafting assistant, and a personal RAG memory.**

> This page documents CHANCO's architecture and build process. CHANCO's source code is not published; the code in this repository is the separate Two-Brain Relay project ([main README](../README.md)).

---

## What it does

- **Captures documents:** a hotkey to select a diagram, an error message or a page on screen and turn it into text with OCR, ready for my notes.
- **Hears and speaks:** local Whisper transcribes voice notes, and Edge-TTS (Christopher Neural voice) speaks the answer back.
- **Remembers your knowledge:** RAG over your own notes and frameworks in a Qdrant vector database, so answers follow your rules, not generic internet knowledge.
- **Secretary:** reads Gmail over IMAP and drafts professional replies on instruction, for me to review and send.
- **Live dashboard:** the "Quantum Architect" glassmorphic UI shows live CPU, RAM, disk and Docker container health.
- **Background agents:**
  - **Shadow Agent:** searches and translates the web, including foreign-language sources.
  - **Governor:** guards against hallucinations and runaway data loops.

## Architecture

## Tech stack

| Layer | Tools |
|---|---|
| Interface | Glassmorphic web dashboard |
| Backend | Python, FastAPI, Docker |
| Document capture | Flameshot, Tesseract OCR |
| Voice | sox, faster-whisper, Edge-TTS |
| LLM routing | LiteLLM → Groq, OpenAI (local Qwen / Ollama optional) |
| Memory (RAG) | Qdrant, embeddings service |
| Platform | Ubuntu Linux (Wayland) |

## Hardest problems solved

- **Screen capture on Wayland:** the capture tool crashed under Wayland; running it in X11 compatibility mode fixed it.
- **Speed vs. local models:** CPU-only local models were too slow, so I put a LiteLLM gateway in front of the models. Groq answers in about 0.5 s, and switching back to local models is a config change.
- **Speech recognition of technical words:** fixed by priming Whisper with a domain-specific prompt.
- **RAG setup:** three Qdrant errors in a row, each fixed by matching the client to the server and the current API.

The full stage-by-stage story, with every bug and fix, is in the **[Build Log](BUILD_LOG.md)**.
