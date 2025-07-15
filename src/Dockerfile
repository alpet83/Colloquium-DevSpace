FROM rust:1.87-slim

RUN apt update
RUN apt -y install inetutils-traceroute nano net-tools curl
# RUN traceroute vds.alpet.me
# Проверка интернета (curl -v для деталей)
# RUN curl -v https://google.com || exit 1

# Создание venv и pip install (если deb include pip deps; иначе добавьте pip deb)
RUN apt install -y python3 python3-pip python3-venv
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
RUN pip install --no-cache-dir flask requests
RUN apt -y install libmariadb-dev libmariadb-dev-compat \
    libcairo2-dev \
    libpango1.0-dev   

RUN pip3 install toml
# Рабочая директория
WORKDIR /app/projects
VOLUME /app/projects

EXPOSE 8084

CMD ["/bin/sh", "-c", "python3 /app/projects/mcp_server.py 2> /app/logs/mcp_errors.log"]
