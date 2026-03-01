FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir Pillow

COPY server.py .

RUN mkdir -p /music

ENV MUSIC_DIR=/music
ENV PORT=8765

EXPOSE 8765

CMD ["python", "main.py"]