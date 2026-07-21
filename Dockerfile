FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY . .

# Build the synthetic DB at image build time so the pilot boots instantly.
RUN python scripts/generate_data.py --db data/ksp.db --firs 20000

EXPOSE 8000
ENV KSP_DB=/app/data/ksp.db

# Optional LLM env — override with -e when running.
# ENV KSP_LLM_BACKEND=ollama
# ENV OLLAMA_HOST=http://host.docker.internal:11434
# ENV KSP_LLM_MODEL=llama3.2:1b

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
