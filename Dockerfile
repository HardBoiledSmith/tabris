FROM node:20-slim

RUN npm install -g @anthropic-ai/claude-code

RUN useradd -m -u 1000 claude
USER claude

WORKDIR /workspace
