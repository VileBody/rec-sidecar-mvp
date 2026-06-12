#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum CoachChatRole {
    User,
    Assistant,
}

pub(crate) struct CoachChatMessage {
    pub(crate) role: CoachChatRole,
    pub(crate) text: String,
    pub(crate) request_id: Option<u64>,
    pub(crate) streaming: bool,
    pub(crate) help_stage_label: Option<&'static str>,
    pub(crate) model_label: Option<String>,
}

pub(crate) struct ContextInput<'a> {
    pub(crate) transcript: &'a [String],
    pub(crate) live_partial: Option<&'a str>,
    pub(crate) coach_bubbles: &'a [String],
    pub(crate) coach_live: Option<&'a str>,
    pub(crate) coach_chat_messages: &'a [CoachChatMessage],
}

pub(crate) struct HelpContextSettings {
    pub(crate) max_transcript_chars: usize,
    pub(crate) max_chat_chars: usize,
    pub(crate) max_live_partial_chars: usize,
    pub(crate) max_coach_messages: usize,
}

pub(crate) fn render_transcript(title: &str, saved_at: &str, input: &ContextInput<'_>) -> String {
    let mut out = String::new();
    out.push_str(&format!("Title: {}\n", title));
    out.push_str(&format!("Saved: {}\n", saved_at));
    out.push_str("\n--- Transcript ---\n\n");

    if input.transcript.is_empty() && input.live_partial.is_none() {
        out.push_str("(empty transcript)\n");
    } else {
        for chunk in input.transcript {
            out.push_str(chunk.trim());
            out.push_str("\n\n");
        }

        if let Some(partial) = input.live_partial {
            out.push_str(partial.trim());
            out.push_str("\n\n");
        }
    }

    if !input.coach_bubbles.is_empty() || input.coach_live.is_some() {
        out.push_str("\n--- Sales Coach ---\n\n");

        for chunk in input.coach_bubbles {
            out.push_str(chunk.trim());
            out.push_str("\n\n");
        }

        if let Some(partial) = input.coach_live {
            out.push_str(partial.trim());
            out.push_str("\n\n");
        }
    }

    if !input.coach_chat_messages.is_empty() {
        out.push_str("\n--- Coach Chat ---\n\n");

        for message in input.coach_chat_messages {
            let role = match message.role {
                CoachChatRole::User => "You",
                CoachChatRole::Assistant => "Coach",
            };
            out.push_str(role);
            out.push_str(": ");
            out.push_str(message.text.trim());
            out.push_str("\n\n");
        }
    }

    out
}

pub(crate) fn render_coach_context(input: &ContextInput<'_>) -> String {
    let mut out = String::new();
    out.push_str("Живой B2C sales call. Диаризация сохранена в строках как \"Спикер N:\" или \"Канал N:\". Транскрипт может содержать live partial в конце.\n\n");
    out.push_str("--- Диалог ---\n");

    for chunk in input.transcript {
        push_clean_line(&mut out, chunk);
    }

    if let Some(partial) = input.live_partial {
        out.push_str("[LIVE] ");
        push_clean_line(&mut out, partial);
    }

    out.push_str("\n--- Уже показанные подсказки тренера ---\n");

    if input.coach_bubbles.is_empty() && input.coach_live.is_none() {
        out.push_str("(пока нет)\n");
    } else {
        for chunk in input.coach_bubbles {
            push_clean_line(&mut out, chunk);
        }

        if let Some(partial) = input.coach_live {
            out.push_str("[STREAMING] ");
            push_clean_line(&mut out, partial);
        }
    }

    out.push_str("\nВерни строго EOS или BOS + один короткий абзац на 2-3 предложения.\n");
    out
}

pub(crate) fn render_chat_context(input: &ContextInput<'_>) -> String {
    let mut out = String::new();
    out.push_str("Снимок контекста на момент отправки вопроса. Диаризация сохранена как \"Спикер N:\" или \"Канал N:\".\n\n");
    out.push_str("--- Диалог ---\n");

    if input.transcript.is_empty() && input.live_partial.is_none() {
        out.push_str("(диалог пока пуст)\n");
    } else {
        for chunk in input.transcript {
            push_clean_line(&mut out, chunk);
        }

        if let Some(partial) = input.live_partial {
            out.push_str("[LIVE] ");
            push_clean_line(&mut out, partial);
        }
    }

    out.push_str("\n--- Уже показанные live-подсказки ---\n");
    if input.coach_bubbles.is_empty() && input.coach_live.is_none() {
        out.push_str("(пока нет)\n");
    } else {
        for chunk in input.coach_bubbles {
            push_clean_line(&mut out, chunk);
        }

        if let Some(partial) = input.coach_live {
            out.push_str("[STREAMING] ");
            push_clean_line(&mut out, partial);
        }
    }

    out.push_str("\n--- Предыдущий чат с тренером ---\n");
    if input.coach_chat_messages.is_empty() {
        out.push_str("(пока нет)\n");
    } else {
        for message in input.coach_chat_messages {
            if message.text.trim().is_empty() {
                continue;
            }

            let role = match message.role {
                CoachChatRole::User => "Продавец",
                CoachChatRole::Assistant => "Тренер",
            };
            out.push_str(role);
            out.push_str(": ");
            push_clean_line(&mut out, &message.text);
        }
    }

    out
}

pub(crate) fn render_help_context(
    input: &ContextInput<'_>,
    settings: HelpContextSettings,
) -> String {
    let mut transcript = String::new();
    for chunk in input.transcript {
        push_clean_line(&mut transcript, chunk);
    }
    let (transcript, transcript_truncated) =
        truncate_left_with_flag(&transcript, settings.max_transcript_chars);

    let mut out = String::new();
    out.push_str("CONTEXT_VERSION: rec-sidecar-help-v1\n\n");
    out.push_str("--- Диалог, последние фрагменты ---\n");

    if transcript_truncated {
        out.push_str("[TRUNCATED: older transcript omitted]\n");
    }

    if transcript.trim().is_empty() && input.live_partial.is_none() {
        out.push_str("(диалог пока пуст)\n");
    } else {
        out.push_str(&transcript);

        if let Some(partial) = input.live_partial {
            let live_partial =
                truncate_left(&clean_one_line(partial), settings.max_live_partial_chars);
            if !live_partial.is_empty() {
                out.push_str("[LIVE] ");
                out.push_str(&live_partial);
                out.push('\n');
            }
        }
    }

    out.push_str("\n--- Уже показанные подсказки / ответы тренера ---\n");
    let coach_context = render_recent_coach_context(
        input,
        settings.max_coach_messages.max(1),
        settings.max_chat_chars,
    );
    if coach_context.trim().is_empty() {
        out.push_str("(пока нет)\n");
    } else {
        out.push_str(&coach_context);
    }

    out.push_str("\n--- Инструкция для режима Помоги ---\n");
    out.push_str("Пользователь нажал кнопку Помоги прямо сейчас. Дай помощь для текущего момента, не пересказывай весь звонок.\n");
    out
}

fn render_recent_coach_context(
    input: &ContextInput<'_>,
    max_messages: usize,
    max_chars: usize,
) -> String {
    let mut messages = Vec::new();

    for chunk in input.coach_bubbles {
        let text = clean_one_line(chunk);
        if !text.is_empty() {
            messages.push(format!("Live-подсказка: {}", text));
        }
    }

    if let Some(partial) = input.coach_live {
        let text = clean_one_line(partial);
        if !text.is_empty() {
            messages.push(format!("[STREAMING] Live-подсказка: {}", text));
        }
    }

    for message in input.coach_chat_messages {
        if !matches!(message.role, CoachChatRole::Assistant) {
            continue;
        }

        let text = clean_one_line(&message.text);
        if !text.is_empty() {
            messages.push(format!("Тренер: {}", text));
        }
    }

    let start = messages.len().saturating_sub(max_messages);
    let joined = messages[start..].join("\n");
    let (text, truncated) = truncate_left_with_flag(&joined, max_chars);

    if truncated {
        format!("[TRUNCATED: older coach messages omitted]\n{}\n", text)
    } else if text.is_empty() {
        String::new()
    } else {
        format!("{}\n", text)
    }
}

pub(crate) fn truncate_left(text: &str, max_chars: usize) -> String {
    truncate_left_with_flag(text, max_chars).0
}

pub(crate) fn truncate_left_with_flag(text: &str, max_chars: usize) -> (String, bool) {
    let total_chars = text.chars().count();
    if total_chars <= max_chars {
        return (text.to_string(), false);
    }

    if max_chars == 0 {
        return (String::new(), true);
    }

    (
        text.chars()
            .skip(total_chars - max_chars)
            .collect::<String>()
            .trim_start()
            .to_string(),
        true,
    )
}

pub(crate) fn clean_one_line(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn push_clean_line(out: &mut String, text: &str) {
    let text = clean_one_line(text);
    if !text.is_empty() {
        out.push_str(&text);
        out.push('\n');
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input<'a>(
        transcript: &'a [String],
        live_partial: Option<&'a str>,
        coach_bubbles: &'a [String],
        coach_live: Option<&'a str>,
        coach_chat_messages: &'a [CoachChatMessage],
    ) -> ContextInput<'a> {
        ContextInput {
            transcript,
            live_partial,
            coach_bubbles,
            coach_live,
            coach_chat_messages,
        }
    }

    #[test]
    fn truncate_left_keeps_fresh_tail() {
        assert_eq!(truncate_left("abcdef", 3), "def");
        assert_eq!(truncate_left_with_flag("abcdef", 0), (String::new(), true));
        assert_eq!(
            truncate_left_with_flag("abc", 5),
            ("abc".to_string(), false)
        );
    }

    #[test]
    fn help_context_marks_truncated_transcript_and_live_tail() {
        let transcript = vec!["first long line".to_string(), "second line".to_string()];
        let empty = Vec::new();
        let empty_messages = Vec::new();
        let input = input(
            &transcript,
            Some("live partial tail"),
            &empty,
            None,
            &empty_messages,
        );

        let text = render_help_context(
            &input,
            HelpContextSettings {
                max_transcript_chars: 8,
                max_chat_chars: 100,
                max_live_partial_chars: 4,
                max_coach_messages: 3,
            },
        );

        assert!(text.contains("[TRUNCATED: older transcript omitted]"));
        assert!(text.contains("line"));
        assert!(!text.contains("first long line"));
        assert!(text.contains("[LIVE] tail"));
    }

    #[test]
    fn chat_context_includes_prior_roles() {
        let transcript = vec!["Спикер 1: hello".to_string()];
        let empty = Vec::new();
        let messages = vec![
            CoachChatMessage {
                role: CoachChatRole::User,
                text: "Что спросить?".to_string(),
                request_id: None,
                streaming: false,
                help_stage_label: None,
                model_label: None,
            },
            CoachChatMessage {
                role: CoachChatRole::Assistant,
                text: "Уточни цель.".to_string(),
                request_id: Some(1),
                streaming: false,
                help_stage_label: None,
                model_label: None,
            },
        ];

        let text = render_chat_context(&input(&transcript, None, &empty, None, &messages));

        assert!(text.contains("Продавец: Что спросить?"));
        assert!(text.contains("Тренер: Уточни цель."));
    }
}
