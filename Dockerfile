FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# IMPORTANT: exactly one gunicorn worker.
# web_ui.py starts the alert worker, the EOD fetcher, the market loop and the
# gap scanner as background threads the moment it is imported. Running more
# than one worker would start those threads in every worker, so every price
# poll and every Telegram alert would fire two, three, four times over.
CMD ["sh", "-c", "gunicorn web_ui:app --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 4 --timeout 120"]
