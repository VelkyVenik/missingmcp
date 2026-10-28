# Garmin landing: demo video of a real claude.ai session

Status: ready-for-human

## Goal

Give `/garmin` what `/car` got in PR #67: a short looping video of a **real** claude.ai
conversation with the Garmin connector, in its own "See it in Claude" section right under the
hero. It should show what the connector knows, not describe it.

## What a human does (the operator)

1. claude.ai on the web in **dark mode**, English, a new chat with the Garmin connector on.
   Record only the chat area: `Cmd+Shift+5` → Record Selected Portion.
2. Script idea (pick one, ~30 s raw): *"How did I sleep last night and am I ready to train
   today?"* → sleep + training readiness + HRV answer; then a follow-up that shows depth, e.g.
   *"Compare my last three runs"*. Read-only tools only. Don't create workouts on camera.
3. Save the `.mov` and hand it over.

## What the agent does (reuse the car pipeline)

- **Review frames first** (`ffmpeg -vf fps=1,scale=…` + `tile=6x6` contact sheet). Pixelate
  anything private: addresses or locations, other people's names, GPS/maps, the chat history
  sidebar. The operator keeps the "Afternoon, Václav" greeting on purpose (car decision).
- **Encode** like the car demo: trim, speed waits up ×1.6, **hold** ~2.5 s on the typed prompt
  and ~4 s on each answer (`tpad` clones around a `trim` split), H.264 `-crf 24 -tune
  stillimage -movflags +faststart`, full recording width, `yuv420p`, no audio. Car result:
  1048×692, 29 s, 243 kB. Exact filter graph: PR #67 conversation / the car commit message.
- **Poster:** a JPEG of the first full answer.
- **Serve** via `app.py` `_assets` + `static_video(...)` (byte ranges; Safari needs 206) and
  `static_png(..., "image/jpeg")` for the poster.
- **Markup:** copy the car `<figure class="demo">` block. `<video controls muted loop
  playsinline preload="none" data-autoplay poster=…>`, **no `autoplay` attribute**: `site.js`
  starts it only when it's on screen and never under `prefers-reduced-motion` (WCAG 2.2.2,
  Codex review on PR #67). Use its own `<section id="demo">` with `sec-h` "See it in Claude",
  not under `.prose`: a full-width video under 62ch text looks misaligned.
- **Tests** in the car style: the `<video>` has controls, poster and `data-autoplay` and no
  `autoplay` attribute; the MP4 answers `Range` with 206.

## Notes

- Garmin stance (memory, 2026-10-21): **no active promotion**. A demo on its own landing page is
  fine. Don't add it to the homepage, campaigns or directories as part of this.
- Optionally do the ChatGPT guide page (`/garmin/chatgpt`) the same way. See
  `.scratch/garmin-client-guides/`.
