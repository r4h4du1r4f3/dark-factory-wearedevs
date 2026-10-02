FROM python:3.12-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY app.py passwords.py ./
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "app.py"]
