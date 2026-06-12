use crate::context::{CoachChatMessage, CoachChatRole};
use eframe::egui::{self, RichText};

pub(crate) fn side_panel_layout(ctx: &egui::Context) -> (f32, f32, f32, f32) {
    let monitor = ctx.input(|input| {
        input
            .viewport()
            .monitor_size
            .unwrap_or_else(|| egui::vec2(1440.0, 900.0))
    });
    let center_min = (monitor.x * 0.42).clamp(320.0, 760.0);
    let max_panel_width = ((monitor.x - center_min) / 2.0).max(240.0);
    let left_width = (monitor.x * 0.20).clamp(300.0, 370.0).min(max_panel_width);
    let right_width = (monitor.x * 0.22).clamp(330.0, 420.0).min(max_panel_width);
    let panel_height = monitor.y.max(520.0);

    (left_width, right_width, panel_height, monitor.x)
}

pub(crate) fn show_panel_header(ctx: &egui::Context, ui: &mut egui::Ui, title: &str) {
    ui.horizontal_wrapped(|ui| {
        ui.heading(title);

        let drag_handle = ui.add(egui::Label::new("двигать").sense(egui::Sense::drag()));
        let is_dragging = drag_handle.dragged();
        let _drag_handle = drag_handle.on_hover_cursor(if is_dragging {
            egui::CursorIcon::Grabbing
        } else {
            egui::CursorIcon::Grab
        });

        if is_dragging {
            drag_viewport_by_pointer_delta(ctx);
        }
    });
}

pub(crate) fn draw_bubble(ui: &mut egui::Ui, text: &str) {
    egui::Frame::group(ui.style()).show(ui, |ui| {
        ui.set_width(ui.available_width() - 12.0);
        draw_dialogue_text(ui, text, false);
    });

    ui.add_space(8.0);
}

pub(crate) fn draw_live_bubble(ui: &mut egui::Ui, text: &str) {
    egui::Frame::group(ui.style())
        .fill(ui.visuals().faint_bg_color)
        .show(ui, |ui| {
            ui.set_width(ui.available_width() - 12.0);
            draw_dialogue_text(ui, text, true);
        });

    ui.add_space(8.0);
}

pub(crate) fn draw_coach_bubble(ui: &mut egui::Ui, text: &str, live: bool) {
    egui::Frame::group(ui.style())
        .fill(ui.visuals().faint_bg_color)
        .show(ui, |ui| {
            ui.set_width(ui.available_width() - 12.0);
            ui.label(RichText::new("Тренер").strong().size(13.0));

            let text = if live {
                format!("{} ...", text.trim())
            } else {
                text.trim().to_string()
            };

            ui.add(egui::Label::new(text).wrap());
        });

    ui.add_space(8.0);
}

pub(crate) fn draw_chat_message(ui: &mut egui::Ui, message: &CoachChatMessage) {
    let fill = match message.role {
        CoachChatRole::User => ui.visuals().extreme_bg_color,
        CoachChatRole::Assistant => ui.visuals().faint_bg_color,
    };
    let label = match message.role {
        CoachChatRole::User => "Вы",
        CoachChatRole::Assistant => "Тренер",
    };

    egui::Frame::group(ui.style()).fill(fill).show(ui, |ui| {
        ui.set_width(ui.available_width() - 12.0);
        ui.horizontal(|ui| {
            ui.label(RichText::new(label).strong().size(13.0));

            if let Some(model) = message.model_label.as_deref() {
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(RichText::new(model).italics().size(11.0));
                });
            }
        });

        if let Some(stage_label) = message
            .help_stage_label
            .filter(|_| !message.text.trim().is_empty())
        {
            ui.label(RichText::new(stage_label).italics().size(12.0));
        }

        let text = if message.streaming && message.text.trim().is_empty() {
            message.help_stage_label.unwrap_or("думаю...").to_string()
        } else {
            message.text.trim().to_string()
        };

        match message.role {
            CoachChatRole::Assistant => draw_coach_markdown(ui, &text),
            CoachChatRole::User => {
                ui.add(egui::Label::new(text).wrap());
            }
        }
    });

    ui.add_space(8.0);
}

fn drag_viewport_by_pointer_delta(ctx: &egui::Context) {
    let next_position = ctx.input(|input| {
        let delta = input.pointer.delta();
        if delta.length_sq() <= f32::EPSILON {
            return None;
        }

        input
            .viewport()
            .outer_rect
            .map(|outer_rect| outer_rect.min + delta)
    });

    if let Some(position) = next_position {
        ctx.send_viewport_cmd(egui::ViewportCommand::OuterPosition(position));
        ctx.request_repaint();
    }
}

fn draw_coach_markdown(ui: &mut egui::Ui, text: &str) {
    let mut drew_anything = false;
    let mut lines = text.lines().peekable();

    while let Some(raw_line) = lines.next() {
        let line = raw_line.trim();
        if line.is_empty() {
            ui.add_space(4.0);
            continue;
        }

        drew_anything = true;

        if is_say_now_heading(line) {
            let mut read_aloud_lines = Vec::new();
            while let Some(next_line) = lines.peek() {
                let next_line = next_line.trim();
                if next_line.is_empty() {
                    let _ = lines.next();
                    break;
                }

                let Some(read_aloud) = next_line.strip_prefix('>') else {
                    break;
                };

                read_aloud_lines.push(strip_basic_markdown(read_aloud.trim()));
                let _ = lines.next();
            }

            draw_guidance_block(ui, "Сказать сейчас", &read_aloud_lines.join(" "), true);
        } else if is_next_step_heading(line) {
            let mut next_step_lines = Vec::new();
            if let Some(body) = next_step_heading_body(line) {
                next_step_lines.push(body);
            }

            for next_line in lines.by_ref() {
                let next_line = next_line.trim();
                if let Some(body) = next_step_heading_body(next_line) {
                    if !body.is_empty() {
                        next_step_lines.push(body);
                    }
                } else {
                    next_step_lines.push(next_line.to_string());
                }
            }

            draw_guidance_block(ui, "Следующий ход", &next_step_lines.join("\n"), false);
            break;
        } else if let Some(read_aloud) = line.strip_prefix('>') {
            let read_aloud = strip_basic_markdown(read_aloud.trim());
            draw_guidance_block(ui, "Читать", &read_aloud, true);
        } else if line.starts_with("_Комментарий:_") || line.starts_with("*Комментарий:*")
        {
            ui.label(RichText::new(strip_basic_markdown(line)).italics());
        } else if let Some((label, body)) = split_markdown_label(line) {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(label).strong());
                if !body.trim().is_empty() {
                    ui.label(strip_basic_markdown(&body));
                }
            });
        } else if let Some(item) = line.strip_prefix("- ") {
            ui.label(format!("• {}", strip_basic_markdown(item)));
        } else {
            ui.add(egui::Label::new(strip_basic_markdown(line)).wrap());
        }
    }

    if !drew_anything {
        ui.label("думаю...");
    }
}

fn is_say_now_heading(line: &str) -> bool {
    strip_basic_markdown(line).trim_end_matches(':') == "Сказать сейчас"
}

fn is_next_step_heading(line: &str) -> bool {
    next_step_heading_body(line).is_some()
}

fn next_step_heading_body(line: &str) -> Option<String> {
    let line = line.trim();

    if let Some((label, body)) = split_markdown_label(line) {
        if label.trim_end_matches(':') == "Следующий ход" {
            return Some(body.trim().to_string());
        }
    }

    let stripped = strip_basic_markdown(line);
    let stripped = stripped.trim();

    if stripped.trim_end_matches(':') == "Следующий ход" {
        return Some(String::new());
    }

    stripped
        .strip_prefix("Следующий ход:")
        .map(|body| body.trim().to_string())
}

fn draw_guidance_block(ui: &mut egui::Ui, title: &str, text: &str, strong_body: bool) {
    let text = text.trim();
    if title.trim().is_empty() && text.is_empty() {
        return;
    }

    let fill = egui::Color32::from_rgb(229, 247, 255);
    let stroke = egui::Stroke::new(1.0, egui::Color32::from_rgb(119, 199, 247));
    egui::Frame::new()
        .fill(fill)
        .stroke(stroke)
        .corner_radius(egui::CornerRadius::same(6))
        .inner_margin(egui::Margin::symmetric(8, 6))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.label(RichText::new(title).strong().size(12.0));
            for line in text.lines() {
                let line = line.trim();
                if line.is_empty() {
                    ui.add_space(4.0);
                } else if line.starts_with("_Комментарий:_") || line.starts_with("*Комментарий:*")
                {
                    ui.label(RichText::new(strip_basic_markdown(line)).italics());
                } else if let Some((label, body)) = split_markdown_label(line) {
                    ui.horizontal_wrapped(|ui| {
                        ui.label(RichText::new(label).strong());
                        if !body.trim().is_empty() {
                            ui.label(strip_basic_markdown(&body));
                        }
                    });
                } else if strong_body {
                    ui.add(
                        egui::Label::new(RichText::new(strip_basic_markdown(line)).strong()).wrap(),
                    );
                } else {
                    ui.add(egui::Label::new(strip_basic_markdown(line)).wrap());
                }
            }
        });
}

pub(crate) fn split_markdown_label(line: &str) -> Option<(String, String)> {
    let rest = line.strip_prefix("**")?;
    let end = rest.find("**")?;
    let label = strip_basic_markdown(&rest[..end]);
    let body = strip_basic_markdown(rest[end + 2..].trim());
    Some((label, body))
}

pub(crate) fn strip_basic_markdown(text: &str) -> String {
    text.trim()
        .trim_matches('_')
        .trim_matches('*')
        .replace("**", "")
        .replace('`', "")
        .trim()
        .to_string()
}

fn draw_dialogue_text(ui: &mut egui::Ui, text: &str, live: bool) {
    let text = text.trim();

    if let Some((label, body)) = split_dialogue_label(text) {
        ui.label(RichText::new(label).strong().size(13.0));
        let body = if live {
            format!("{} ...", body.trim())
        } else {
            body.trim().to_string()
        };
        ui.add(egui::Label::new(body).wrap());
    } else {
        let text = if live {
            format!("{} ...", text)
        } else {
            text.to_string()
        };
        ui.add(egui::Label::new(text).wrap());
    }
}

pub(crate) fn split_dialogue_label(text: &str) -> Option<(&str, &str)> {
    let colon = text.find(':')?;
    if colon > 32 {
        return None;
    }

    let label = &text[..=colon];
    if label.starts_with("Спикер ") || label.starts_with("Канал ") {
        Some((label, &text[colon + 1..]))
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_basic_markdown_removes_simple_markup() {
        assert_eq!(
            strip_basic_markdown(" **Уточнить:** `тест` "),
            "Уточнить: тест"
        );
    }

    #[test]
    fn split_dialogue_label_accepts_speaker_and_channel() {
        assert_eq!(
            split_dialogue_label("Спикер 1: привет"),
            Some(("Спикер 1:", " привет"))
        );
        assert_eq!(
            split_dialogue_label("Канал 2: hello"),
            Some(("Канал 2:", " hello"))
        );
        assert_eq!(split_dialogue_label("Other: hello"), None);
    }
}
