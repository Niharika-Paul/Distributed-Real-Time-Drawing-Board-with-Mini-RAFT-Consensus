FROM python:3.12-slim

WORKDIR /app

RUN pip install aiohttp==3.9.5 watchfiles==0.21.0

COPY . .

# watchfiles hot-reloads on any .py change in the bind-mounted folder
CMD ["python", "-m", "watchfiles", "--filter", "python", "python replica.py", "."]
