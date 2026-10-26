# Build Log: engineering a local-first AI assistant on Linux

A beginner-friendly retrospective on the Linux engineering behind CHANCO's
personal-productivity features — a private retrieval memory, a voice loop, an
email drafter, and a system dashboard. Each stage covers **what** was built,
**how**, the **bug** that got in the way, and the **lesson** that fixed it.

[← Back to the CHANCO overview](CHANCO.md)

---

## 1. A desktop HUD for your own reference

- **What:** A small always-on-top window to show your own notes and the
  assistant's answers at a glance while you work, without stealing focus from
  the app you're in.
- **How:** Python and Tkinter — remove the title bar, keep it on top, make it
  draggable and resizable.
- **Bug:** A third-party overlay tool (`wanderlay`) flickered badly on GNOME
  and ignored screen coordinates, because GNOME runs on the strict **Wayland**
  display server.
- **Lesson:** Third-party overlay tools often break on Wayland. Native Tkinter
  gave full control to build a simple, well-behaved window from scratch rather
  than fight a tool that assumed X11.

## 2. Reading the screen: screenshot & OCR

- **What:** Press a hotkey, drag a box around something on your *own* screen
  (a PDF, a terminal, a diagram), and turn that region into text you can feed
  to your notes.
- **How:** Flameshot takes the screenshot, Tesseract does the OCR, a small
  Python script ties them together.
- **Bug:** Flameshot kept aborting with `Screenshot aborted`, because Wayland
  strictly limits which programs may capture the screen.
- **Lesson:** Running Flameshot in X11 mode with `QT_QPA_PLATFORM=xcb` makes it
  behave as on an older display system and sidesteps the restriction. (The same
  Wayland-vs-X11 split shows up again and again — it's the single biggest
  source of surprises on a modern Linux desktop.)

## 3. The ears: voice & Whisper

- **What:** Speak into the microphone, transcribe to text, and query your notes
  hands-free.
- **How:** `sox` records the mic; `faster-whisper`, a local model, transcribes
  it.
- **Bugs:**
  - Whisper misheard technical terms — "firewall" came out as "burns."
  - In noisy rooms the mic got stuck recording forever.
- **Lessons:**
  - A short initial prompt ("Technical jargon, IT, programming, Python…")
    biases Whisper toward the right vocabulary and noticeably improves accuracy.
  - A **walkie-talkie toggle** (press to start, press again to stop) beats
    automatic silence-detection in a noisy room.

## 4. The voice back: Edge-TTS

- **What:** Have answers read aloud so the loop is truly hands-free.
- **How:** Edge-TTS with the Christopher Neural voice, triggered by the backend
  once an answer is ready.
- **Lesson:** Keeping speech-in (Whisper) and speech-out (Edge-TTS) as separate,
  swappable stages made the voice loop easy to debug one half at a time.

## 5. The brain & routing (the AI layer)

- **What:** An assistant that reads your query and the retrieved notes and
  answers quickly.
- **How:** Started with a local model (Qwen 7B in Docker), but it was too slow
  on a laptop CPU. Moved to cloud APIs — **Groq** for speed, **OpenAI** as an
  alternative — both behind a **LiteLLM** gateway in the Docker stack.
- **Bugs:**
  - The local Docker model fought with Ollama over port `11434`.
  - API calls failed when the endpoint URL didn't exactly match the provider.
- **Lessons:**
  - CPU-only local models are slow; a gateway lets you reach for a fast cloud
    model when you need one.
  - **Put the model provider behind a gateway.** LiteLLM sends a request to Groq
    in about half a second and keeps the provider a *config* detail — you can
    swap back to a local model without touching the Python.

## 6. The second brain: RAG with Qdrant

- **What:** Let the assistant answer from your own notes and rules, not generic
  internet knowledge.
- **How:** Retrieval-Augmented Generation. A Docker **Qdrant** database stores
  the text; a Docker **embeddings** container turns text into vectors.
- **Bugs:**
  1. `SSL: RECORD_LAYER_FAILURE` — the client tried HTTPS against a local HTTP
     database.
  2. `404 Not Found` — the collection didn't exist yet.
  3. `AttributeError` — Qdrant had renamed `.search()` to `.query_points()`.
- **Lessons:**
  1. Force a plain-HTTP connection for a local database.
  2. Run the ingestion step first, so the collection exists before you query it.
  3. **Client libraries change** — pin versions and read changelogs. A minor
     version bump renamed the core method out from under the code.

## 7. From script to small system

The components moved from loose terminal scripts to a single local-first
service: a web dashboard on top of a Python FastAPI backend, the voice loop
(Whisper in, Edge-TTS out), the Gmail drafting helper, and two background
workers — the research/translation helper and the hallucination-and-loop guard.

---

## Key takeaways

1. **Wayland changes the rules.** Screen capture and overlays need deliberate
   handling (X11 compatibility mode, native toolkits) rather than assuming the
   X11-era approach still works.
2. **Put the AI behind a gateway.** LiteLLM makes the model provider a config
   change instead of a code change.
3. **Prompt the speech model.** A short domain prompt markedly improves
   transcription of technical terms.
4. **Client libraries change.** Pin versions and read changelogs — the Qdrant
   method rename is the cautionary tale.
5. **Local-first, cloud when needed.** Keep your data and memory on your own
   machine; reach for a cloud model only for speed.
