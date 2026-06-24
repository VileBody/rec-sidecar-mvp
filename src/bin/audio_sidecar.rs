use std::{env, error::Error, net::SocketAddr};

use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::{SinkExt, StreamExt};
use rec_sidecar_mvp::audio_aec::{Aec3Processor, Aec3Stats};
use serde::{Deserialize, Serialize};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::{accept_async, tungstenite::Message};

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ClientMessage {
    Hello {
        #[serde(default, alias = "sample_rate")]
        sample_rate_hz: Option<u32>,
        #[serde(default)]
        channels: Option<u16>,
    },
    Far {
        pcm16: String,
    },
    Near {
        pcm16: String,
        #[serde(default)]
        flush: bool,
    },
    Flush,
    Reset,
    Stats,
    Close,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ServerMessage<'a> {
    Ready {
        sample_rate_hz: u32,
        frame_samples: usize,
        version: &'a str,
    },
    Ack {
        what: &'a str,
        frames: usize,
        stats: Aec3Stats,
    },
    Clean {
        pcm16: String,
        samples: usize,
        stats: Aec3Stats,
    },
    Stats {
        stats: Aec3Stats,
    },
    Error {
        error: String,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let addr = parse_addr()?;
    let listener = TcpListener::bind(addr).await?;
    eprintln!("audio_sidecar: listening on ws://{addr}");

    loop {
        let (stream, peer) = listener.accept().await?;
        tokio::spawn(async move {
            if let Err(error) = handle_connection(stream, peer).await {
                eprintln!("audio_sidecar: {peer} failed: {error}");
            }
        });
    }
}

fn parse_addr() -> Result<SocketAddr, Box<dyn Error>> {
    let mut args = env::args().skip(1);
    let mut addr = env::var("REC_AEC3_ADDR").unwrap_or_else(|_| "127.0.0.1:8122".to_string());

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--addr" => {
                addr = args.next().ok_or("--addr requires a value")?;
            }
            "--help" | "-h" => {
                println!(
                    "Usage: cargo run --bin audio_sidecar -- [--addr 127.0.0.1:8122]\n\n\
                     WebSocket JSON protocol:\n\
                     {{\"type\":\"hello\",\"sample_rate_hz\":16000,\"channels\":1}}\n\
                     {{\"type\":\"far\",\"pcm16\":\"<base64 little-endian i16>\"}}\n\
                     {{\"type\":\"near\",\"pcm16\":\"<base64 little-endian i16>\"}}\n\
                     {{\"type\":\"flush\"}}\n"
                );
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument: {other}").into()),
        }
    }

    Ok(addr.parse()?)
}

async fn handle_connection(stream: TcpStream, peer: SocketAddr) -> Result<(), Box<dyn Error>> {
    let mut ws = accept_async(stream).await?;
    let mut aec = Aec3Processor::new(16_000)?;
    eprintln!("audio_sidecar: {peer} connected");
    send_json(
        &mut ws,
        &ServerMessage::Ready {
            sample_rate_hz: aec.sample_rate_hz(),
            frame_samples: aec.frame_samples(),
            version: env!("CARGO_PKG_VERSION"),
        },
    )
    .await?;

    while let Some(message) = ws.next().await {
        let message = message?;
        let text = match message {
            Message::Text(text) => text,
            Message::Binary(_) => {
                send_error(&mut ws, "binary websocket frames are not supported").await?;
                continue;
            }
            Message::Close(_) => break,
            Message::Ping(payload) => {
                ws.send(Message::Pong(payload)).await?;
                continue;
            }
            Message::Pong(_) => continue,
            Message::Frame(_) => continue,
        };

        let command = match serde_json::from_str::<ClientMessage>(&text) {
            Ok(command) => command,
            Err(error) => {
                send_error(&mut ws, &format!("bad json: {error}")).await?;
                continue;
            }
        };

        match command {
            ClientMessage::Hello {
                sample_rate_hz,
                channels,
            } => {
                if channels.unwrap_or(1) != 1 {
                    send_error(&mut ws, "AEC3 sidecar currently expects mono PCM16").await?;
                    continue;
                }
                if let Some(sample_rate_hz) = sample_rate_hz {
                    aec = Aec3Processor::new(sample_rate_hz)?;
                }
                send_json(
                    &mut ws,
                    &ServerMessage::Ready {
                        sample_rate_hz: aec.sample_rate_hz(),
                        frame_samples: aec.frame_samples(),
                        version: env!("CARGO_PKG_VERSION"),
                    },
                )
                .await?;
            }
            ClientMessage::Far { pcm16 } => {
                let pcm = decode_pcm16(&pcm16)?;
                let frames = aec.process_render_pcm16(&pcm)?;
                send_json(
                    &mut ws,
                    &ServerMessage::Ack {
                        what: "far",
                        frames,
                        stats: aec.stats(),
                    },
                )
                .await?;
            }
            ClientMessage::Near { pcm16, flush } => {
                let pcm = decode_pcm16(&pcm16)?;
                let mut clean = aec.process_capture_pcm16(&pcm)?;
                if flush {
                    clean.extend(aec.flush_capture_pcm16()?);
                }
                send_json(
                    &mut ws,
                    &ServerMessage::Clean {
                        samples: clean.len(),
                        pcm16: encode_pcm16(&clean),
                        stats: aec.stats(),
                    },
                )
                .await?;
            }
            ClientMessage::Flush => {
                let clean = aec.flush_capture_pcm16()?;
                send_json(
                    &mut ws,
                    &ServerMessage::Clean {
                        samples: clean.len(),
                        pcm16: encode_pcm16(&clean),
                        stats: aec.stats(),
                    },
                )
                .await?;
            }
            ClientMessage::Reset => {
                aec.reset();
                send_json(
                    &mut ws,
                    &ServerMessage::Ack {
                        what: "reset",
                        frames: 0,
                        stats: aec.stats(),
                    },
                )
                .await?;
            }
            ClientMessage::Stats => {
                send_json(&mut ws, &ServerMessage::Stats { stats: aec.stats() }).await?;
            }
            ClientMessage::Close => break,
        }
    }

    eprintln!("audio_sidecar: {peer} disconnected");
    Ok(())
}

async fn send_json<S>(
    ws: &mut tokio_tungstenite::WebSocketStream<S>,
    message: &ServerMessage<'_>,
) -> Result<(), Box<dyn Error>>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    ws.send(Message::Text(serde_json::to_string(message)?.into()))
        .await?;
    Ok(())
}

async fn send_error<S>(
    ws: &mut tokio_tungstenite::WebSocketStream<S>,
    error: &str,
) -> Result<(), Box<dyn Error>>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    send_json(
        ws,
        &ServerMessage::Error {
            error: error.to_string(),
        },
    )
    .await
}

fn decode_pcm16(encoded: &str) -> Result<Vec<i16>, Box<dyn Error>> {
    let bytes = STANDARD.decode(encoded)?;
    if bytes.len() % 2 != 0 {
        return Err("PCM16 payload has an odd byte length".into());
    }
    Ok(bytes
        .chunks_exact(2)
        .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
        .collect())
}

fn encode_pcm16(samples: &[i16]) -> String {
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    STANDARD.encode(bytes)
}
