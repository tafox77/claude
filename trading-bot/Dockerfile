FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY cryptobot ./cryptobot
RUN pip install --no-cache-dir .
# Mount config.toml and a data directory (set db_path = "/data/cryptobot.db" in the config).
VOLUME ["/data"]
ENTRYPOINT ["cryptobot", "-c", "/app/config.toml"]
CMD ["run"]
