# youtube-distill

Turn an educational YouTube video into a standalone article, with screenshots pulled from the video.

## Run

Requires [uv](https://docs.astral.sh/uv/) and `ffmpeg`.

```sh
echo 'OPENAI_API_KEY=sk-...' > .env
uv run --env-file .env uvicorn app.main:app
```

Open http://localhost:8000.

The model defaults to `gpt-6-luna`; set `OPENAI_MODEL` to override.
