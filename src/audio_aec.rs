use std::{error::Error as StdError, fmt};

use serde::Serialize;
use webrtc_audio_processing::{
    config::{EchoCanceller, HighPassFilter, NoiseSuppression, NoiseSuppressionLevel},
    Config, Processor,
};

#[derive(Debug)]
pub enum Aec3Error {
    UnsupportedSampleRate(u32),
    WebRtc(webrtc_audio_processing::Error),
}

impl fmt::Display for Aec3Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedSampleRate(rate) => {
                write!(f, "unsupported AEC3 sample rate: {rate}")
            }
            Self::WebRtc(error) => write!(f, "webrtc audio processing failed: {error}"),
        }
    }
}

impl StdError for Aec3Error {}

impl From<webrtc_audio_processing::Error> for Aec3Error {
    fn from(value: webrtc_audio_processing::Error) -> Self {
        Self::WebRtc(value)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Aec3Stats {
    pub sample_rate_hz: u32,
    pub frame_samples: usize,
    pub render_frames: u64,
    pub capture_frames: u64,
    pub pending_render_samples: usize,
    pub pending_capture_samples: usize,
    pub echo_return_loss: Option<f64>,
    pub echo_return_loss_enhancement: Option<f64>,
    pub residual_echo_likelihood: Option<f64>,
    pub residual_echo_likelihood_recent_max: Option<f64>,
    pub delay_ms: Option<u32>,
}

#[derive(Debug)]
pub struct Aec3Processor {
    processor: Processor,
    sample_rate_hz: u32,
    frame_samples: usize,
    render_pending: Vec<i16>,
    capture_pending: Vec<i16>,
    render_frames: u64,
    capture_frames: u64,
}

impl Aec3Processor {
    pub fn new(sample_rate_hz: u32) -> Result<Self, Aec3Error> {
        match sample_rate_hz {
            8_000 | 16_000 | 32_000 | 48_000 => {}
            other => return Err(Aec3Error::UnsupportedSampleRate(other)),
        }

        let processor = Processor::new(sample_rate_hz)?;
        processor.set_config(Config {
            high_pass_filter: Some(HighPassFilter::default()),
            echo_canceller: Some(EchoCanceller::Full {
                stream_delay_ms: None,
            }),
            noise_suppression: Some(NoiseSuppression {
                level: NoiseSuppressionLevel::Low,
                analyze_linear_aec_output: false,
            }),
            ..Default::default()
        });

        let frame_samples = processor.num_samples_per_frame();
        Ok(Self {
            processor,
            sample_rate_hz,
            frame_samples,
            render_pending: Vec::with_capacity(frame_samples * 2),
            capture_pending: Vec::with_capacity(frame_samples * 2),
            render_frames: 0,
            capture_frames: 0,
        })
    }

    pub fn sample_rate_hz(&self) -> u32 {
        self.sample_rate_hz
    }

    pub fn frame_samples(&self) -> usize {
        self.frame_samples
    }

    pub fn reset(&mut self) {
        self.processor.reinitialize();
        self.render_pending.clear();
        self.capture_pending.clear();
        self.render_frames = 0;
        self.capture_frames = 0;
    }

    pub fn process_render_pcm16(&mut self, samples: &[i16]) -> Result<usize, Aec3Error> {
        self.render_pending.extend_from_slice(samples);
        let mut frames = 0;
        while self.render_pending.len() >= self.frame_samples {
            let chunk: Vec<i16> = self.render_pending.drain(..self.frame_samples).collect();
            self.analyze_render_frame(&chunk)?;
            frames += 1;
        }
        Ok(frames)
    }

    pub fn process_capture_pcm16(&mut self, samples: &[i16]) -> Result<Vec<i16>, Aec3Error> {
        self.capture_pending.extend_from_slice(samples);
        let mut output = Vec::with_capacity(samples.len());
        while self.capture_pending.len() >= self.frame_samples {
            let chunk: Vec<i16> = self.capture_pending.drain(..self.frame_samples).collect();
            output.extend(self.process_capture_frame(&chunk)?);
        }
        Ok(output)
    }

    pub fn flush_capture_pcm16(&mut self) -> Result<Vec<i16>, Aec3Error> {
        if self.capture_pending.is_empty() {
            return Ok(Vec::new());
        }

        let original_len = self.capture_pending.len();
        let mut chunk = std::mem::take(&mut self.capture_pending);
        chunk.resize(self.frame_samples, 0);
        let mut output = self.process_capture_frame(&chunk)?;
        output.truncate(original_len);
        Ok(output)
    }

    pub fn stats(&self) -> Aec3Stats {
        let stats = self.processor.get_stats();
        Aec3Stats {
            sample_rate_hz: self.sample_rate_hz,
            frame_samples: self.frame_samples,
            render_frames: self.render_frames,
            capture_frames: self.capture_frames,
            pending_render_samples: self.render_pending.len(),
            pending_capture_samples: self.capture_pending.len(),
            echo_return_loss: stats.echo_return_loss,
            echo_return_loss_enhancement: stats.echo_return_loss_enhancement,
            residual_echo_likelihood: stats.residual_echo_likelihood,
            residual_echo_likelihood_recent_max: stats.residual_echo_likelihood_recent_max,
            delay_ms: stats.delay_ms,
        }
    }

    fn analyze_render_frame(&mut self, samples: &[i16]) -> Result<(), Aec3Error> {
        let frame = vec![pcm16_to_f32(samples)];
        self.processor.analyze_render_frame(&frame)?;
        self.render_frames += 1;
        Ok(())
    }

    fn process_capture_frame(&mut self, samples: &[i16]) -> Result<Vec<i16>, Aec3Error> {
        let mut frame = vec![pcm16_to_f32(samples)];
        self.processor.process_capture_frame(&mut frame)?;
        self.capture_frames += 1;
        Ok(f32_to_pcm16(&frame[0]))
    }
}

pub fn pcm16_to_f32(samples: &[i16]) -> Vec<f32> {
    samples
        .iter()
        .map(|sample| f32::from(*sample) / 32768.0)
        .collect()
}

pub fn f32_to_pcm16(samples: &[f32]) -> Vec<i16> {
    samples
        .iter()
        .map(|sample| {
            let clamped = sample.clamp(-1.0, 1.0);
            if clamped < 0.0 {
                (clamped * 32768.0).round() as i16
            } else {
                (clamped * 32767.0).round() as i16
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buffers_until_ten_millisecond_capture_frame() {
        let mut processor = Aec3Processor::new(16_000).unwrap();
        assert_eq!(processor.frame_samples(), 160);

        let first = processor.process_capture_pcm16(&[0; 80]).unwrap();
        assert!(first.is_empty());
        assert_eq!(processor.stats().pending_capture_samples, 80);

        let second = processor.process_capture_pcm16(&[0; 80]).unwrap();
        assert_eq!(second.len(), 160);
        assert_eq!(processor.stats().pending_capture_samples, 0);
        assert_eq!(processor.stats().capture_frames, 1);
    }

    #[test]
    fn flushes_partial_capture_frame_without_leaking_padding() {
        let mut processor = Aec3Processor::new(16_000).unwrap();
        let first = processor.process_capture_pcm16(&[0; 32]).unwrap();
        assert!(first.is_empty());

        let flushed = processor.flush_capture_pcm16().unwrap();
        assert_eq!(flushed.len(), 32);
        assert_eq!(processor.stats().pending_capture_samples, 0);
        assert_eq!(processor.stats().capture_frames, 1);
    }

    #[test]
    fn render_path_tracks_full_frames() {
        let mut processor = Aec3Processor::new(16_000).unwrap();
        let processed = processor.process_render_pcm16(&[0; 320]).unwrap();
        assert_eq!(processed, 2);
        assert_eq!(processor.stats().render_frames, 2);
    }

    #[test]
    fn rejects_non_webrtc_sample_rates() {
        let error = Aec3Processor::new(44_100).unwrap_err();
        assert!(matches!(error, Aec3Error::UnsupportedSampleRate(44_100)));
    }
}
