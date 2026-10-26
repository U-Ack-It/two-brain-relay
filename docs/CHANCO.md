# CHANCO — a local-first AI "second brain" for Linux

**A personal, local-first assistant for Linux: a private retrieval memory over
your own notes, a voice loop, an email drafting helper, and a live
system-health dashboard — stitched together over a small FastAPI backend.**

> This page documents the legitimate, personal-productivity components of
> CHANCO and the Linux engineering behind them. CHANCO's own source code is not
> published here; the code in this repository is the separate **Two-Brain
> Relay** project ([main README](../README.md)). This is a companion write-up,
> kept alongside the relay because the two share the same design instincts —
> local-first, model-agnostic, run-it-yourself.

---

## What it does

- **Personal retrieval memory ("second brain").** Retrieval-Augmented
  Generation over *your own* notes and frameworks, stored in a local Qdrant
  vector database, so answers are grounded in your material instead of generic
  web knowledge.
- **Voice loop.** Local Whisper transcribes what you say; Edge-TTS (Christopher
  Neural voice) reads the answer back — a hands-free way to query your own
  notes at your desk.
- **Email drafting assistant.** Reads your Gmail over IMAP and drafts
  professional replies on instruction, for you to review and send.
- **Live system dashboard.** A glassmorphic web UI showing real-time CPU, RAM,
  disk, and Docker-container health for the stack itself.
- **Background helpers.**
  - A **research helper** that searches and translates web sources, including
    foreign-language material, into your notes.
  - A **guard** that watches for model hallucinations and runaway data loops
    and stops them.

## Architecture

```
 Your voice / your notes / your inbox
            │
            ▼
   Whisper · your documents · IMAP
            │
            ▼
   FastAPI backend ──► LiteLLM gateway ──► Groq / OpenAI / local models
            │                  ▲
            ▼                  │
   Qdrant (RAG memory) ────────┘
            │
            ▼
   Dashboard · Edge-TTS voice · research helper · hallucination guard
```

Everything runs on your own machine. The only outbound calls are to whichever
LLM provider you point the gateway at — and that is a one-line config change,
or nothing at all if you run a local model.

## Tech stack

| Layer | Tools |
|---|---|
| Backend | Python, FastAPI, Docker |
| Retrieval memory | Qdrant, a local embeddings service |
| Voice | sox, faster-whisper (in), Edge-TTS (out) |
| LLM routing | LiteLLM → Groq, OpenAI (local Qwen / Ollama optional) |
| Dashboard | glassmorphic web UI over the FastAPI backend |
| Desktop | Ubuntu, KDE Plasma (Wayland) |

## Hardest problems solved (the short version)

- **Local models were too slow on a laptop CPU**, so a LiteLLM gateway sits in
  front of the models: Groq answers in about half a second, and switching back
  to a local model is a config change rather than a code change.
- **Speech recognition mangled technical words**, fixed by priming Whisper with
  a short domain prompt.
- **A client-library API rename broke ingestion**, a reminder to pin versions
  and read changelogs (see the build log).

The full stage-by-stage story — what was built, how, the bug, and the lesson —
is in the **[Build Log](BUILD_LOG.md)**.
