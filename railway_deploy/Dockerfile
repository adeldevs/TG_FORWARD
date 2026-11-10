FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY forwarder.py .
COPY forwarder_session.session .
COPY state.json .

CMD ["python", "app.py"]
