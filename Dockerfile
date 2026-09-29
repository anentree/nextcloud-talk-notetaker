# Claude Code CLI for NOTES_BACKEND=claude. The npm package ships a native
# binary per platform; install it here and copy just that binary, so the
# runtime image needs no Node. Pinned: bump deliberately, not on rebuild.
FROM node:22-bookworm-slim AS claude
RUN npm install -g @anthropic-ai/claude-code@2.1.280 \
    && cp "$(readlink -f "$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe")" /claude \
    && /claude --version

FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=claude /claude /usr/local/bin/claude

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

RUN playwright install --with-deps chromium

ENTRYPOINT ["notetaker"]
