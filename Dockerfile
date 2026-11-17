FROM python:3.12-slim
# ffmpeg turns Gemini's voice into the .m4a Telegram plays (a Mac uses its built-in afconvert instead)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY fridgebud ./fridgebud
ENV FRIDGEBUD_DB=/data/fridgebud.db PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "fridgebud.telegram"]
