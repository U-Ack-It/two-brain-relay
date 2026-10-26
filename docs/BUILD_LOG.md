# Build Log: from Second Brain to CHANCO

A beginner-friendly retrospective on building a local-first AI assistant and a personal RAG database on Linux. Each stage covers **what** I built, **how** I built it, the **bug** that got in the way, and the **lesson** that fixed it.

[← Back to CHANCO overview](CHANCO.md)

---

## 1. Document capture: screenshot & OCR

- **What:** Press a hotkey, drag a box around a diagram, an error message or a page on my screen, and turn it into text for my notes.
- **How:** Flameshot takes the screenshot, Tesseract does the OCR, and a Python script ties them together.
- **Bug:** Flameshot kept crashing or freezing the terminal with `Screenshot aborted`, because Wayland strictly limits which programs can capture the screen.
- **Lesson:** Running Flameshot in X11 compatibility mode with `QT_QPA_PLATFORM=xcb` fixed it. When a tool fails on Linux, check the display server first.

## 2. Voice: Whisper

- **What:** Speak a voice note into the microphone, transcribe it to text, and send it to the AI.
- **How:** `sox` records the mic and `faster-whisper`, a local model, transcribes it.
- **Bugs:**
  - Whisper misheard technical terms, for example "firewall" came out as "burns."
  - In noisy rooms the mic got stuck recording forever.
- **Lessons:**
  - I gave Whisper an initial prompt ("Technical jargon, IT, programming, Python...") to bias it toward tech vocabulary.
  - I replaced auto-stop recording with a **walkie-talkie toggle**: press once to start, press again to stop.

## 3. The brain & routing (the AI layer)

- **What:** An AI that reads the captured text and answers fast.
- **How:** I started with a local model (Qwen 7B in Docker), but it was too slow on a laptop CPU. I switched to cloud APIs, **Groq** for speed and **OpenAI**, both routed through a **LiteLLM** gateway in my Docker stack.
- **Bugs:**
  - The local Docker model fought with Ollama over port `11434`.
  - API keys failed when the endpoint URL didn't exactly match the provider.
- **Lessons:**
  - Local CPU models are slow.
  - The LiteLLM gateway sends a request to Groq in about 0.5 s, and it keeps the architecture clean: I can switch back to local models without touching the Python scripts.

## 4. The second brain: RAG with Qdrant

- **What:** Let the AI answer from my own notes (such as my Investigative Framework) and my own rules, not generic internet knowledge.
- **How:** Retrieval-Augmented Generation (RAG). A Docker **Qdrant** database stores the text, and a Docker **embeddings** container turns that text into vectors.
- **Bugs:**
  1. `SSL: RECORD_LAYER_FAILURE`: the Python client tried HTTPS against a local HTTP database.
  2. `404 Not Found`: the collection didn't exist yet.
  3. `AttributeError`: Qdrant renamed `.search()` to `.query_points()`.
- **Lessons:**
  1. Force a plain-HTTP connection to the local database.
  2. Run the ingestion step first, so the collection exists.
  3. Update the code to match the current Qdrant client API.

## 5. CHANCO: from script to AI operating system

The last stage moved everything from a terminal script to a full local-first system: a glassmorphic web dashboard on top of a Python FastAPI backend. It added the voice loop (Whisper in, Edge-TTS out), the Gmail Secretary that drafts replies for me to review, and two background workers: the Shadow Agent for web research and translation, and the Governor to block hallucinations and data loops.

---

## Key takeaways

1. **Wayland changes the rules.** Screen capture needs X11 compatibility mode on many tools; check the display server first.
2. **Put the AI behind a gateway.** LiteLLM makes the model provider a config change instead of a code change.
3. **Prompt the speech model.** A short initial prompt noticeably improves transcription of technical terms.
4. **Client libraries change.** Pin versions and read changelogs, as the Qdrant API rename showed.
5. **Local-first, cloud when needed.** Keep data and memory local, and use cloud models for speed.
