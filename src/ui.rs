use crate::context::{CoachChatMessage, CoachChatRole};
use eframe::egui::{self, RichText};

pub(crate) fn apply_liquid_glass_style(ctx: &egui::Context) {
    let mut visuals = egui::Visuals::dark();
    visuals.override_text_color = Some(egui::Color32::from_rgb(235, 242, 248));
    visuals.panel_fill = egui::Color32::TRANSPARENT;
    visuals.window_fill = egui::Color32::from_rgb(22, 29, 42);
    visuals.window_corner_radius = egui::CornerRadius::same(16);
    visuals.faint_bg_color = egui::Color32::from_rgba_unmultiplied(255, 255, 255, 18);
    visuals.extreme_bg_color = egui::Color32::from_rgba_unmultiplied(4, 8, 16, 220);
    visuals.hyperlink_color = egui::Color32::from_rgb(126, 210, 255);
    visuals.selection.bg_fill = egui::Color32::from_rgb(56, 128, 190);
    visuals.widgets.noninteractive.bg_fill =
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 12);
    visuals.widgets.noninteractive.bg_stroke = egui::Stroke::new(
        1.0,
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 42),
    );
    visuals.widgets.inactive.bg_fill = egui::Color32::from_rgba_unmultiplied(255, 255, 255, 28);
    visuals.widgets.inactive.weak_bg_fill =
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 18);
    visuals.widgets.inactive.bg_stroke = egui::Stroke::new(
        1.0,
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 54),
    );
    visuals.widgets.hovered.bg_fill = egui::Color32::from_rgba_unmultiplied(255, 255, 255, 42);
    visuals.widgets.hovered.bg_stroke = egui::Stroke::new(
        1.0,
        egui::Color32::from_rgba_unmultiplied(190, 230, 255, 110),
    );
    visuals.widgets.active.bg_fill = egui::Color32::from_rgba_unmultiplied(91, 171, 226, 96);
    visuals.widgets.active.bg_stroke = egui::Stroke::new(
        1.0,
        egui::Color32::from_rgba_unmultiplied(200, 238, 255, 150),
    );
    visuals.widgets.open.bg_fill = visuals.widgets.hovered.bg_fill;
    visuals.widgets.open.bg_stroke = visuals.widgets.hovered.bg_stroke;
    visuals.window_shadow = glass_shadow();
    visuals.popup_shadow = glass_shadow();
    ctx.set_visuals(visuals);

    let mut style = (*ctx.style()).clone();
    style.spacing.item_spacing = egui::vec2(8.0, 8.0);
    style.spacing.button_padding = egui::vec2(12.0, 7.0);
    style.spacing.window_margin = egui::Margin::same(14);
    ctx.set_style(style);
}

pub(crate) fn app_background_frame() -> egui::Frame {
    transparent_frame()
}

pub(crate) fn transparent_frame() -> egui::Frame {
    egui::Frame::new().fill(egui::Color32::TRANSPARENT)
}

pub(crate) fn toolbar_frame() -> egui::Frame {
    egui::Frame::new()
        .fill(egui::Color32::from_rgba_unmultiplied(18, 25, 38, 178))
        .stroke(egui::Stroke::new(
            1.0,
            egui::Color32::from_rgba_unmultiplied(255, 255, 255, 28),
        ))
        .inner_margin(egui::Margin::symmetric(14, 10))
}

pub(crate) fn glass_panel_frame() -> egui::Frame {
    tinted_glass_frame(
        egui::Color32::from_rgba_unmultiplied(42, 50, 64, 138),
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 76),
    )
}

pub(crate) fn compact_overlay_frame() -> egui::Frame {
    tinted_glass_frame(
        egui::Color32::from_rgba_unmultiplied(18, 24, 34, 168),
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 92),
    )
    .inner_margin(egui::Margin::symmetric(12, 10))
    .outer_margin(egui::Margin::same(6))
}

pub(crate) fn tinted_glass_frame(fill: egui::Color32, stroke: egui::Color32) -> egui::Frame {
    egui::Frame::new()
        .fill(fill)
        .stroke(egui::Stroke::new(1.0, stroke))
        .corner_radius(egui::CornerRadius::same(18))
        .inner_margin(egui::Margin::same(14))
        .outer_margin(egui::Margin::same(8))
        .shadow(glass_shadow())
}

pub(crate) fn section_header(ui: &mut egui::Ui, title: &str, detail: impl AsRef<str>) {
    ui.horizontal_wrapped(|ui| {
        ui.label(RichText::new(title).size(16.0).strong());
        let detail = detail.as_ref();
        if !detail.is_empty() {
            ui.label(
                RichText::new(detail)
                    .size(12.0)
                    .color(egui::Color32::from_rgb(168, 180, 192)),
            );
        }
    });
}

fn glass_shadow() -> egui::Shadow {
    egui::Shadow {
        offset: [0, 12],
        blur: 28,
        spread: 0,
        color: egui::Color32::from_rgba_unmultiplied(0, 0, 0, 88),
    }
}

pub(crate) fn draw_bubble(ui: &mut egui::Ui, text: &str) {
    glass_panel_frame().show(ui, |ui| {
        ui.set_width(ui.available_width() - 12.0);
        draw_dialogue_text(ui, text, false);
    });

    ui.add_space(8.0);
}

pub(crate) fn draw_live_bubble(ui: &mut egui::Ui, text: &str) {
    tinted_glass_frame(
        egui::Color32::from_rgba_unmultiplied(116, 208, 255, 36),
        egui::Color32::from_rgba_unmultiplied(135, 220, 255, 92),
    )
    .show(ui, |ui| {
        ui.set_width(ui.available_width() - 12.0);
        draw_dialogue_text(ui, text, true);
    });

    ui.add_space(8.0);
}

pub(crate) fn draw_coach_bubble(ui: &mut egui::Ui, text: &str, live: bool) {
    let fill = if live {
        egui::Color32::from_rgba_unmultiplied(107, 227, 179, 36)
    } else {
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 24)
    };

    tinted_glass_frame(
        fill,
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 68),
    )
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

    tinted_glass_frame(
        fill,
        egui::Color32::from_rgba_unmultiplied(255, 255, 255, 56),
    )
    .show(ui, |ui| {
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
