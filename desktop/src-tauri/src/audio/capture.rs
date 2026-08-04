use cpal::{
    traits::{DeviceTrait, HostTrait},
    SampleFormat, Stream, StreamConfig,
};
use std::{
    ffi::{c_char, c_void, CStr},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
};
use tokio::sync::mpsc;

pub type AudioChunk = Vec<i16>;
const TARGET_RATE: u32 = 16_000;
const CHUNK_MS: u32 = 20;

pub enum CaptureResource {
    Microphone(Stream),
    System(SystemAudioTap),
}

unsafe impl Send for CaptureResource {}

impl CaptureResource {
    pub fn start(&mut self) -> Result<(), String> {
        match self {
            Self::Microphone(stream) => cpal::traits::StreamTrait::play(stream)
                .map_err(|error| format!("microphone start failed: {error}")),
            Self::System(tap) => tap.start(),
        }
    }
}

pub fn microphone(
    audio_tx: mpsc::Sender<AudioChunk>,
    dropped: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
) -> Result<CaptureResource, String> {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| "no default microphone is available".to_string())?;
    let supported = device
        .default_input_config()
        .map_err(|error| format!("microphone configuration failed: {error}"))?;
    let sample_format = supported.sample_format();
    let config: StreamConfig = supported.into();
    let processor = Arc::new(Mutex::new(AudioProcessor::new(
        config.sample_rate.0,
        config.channels as usize,
        audio_tx,
        dropped,
        stop,
    )));
    let error_callback = |error| eprintln!("REC Coach microphone stream error: {error}");

    macro_rules! build_stream {
        ($sample:ty, $convert:expr) => {{
            let processor = Arc::clone(&processor);
            device.build_input_stream(
                &config,
                move |data: &[$sample], _| {
                    if let Ok(mut processor) = processor.lock() {
                        processor.push_samples(data, $convert);
                    }
                },
                error_callback,
                None,
            )
        }};
    }

    let stream = match sample_format {
        SampleFormat::F32 => build_stream!(f32, |sample: f32| sample),
        SampleFormat::I16 => build_stream!(i16, |sample: i16| sample as f32 / 32768.0),
        SampleFormat::U16 => build_stream!(u16, |sample: u16| (sample as f32 - 32768.0) / 32768.0),
        SampleFormat::F64 => build_stream!(f64, |sample: f64| sample as f32),
        other => return Err(format!("unsupported microphone format: {other:?}")),
    }
    .map_err(|error| format!("microphone setup failed: {error}"))?;

    Ok(CaptureResource::Microphone(stream))
}

pub fn system_audio(
    audio_tx: mpsc::Sender<AudioChunk>,
    dropped: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
) -> Result<CaptureResource, String> {
    SystemAudioTap::create(audio_tx, dropped, stop).map(CaptureResource::System)
}

struct AudioProcessor {
    input_rate: f64,
    channels: usize,
    next_source_frame: f64,
    pending: Vec<i16>,
    audio_tx: mpsc::Sender<AudioChunk>,
    dropped: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
}

impl AudioProcessor {
    fn new(
        input_rate: u32,
        channels: usize,
        audio_tx: mpsc::Sender<AudioChunk>,
        dropped: Arc<AtomicU64>,
        stop: Arc<AtomicBool>,
    ) -> Self {
        Self {
            input_rate: input_rate as f64,
            channels: channels.max(1),
            next_source_frame: 0.0,
            pending: Vec::with_capacity(chunk_samples() * 2),
            audio_tx,
            dropped,
            stop,
        }
    }

    fn push_samples<T: Copy>(&mut self, data: &[T], mut to_f32: impl FnMut(T) -> f32) {
        if self.stop.load(Ordering::Relaxed) || data.is_empty() {
            return;
        }
        let frames = data.len() / self.channels;
        if frames == 0 {
            return;
        }
        let step = self.input_rate / TARGET_RATE as f64;
        while self.next_source_frame < frames as f64 {
            let frame = self.next_source_frame.floor() as usize;
            let start = frame * self.channels;
            let mono = (0..self.channels)
                .map(|channel| to_f32(data[start + channel]))
                .sum::<f32>()
                / self.channels as f32;
            self.pending.push(float_to_i16(mono));
            if self.pending.len() >= chunk_samples() {
                let chunk = self.pending.drain(..chunk_samples()).collect();
                match self.audio_tx.try_send(chunk) {
                    Ok(()) => {}
                    Err(mpsc::error::TrySendError::Full(_)) => {
                        self.dropped.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => {
                        self.stop.store(true, Ordering::Relaxed);
                    }
                }
            }
            self.next_source_frame += step;
        }
        self.next_source_frame -= frames as f64;
    }
}

fn chunk_samples() -> usize {
    TARGET_RATE as usize * CHUNK_MS as usize / 1000
}

fn float_to_i16(sample: f32) -> i16 {
    let sample = sample.clamp(-1.0, 1.0);
    if sample < 0.0 {
        (sample * 32768.0).round() as i16
    } else {
        (sample * 32767.0).round() as i16
    }
}

#[cfg(target_os = "macos")]
mod ffi {
    use std::ffi::{c_char, c_void};

    #[repr(C)]
    pub struct RecSystemAudioTapHandle {
        _private: [u8; 0],
    }

    unsafe extern "C" {
        pub fn rec_system_audio_is_supported() -> bool;
        pub fn rec_system_audio_create(
            callback: extern "C" fn(*mut c_void, *const f32, u32, u32, f64),
            context: *mut c_void,
            out_sample_rate: *mut f64,
            out_channels: *mut u32,
            error_buffer: *mut c_char,
            error_buffer_len: usize,
        ) -> *mut RecSystemAudioTapHandle;
        pub fn rec_system_audio_start(
            handle: *mut RecSystemAudioTapHandle,
            error_buffer: *mut c_char,
            error_buffer_len: usize,
        ) -> bool;
        pub fn rec_system_audio_destroy(handle: *mut RecSystemAudioTapHandle);
    }
}

struct SystemCallbackContext {
    processor: Mutex<Option<AudioProcessor>>,
}

pub struct SystemAudioTap {
    #[cfg(target_os = "macos")]
    handle: *mut ffi::RecSystemAudioTapHandle,
    context: *mut SystemCallbackContext,
}

unsafe impl Send for SystemAudioTap {}

impl SystemAudioTap {
    #[cfg(target_os = "macos")]
    fn create(
        audio_tx: mpsc::Sender<AudioChunk>,
        dropped: Arc<AtomicU64>,
        stop: Arc<AtomicBool>,
    ) -> Result<Self, String> {
        if !unsafe { ffi::rec_system_audio_is_supported() } {
            return Err("system audio requires macOS 14.2 or newer".to_string());
        }
        let context = Box::into_raw(Box::new(SystemCallbackContext {
            processor: Mutex::new(None),
        }));
        let mut sample_rate = 0.0;
        let mut channels = 0_u32;
        let mut error_buffer = [0_i8; 512];
        let handle = unsafe {
            ffi::rec_system_audio_create(
                system_audio_callback,
                context.cast::<c_void>(),
                &mut sample_rate,
                &mut channels,
                error_buffer.as_mut_ptr(),
                error_buffer.len(),
            )
        };
        if handle.is_null() {
            unsafe { drop(Box::from_raw(context)) };
            return Err(read_ffi_error(&error_buffer, "system audio setup failed"));
        }
        if let Ok(mut processor) = unsafe { &*context }.processor.lock() {
            *processor = Some(AudioProcessor::new(
                sample_rate.round().max(1.0) as u32,
                channels.max(1) as usize,
                audio_tx,
                dropped,
                stop,
            ));
        }
        Ok(Self { handle, context })
    }

    #[cfg(not(target_os = "macos"))]
    fn create(
        _audio_tx: mpsc::Sender<AudioChunk>,
        _dropped: Arc<AtomicU64>,
        _stop: Arc<AtomicBool>,
    ) -> Result<Self, String> {
        Err("system audio is available only on macOS".to_string())
    }

    #[cfg(target_os = "macos")]
    fn start(&mut self) -> Result<(), String> {
        let mut error_buffer = [0_i8; 512];
        if unsafe {
            ffi::rec_system_audio_start(self.handle, error_buffer.as_mut_ptr(), error_buffer.len())
        } {
            Ok(())
        } else {
            Err(read_ffi_error(&error_buffer, "system audio start failed"))
        }
    }

    #[cfg(not(target_os = "macos"))]
    fn start(&mut self) -> Result<(), String> {
        Err("system audio is available only on macOS".to_string())
    }
}

impl Drop for SystemAudioTap {
    fn drop(&mut self) {
        #[cfg(target_os = "macos")]
        unsafe {
            if !self.handle.is_null() {
                ffi::rec_system_audio_destroy(self.handle);
            }
        }
        unsafe {
            if !self.context.is_null() {
                drop(Box::from_raw(self.context));
            }
        }
    }
}

#[cfg(target_os = "macos")]
extern "C" fn system_audio_callback(
    context: *mut c_void,
    samples: *const f32,
    frame_count: u32,
    channels: u32,
    _sample_rate: f64,
) {
    if context.is_null() || samples.is_null() || frame_count == 0 {
        return;
    }
    let len = frame_count as usize * channels.max(1) as usize;
    let samples = unsafe { std::slice::from_raw_parts(samples, len) };
    let context = unsafe { &*(context as *mut SystemCallbackContext) };
    if let Ok(mut processor) = context.processor.lock() {
        if let Some(processor) = processor.as_mut() {
            processor.push_samples(samples, |sample| sample);
        }
    }
}

fn read_ffi_error(buffer: &[c_char], fallback: &str) -> String {
    unsafe { CStr::from_ptr(buffer.as_ptr()) }
        .to_str()
        .ok()
        .map(str::trim)
        .filter(|message| !message.is_empty())
        .unwrap_or(fallback)
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audio_processor_downmixes_and_resamples_into_twenty_ms_chunks() {
        let (tx, mut rx) = mpsc::channel(4);
        let dropped = Arc::new(AtomicU64::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let mut processor = AudioProcessor::new(16_000, 2, tx, dropped, stop);
        let stereo = vec![0.5_f32; chunk_samples() * 2];
        processor.push_samples(&stereo, |sample| sample);
        let chunk = rx.try_recv().unwrap();
        assert_eq!(chunk.len(), 320);
        assert!(chunk.iter().all(|sample| *sample > 16_000));
    }
}
