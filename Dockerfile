FROM python:3.12-alpine
RUN apk add --no-cache git tzdata && pip install --no-cache-dir fonttools brotli fastapi "uvicorn[standard]"
ENV TZ=America/Ciudad_Juarez FONTS_DIR=/fonts STORE_DIR=/store
WORKDIR /app
COPY app /app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
