use crate::models::SessionState;

#[derive(Default)]
pub struct SseDecoder {
    buffer: String,
}

impl SseDecoder {
    pub fn push(&mut self, bytes: &[u8]) -> Vec<SessionState> {
        self.buffer.push_str(&String::from_utf8_lossy(bytes));
        let mut snapshots = Vec::new();
        while let Some(index) = self.buffer.find("\n\n") {
            let frame = self.buffer[..index].replace("\r", "");
            self.buffer.drain(..index + 2);
            if let Some(snapshot) = decode_snapshot_frame(&frame) {
                snapshots.push(snapshot);
            }
        }
        snapshots
    }
}

fn decode_snapshot_frame(frame: &str) -> Option<SessionState> {
    let mut event = "message";
    let mut data = String::new();
    for line in frame.lines() {
        if let Some(value) = line.strip_prefix("event:") {
            event = value.trim();
        } else if let Some(value) = line.strip_prefix("data:") {
            if !data.is_empty() {
                data.push('\n');
            }
            data.push_str(value.trim_start());
        }
    }
    if event != "snapshot" || data.is_empty() {
        return None;
    }
    serde_json::from_str(&data).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_fragmented_snapshot_and_ignores_event_frames() {
        let mut decoder = SseDecoder::default();
        assert!(decoder
            .push(b"event: snapshot\ndata: {\"session_id\":\"sess")
            .is_empty());
        let snapshots = decoder
            .push(b"-1\",\"seller_draft\":\"hello\"}\n\nevent: event\ndata: {\"type\":\"x\"}\n\n");
        assert_eq!(snapshots.len(), 1);
        assert_eq!(snapshots[0].session_id, "sess-1");
        assert_eq!(snapshots[0].seller_draft, "hello");
    }
}
