FROM node:20-slim

RUN npm install -g @anthropic-ai/claude-code

RUN useradd -m -u 1001 claude
USER claude

WORKDIR /workspace
