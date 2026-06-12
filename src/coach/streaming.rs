pub(super) fn take_sse_event(buffer: &mut String) -> Option<String> {
    let lf = buffer.find("\n\n");
    let crlf = buffer.find("\r\n\r\n");

    let (index, len) = match (lf, crlf) {
        (Some(lf), Some(crlf)) if crlf < lf => (crlf, 4),
        (Some(lf), _) => (lf, 2),
        (_, Some(crlf)) => (crlf, 4),
        _ => return None,
    };

    let event = buffer[..index].to_string();
    buffer.drain(..index + len);
    Some(event)
}

pub(super) fn event_data_lines(event: &str) -> Vec<String> {
    let mut data = Vec::new();

    for line in event.lines() {
        let line = line.trim_start();
        if let Some(value) = line.strip_prefix("data:") {
            data.push(value.trim_start().to_string());
        }
    }

    data
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sse_event_parser_extracts_multiline_data() {
        let mut buffer = "event: message\ndata: {\"a\":1}\ndata: [DONE]\n\nrest".to_string();

        let event = take_sse_event(&mut buffer).unwrap();

        assert_eq!(event_data_lines(&event), vec!["{\"a\":1}", "[DONE]"]);
        assert_eq!(buffer, "rest");
    }

    #[test]
    fn sse_event_parser_handles_crlf_delimiter() {
        let mut buffer = "data: one\r\n\r\ndata: two\n\n".to_string();

        let first = take_sse_event(&mut buffer).unwrap();
        let second = take_sse_event(&mut buffer).unwrap();

        assert_eq!(event_data_lines(&first), vec!["one"]);
        assert_eq!(event_data_lines(&second), vec!["two"]);
        assert!(take_sse_event(&mut buffer).is_none());
    }
}
