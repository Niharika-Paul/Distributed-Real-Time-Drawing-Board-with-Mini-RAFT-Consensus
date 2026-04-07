FROM python:3.12-slim

WORKDIR /app

RUN pip install aiohttp==3.9.5 watchfiles==0.21.0

COPY . .

# watchfiles restarts the process when any .py file changes (hot-reload)
CMD ["python", "-m", "watchfiles", "--filter", "python", "python gateway.py", "."]
