use std::{collections::VecDeque, time::Instant};

const MAX_REFERENCE_MS: u128 = 2_500;
const MIN_LAG_MS: u128 = 20;
const MAX_LAG_MS: u128 = 1_000;
const MIN_SYSTEM_RMS: f64 = 0.0025;
const CORRELATION_REJECT: f64 = 0.62;
const CORRELATION_MAYBE: f64 = 0.45;
const RESIDUAL_SELLER_MIN: f64 = 0.38;

#[derive(Debug, Clone, Copy, Default)]
pub struct EchoDecision {
    pub echo_only: bool,
    pub double_talk: bool,
    pub correlation: f64,
    pub residual_ratio: f64,
    pub lag_ms: u64,
}

struct ReferenceFrame {
    samples: Vec<i16>,
    at: Instant,
    rms: f64,
}

#[derive(Default)]
pub struct EchoClassifier {
    frames: VecDeque<ReferenceFrame>,
}

impl EchoClassifier {
    pub fn remember_system(&mut self, samples: &[i16], at: Instant) {
        let rms = pcm_rms(samples);
        if rms >= MIN_SYSTEM_RMS {
            self.frames.push_back(ReferenceFrame {
                samples: samples.to_vec(),
                at,
                rms,
            });
        }
        while self
            .frames
            .front()
            .is_some_and(|frame| at.duration_since(frame.at).as_millis() > MAX_REFERENCE_MS)
        {
            self.frames.pop_front();
        }
    }

    pub fn classify(&self, microphone: &[i16], at: Instant) -> EchoDecision {
        let mut result = EchoDecision {
            residual_ratio: 1.0,
            ..EchoDecision::default()
        };
        for frame in self.frames.iter().rev() {
            let lag = at.saturating_duration_since(frame.at).as_millis();
            if lag < MIN_LAG_MS {
                continue;
            }
            if lag > MAX_LAG_MS {
                break;
            }
            if frame.rms < MIN_SYSTEM_RMS {
                continue;
            }
            let correlation = normalized_cross_correlation(microphone, &frame.samples);
            if correlation > result.correlation {
                result.correlation = correlation;
                result.residual_ratio = residual_ratio(microphone, &frame.samples);
                result.lag_ms = lag as u64;
            }
        }
        result.echo_only = (result.correlation >= CORRELATION_REJECT
            && result.residual_ratio < RESIDUAL_SELLER_MIN)
            || (result.correlation >= CORRELATION_MAYBE
                && result.residual_ratio < RESIDUAL_SELLER_MIN * 0.7);
        result.double_talk = result.correlation >= CORRELATION_REJECT
            && result.residual_ratio >= RESIDUAL_SELLER_MIN;
        result
    }
}

pub fn pcm_rms(samples: &[i16]) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum = samples
        .iter()
        .map(|sample| {
            let value = *sample as f64 / 32768.0;
            value * value
        })
        .sum::<f64>();
    (sum / samples.len() as f64).sqrt()
}

fn normalized_cross_correlation(left: &[i16], right: &[i16]) -> f64 {
    let len = left.len().min(right.len());
    if len < 32 {
        return 0.0;
    }
    let stride = (len / 256).max(1);
    let mut dot = 0.0;
    let mut aa = 0.0;
    let mut bb = 0.0;
    for index in (0..len).step_by(stride) {
        let a = left[index] as f64;
        let b = right[index] as f64;
        dot += a * b;
        aa += a * a;
        bb += b * b;
    }
    if aa == 0.0 || bb == 0.0 {
        0.0
    } else {
        (dot / (aa * bb).sqrt()).abs()
    }
}

fn residual_ratio(microphone: &[i16], reference: &[i16]) -> f64 {
    let len = microphone.len().min(reference.len());
    if len < 32 {
        return 1.0;
    }
    let stride = (len / 256).max(1);
    let mut dot = 0.0;
    let mut reference_power = 0.0;
    let mut microphone_power = 0.0;
    for index in (0..len).step_by(stride) {
        let mic = microphone[index] as f64;
        let far = reference[index] as f64;
        dot += mic * far;
        reference_power += far * far;
        microphone_power += mic * mic;
    }
    if reference_power == 0.0 || microphone_power == 0.0 {
        return 1.0;
    }
    let alpha = dot / reference_power;
    let residual = (0..len)
        .step_by(stride)
        .map(|index| microphone[index] as f64 - alpha * reference[index] as f64)
        .map(|value| value * value)
        .sum::<f64>();
    (residual / microphone_power).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matching_delayed_frame_is_classified_as_echo() {
        let mut classifier = EchoClassifier::default();
        let now = Instant::now();
        let signal: Vec<i16> = (0..320).map(|index| ((index % 31) * 700) as i16).collect();
        classifier.remember_system(&signal, now);
        let decision = classifier.classify(&signal, now + std::time::Duration::from_millis(80));
        assert!(decision.echo_only);
        assert!(decision.correlation > 0.99);
    }
}
