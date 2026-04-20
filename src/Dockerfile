FROM rust:1.87-slim

RUN apt update && apt -y install --no-install-recommends \
    ca-certificates \
    curl \
    git \
    inetutils-traceroute \
    nano \
    net-tools \
    sudo \
    build-essential \
    pkg-config \
    libssl-dev \
    libmariadb-dev \
    libmariadb-dev-compat \
    libcairo2-dev \
    libpango1.0-dev \
    cmake \
    clang \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Rust toolchain extras useful for local development checks.
RUN rustup component add rustfmt clippy
RUN ln -sf /usr/local/cargo/bin/cargo /usr/local/bin/cargo \
    && ln -sf /usr/local/cargo/bin/rustc /usr/local/bin/rustc \
    && ln -sf /usr/local/cargo/bin/rustfmt /usr/local/bin/rustfmt \
    && ln -sf /usr/local/cargo/bin/cargo-clippy /usr/local/bin/cargo-clippy

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:/usr/local/cargo/bin:${PATH}"
RUN pip install --no-cache-dir flask requests
RUN pip install --no-cache-dir toml gitpython quart fastapi asyncio
# Создание непривилегированного пользователя agent
RUN useradd -m -s /bin/bash -d /app/projects agent
# Настройка sudoers для разрешения apt без пароля
RUN echo "agent ALL=(ALL) NOPASSWD: /usr/bin/apt, /usr/bin/apt-get" > /etc/sudoers.d/agent

# Рабочая директория
WORKDIR /app/projects
VOLUME /app/projects
ENV PYTHONPATH=/app/agent

EXPOSE 8084

CMD ["/bin/sh", "-c", "python3 /app/projects/mcp_server.py 2> /app/logs/mcp_errors.log"]
