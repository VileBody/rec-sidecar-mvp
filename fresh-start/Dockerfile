FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY llm_service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY llm_service ./llm_service
COPY fresh-start ./fresh-start
COPY scripts/live_client_voice_agent.py ./scripts/live_client_voice_agent.py
COPY sales_scripts/glubina_kazan_10_call_scripts_v1.md ./sales_scripts/glubina_kazan_10_call_scripts_v1.md

EXPOSE 8101

CMD ["python3", "fresh-start/chat_loop_app.py", "--host", "0.0.0.0", "--port", "8101"]
