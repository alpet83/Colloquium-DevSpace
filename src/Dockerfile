FROM rust:1.87-slim

RUN apt update
RUN apt -y install inetutils-traceroute nano net-tools curl

# Создание venv и pip install (если deb include pip deps; иначе добавьте pip deb)
RUN apt install -y python3 python3-pip python3-venv
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
RUN pip install --no-cache-dir flask requests
RUN apt -y install libmariadb-dev libmariadb-dev-compat \
    libcairo2-dev \
    libpango1.0-dev   

RUN apt install -y sudo git
RUN apt install -y build-essential
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
