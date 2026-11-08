# JuneDesk processing worker (Render)

JuneDesk reads most files by itself. This worker takes the files that are too big or unusual for that:

- PDFs over 100 MB or over 600 pages, with Docling layout and table recognition (up to 400 pages by default; longer PDFs use fast text extraction)
- Old Word, PowerPoint and other office formats (.doc, .rtf, .odt, .ppt, .pptx), converted with LibreOffice
- Very large spreadsheets (the first 150,000 rows can be searched; the whole file is kept)
- Long recordings and videos, cut into 10-minute pieces so JuneDesk can transcribe them
- Large or nested archives (.zip, .rar, .7z, .tar.gz) and Outlook mailboxes (.pst, .ost, .mbox): every file inside becomes its own document
- Multi-page TIFF scans, HEIC photos and other image formats

Scanned pages are not read here. The worker cuts them into small page sets, and JuneDesk reads those with its AI model, which is better at Indian scripts and handwriting.

The worker never holds storage keys. It asks JuneDesk for one job at a time and gets short-lived links to download the file and upload the results.

## Deploy (about 10 minutes)

1. In JuneDesk, save the worker key when asked (the secure form has a **Generate** button). Copy the value.
2. Push this project to GitHub (Lovable → GitHub → Connect), so Render can see the `render-worker` folder.
3. In Render, choose **New → Blueprint**, pick the repository, and confirm the `junedesk-worker` service.
4. When Render asks for `JUNEDESK_WORKER_KEY`, paste the value from step 1.
5. Deploy. The first build takes 10–15 minutes because it installs Docling's models.

In Render's **Logs** you should see `JuneDesk worker started`. Files waiting for the worker show as "Waiting for the processing worker" in JuneDesk and start moving within a minute.

## Sizing

| Plan | RAM | Setting | Good for |
| --- | --- | --- | --- |
| Standard | 2 GB | `WORKER_CONCURRENCY=1` | Occasional large files |
| Pro | 4 GB | `WORKER_CONCURRENCY=2` | Regular bulk uploads |
| Pro Plus | 8 GB | `WORKER_CONCURRENCY=3`, `DOCLING_MAX_PAGES=1000` | Large e-discovery batches |

You can run more than one worker instance; they never pick up the same file twice.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JUNEDESK_URL` | published JuneDesk address | Where to fetch work |
| `JUNEDESK_WORKER_KEY` | — | Must match the key saved in JuneDesk |
| `WORKER_CONCURRENCY` | 1 | Files processed at once |
| `DOCLING_MAX_PAGES` | 400 | Longest PDF read with Docling layout |
| `MAX_SHEET_ROWS` | 150000 | Searchable rows per spreadsheet |
| `AUDIO_CHUNK_SECONDS` | 600 | Length of each transcription piece |
| `USE_DOCLING` | 1 | Set to 0 to use fast text extraction only |

## Run locally (optional)

```bash
docker build -t junedesk-worker render-worker
docker run --rm -e JUNEDESK_URL=https://case-cosmos-research-desk.lovable.app -e JUNEDESK_WORKER_KEY=... junedesk-worker
```
