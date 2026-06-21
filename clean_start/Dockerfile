FROM golang:1.26-alpine AS build

WORKDIR /src
COPY clean_start/go.mod clean_start/go.sum* ./
RUN go mod download

COPY clean_start/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/clean-start ./cmd/clean-start

FROM alpine:3.22
RUN adduser -D -H clean
USER clean
COPY --from=build /out/clean-start /usr/local/bin/clean-start
EXPOSE 8110
ENTRYPOINT ["/usr/local/bin/clean-start"]
