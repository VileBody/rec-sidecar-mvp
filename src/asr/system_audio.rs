use super::{audio::AudioProcessor, BoxError};
use std::{
    ffi::{c_char, c_void, CStr},
    sync::{
        atomic::{AtomicBool, AtomicU64},
        Arc, Mutex,
    },
};
use tokio::sync::mpsc;

use super::audio::AudioChunk;

#[cfg(target_os = "macos")]
mod ffi {
    use std::ffi::{c_char, c_void};

    #[repr(C)]
    pub(super) struct RecSystemAudioTapHandle {
        _private: [u8; 0],
    }

    unsafe extern "C" {
        pub(super) fn rec_system_audio_is_supported() -> bool;
        pub(super) fn rec_system_audio_create(
            callback: extern "C" fn(*mut c_void, *const f32, u32, u32, f64),
            context: *mut c_void,
            out_sample_rate: *mut f64,
            out_channels: *mut u32,
            error_buffer: *mut c_char,
            error_buffer_len: usize,
        ) -> *mut RecSystemAudioTapHandle;
        pub(super) fn rec_system_audio_start(
            handle: *mut RecSystemAudioTapHandle,
            error_buffer: *mut c_char,
            error_buffer_len: usize,
        ) -> bool;
        pub(super) fn rec_system_audio_destroy(handle: *mut RecSystemAudioTapHandle);
    }
}

#[cfg(not(target_os = "macos"))]
mod ffi {}

struct CallbackContext {
    processor: Mutex<Option<AudioProcessor>>,
}

pub(super) struct SystemAudioTap {
    #[cfg(target_os = "macos")]
    handle: *mut ffi::RecSystemAudioTapHandle,
    context: *mut CallbackContext,
    sample_rate: u32,
    channels: usize,
}

impl SystemAudioTap {
    #[cfg(target_os = "macos")]
    pub(super) fn is_supported() -> bool {
        unsafe { ffi::rec_system_audio_is_supported() }
    }

    #[cfg(not(target_os = "macos"))]
    pub(super) fn is_supported() -> bool {
        false
    }

    #[cfg(target_os = "macos")]
    pub(super) fn create(
        target_rate: u32,
        chunk_ms: u32,
        audio_tx: mpsc::Sender<AudioChunk>,
        dropped_audio_chunks: Arc<AtomicU64>,
        stop: Arc<AtomicBool>,
    ) -> Result<Self, BoxError> {
        if !Self::is_supported() {
            return Err("Core Audio system taps require macOS 14.2 or newer".into());
        }

        let context = Box::into_raw(Box::new(CallbackContext {
            processor: Mutex::new(None),
        }));
        let mut sample_rate = 0.0_f64;
        let mut channels = 0_u32;
        let mut error_buffer = [0_i8; 512];
        let handle = unsafe {
            ffi::rec_system_audio_create(
                system_audio_samples_callback,
                context.cast::<c_void>(),
                &mut sample_rate,
                &mut channels,
                error_buffer.as_mut_ptr(),
                error_buffer.len(),
            )
        };

        if handle.is_null() {
            unsafe {
                drop(Box::from_raw(context));
            }
            return Err(read_error(
                &error_buffer,
                "failed to create Core Audio system tap",
            ));
        }

        let input_rate = sample_rate.round().max(1.0) as u32;
        let input_channels = channels.max(1) as usize;
        let processor = AudioProcessor::new(
            input_rate,
            input_channels,
            target_rate,
            chunk_ms,
            audio_tx,
            dropped_audio_chunks,
            stop,
        );
        if let Ok(mut guard) = unsafe { &*context }.processor.lock() {
            *guard = Some(processor);
        }

        Ok(Self {
            handle,
            context,
            sample_rate: input_rate,
            channels: input_channels,
        })
    }

    #[cfg(not(target_os = "macos"))]
    pub(super) fn create(
        _target_rate: u32,
        _chunk_ms: u32,
        _audio_tx: mpsc::Sender<AudioChunk>,
        _dropped_audio_chunks: Arc<AtomicU64>,
        _stop: Arc<AtomicBool>,
    ) -> Result<Self, BoxError> {
        Err("system audio taps are only supported on macOS".into())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn start(&mut self) -> Result<(), BoxError> {
        let mut error_buffer = [0_i8; 512];
        let ok = unsafe {
            ffi::rec_system_audio_start(self.handle, error_buffer.as_mut_ptr(), error_buffer.len())
        };
        if ok {
            Ok(())
        } else {
            Err(read_error(
                &error_buffer,
                "failed to start Core Audio system tap",
            ))
        }
    }

    #[cfg(not(target_os = "macos"))]
    pub(super) fn start(&mut self) -> Result<(), BoxError> {
        Err("system audio taps are only supported on macOS".into())
    }

    pub(super) fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    pub(super) fn channels(&self) -> usize {
        self.channels
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
extern "C" fn system_audio_samples_callback(
    context: *mut c_void,
    samples: *const f32,
    frame_count: u32,
    channels: u32,
    _sample_rate: f64,
) {
    if context.is_null() || samples.is_null() || frame_count == 0 {
        return;
    }

    let sample_count = frame_count as usize * channels.max(1) as usize;
    let slice = unsafe { std::slice::from_raw_parts(samples, sample_count) };
    let context = unsafe { &*(context as *mut CallbackContext) };

    if let Ok(mut guard) = context.processor.lock() {
        if let Some(processor) = guard.as_mut() {
            processor.push_samples(slice, |sample| sample);
        }
    }
}

fn read_error(buffer: &[c_char], fallback: &str) -> BoxError {
    let message = unsafe { CStr::from_ptr(buffer.as_ptr()) }
        .to_str()
        .ok()
        .map(str::trim)
        .filter(|message| !message.is_empty())
        .unwrap_or(fallback);
    message.to_string().into()
}
