# Sample files

Small files for trying the project quickly, about 24 MB in total. They are cut from
the material handed out at the hackathon, and `data/raw/` (where full-size files go)
is not in git.

The commands below write to the normal output folders (`data/meeting_notes/`,
`data/resources/`). Delete those files afterwards if you want a clean state.

## Videos and audio: meeting notes

| File | Length | What it shows |
| --- | --- | --- |
| `video/graalvm_talk_clip.mp4` | 90 s, 1.5 MB | A recorded slide talk in English: key frames on slide changes |
| `video/tech_concept_meeting_clip.mp4` | 90 s, 2.6 MB | A Teams screen share in Swedish: the presenter moves windows around, and the silent-frame filter removes most of those frames |
| `video/jury_camera_clip.mp4` | 90 s, 3.3 MB | Camera footage in English: camera mode keeps only real scene changes |
| `audio/jury_audio_clip.mp3` | 60 s, 0.4 MB | Audio only: the notes get a part every 5 minutes instead of frames |

```bash
python run.py notes samples/video/graalvm_talk_clip.mp4
python run.py notes samples/video --keywords --speakers     # every clip in the folder
python run.py notes samples/audio/jury_audio_clip.mp3 --no-summary
```

Or open the website (`python run.py web`) and drop the clips on the upload page.

Approximate results (whisper `base`, GTX 1660 Ti; a CPU is a few times slower):

| Clip | Frames kept | Time |
| --- | --- | --- |
| GraalVM | 3 | ~20 s with summary and keywords |
| Tech Concept | 7 (20 silent frames left out) | ~20 s, ~85 s with speakers |
| Jury | 3 | ~15 s, ~40 s with speakers |
| Audio | none (audio only) | ~5 s without summary |

The summary and keywords need Ollama, and `--speakers` needs pyannote plus a Hugging
Face token. Without them the notes are still made, just without those parts. See
[HOW_TO_RUN.md](../HOW_TO_RUN.md).

Find the video again from its notes, or the notes from the video:

```bash
python run.py lookup data/meeting_notes/graalvm_talk_clip.md --in samples/video
python run.py lookup samples/video/graalvm_talk_clip.mp4
```

## Documents: duplicate cleanup, versions and the store

| File | What it tests |
| --- | --- |
| `docs/C2L_Presentation_Hackathon_RevB.pdf` | The original, 26 pages |
| `docs/C2L_Presentation_Hackathon_RevB - Copy.pdf` | An exact copy (same bytes, different name) |
| `docs/C2L_Presentation_short.pdf` | A shortened version: its first 15 pages |
| `docs/SD-WAN Orchestration_commercial presentation_final.pptx` | A 28-slide deck |
| `docs/SD-WAN Orchestration_commercial presentation_final2.pptx` | A later version of it: 25 slides, a few reworded |
| `docs/Brief.png` | A single image, counted as a one-slide document |

```bash
python run.py dedupe samples/docs --mode batch --index samples_index.json
```

Expected: 62 unique slides, and 2 of the 6 files are not needed (the exact copy and the
shortened PDF, whose pages are all in the original). Removing them would save 5.5 MB
(35 % of 15.9 MB). The `--index` option keeps this test out of the default
`data/slide_index.json`; delete `samples_index.json` afterwards.

On the website, open **Remove duplicates**, pick the `samples/docs` folder and press
run. Besides the report, the PDFs and decks go into the document store, and the
**Versions** page then shows the version trees: the original PDF with the shortened
version, and the two SD-WAN decks. Any version can be downloaded again from the store.
A PDF comes back pixel-identical. A deck comes back with every slide, picture and note
byte-identical; only the ZIP packaging can differ.

PDF to Markdown, with every image saved once:

```bash
python run.py pdf samples/docs/C2L_Presentation_short.pdf --out data/docs
```

## Source

- The GraalVM talk, the Tech Concept meeting and the Jury and prizes recording are
  each a 90-second cut from the full recording, made smaller (720p/540p, H.264, mono
  AAC).
- The PDFs, the decks and `Brief.png` are hackathon material. The shortened PDF is the
  first 15 pages of the original, saved with pypdf.
