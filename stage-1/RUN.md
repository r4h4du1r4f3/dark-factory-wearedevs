# Tablekeeper Stage 1 — Run

Single command that builds and starts the service:

```sh
docker build -t tablekeeper-stage-1 ./stage-1 && docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-1
```

The service listens on `0.0.0.0` using `$PORT` (default `8080`) and answers `GET /health` with `200 {"status": "ok"}`.
