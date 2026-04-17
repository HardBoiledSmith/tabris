FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg unzip \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN case "$(uname -m)" in \
        aarch64) AWS_ARCH=aarch64 ;; \
        x86_64) AWS_ARCH=x86_64 ;; \
        *) echo "unsupported arch: $(uname -m)" && exit 1 ;; \
    esac \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

RUN npm install -g @anthropic-ai/claude-code @sentry/cli

RUN useradd -m -u 1001 claude
USER claude

WORKDIR /workspace
