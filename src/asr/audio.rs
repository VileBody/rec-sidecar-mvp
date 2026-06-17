use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc,
};
use std::time::Duration;
use tokio::sync::mpsc;

pub(super) type AudioChunk = Vec<i16>;

pub(super) struct AudioBatch {
    pub(super) bytes: Vec<u8>,
    pub(super) chunk_count: usize,
    pub(super) flushed_chunks: usize,
}

pub(super) fn chunks_for_duration(duration: Duration, chunk_ms: u32) -> usize {
    let chunk_ms = chunk_ms.max(1) as u128;
    duration
        .as_millis()
        .div_ceil(chunk_ms)
        .max(1)
        .try_into()
        .unwrap_or(usize::MAX)
}

pub(super) fn chunk_samples(target_rate: u32, chunk_ms: u32) -> usize {
    (target_rate as usize * chunk_ms as usize / 1000).max(1)
}

pub(super) fn build_audio_batch(
    first_chunk: AudioChunk,
    audio_rx: &mut mpsc::Receiver<AudioChunk>,
    max_batch_chunks: usize,
    flush_latency_chunks: Option<usize>,
) -> Option<AudioBatch> {
    let max_batch_chunks = max_batch_chunks.max(1);
    let mut first_chunk = Some(first_chunk);
    let mut flushed_chunks = 0_usize;
    let queued_chunks = 1 + audio_rx.len();

    if let Some(critical_chunks) = flush_latency_chunks {
        if queued_chunks >= critical_chunks.max(1) {
            let target_chunks = (critical_chunks / 2).max(max_batch_chunks).max(1);
            let mut chunks_to_flush = queued_chunks.saturating_sub(target_chunks);

            if chunks_to_flush > 0 {
                first_chunk = None;
                flushed_chunks += 1;
                chunks_to_flush -= 1;
            }

            for _ in 0..chunks_to_flush {
                match audio_rx.try_recv() {
                    Ok(_) => flushed_chunks += 1,
                    Err(_) => break,
                }
            }

            if first_chunk.is_none() {
                first_chunk = audio_rx.try_recv().ok();
            }

            if flushed_chunks > 0 {
                super::debug_log(format!(
                    "audio backlog half-flush queued_chunks={} flushed_chunks={} target_chunks={}",
                    queued_chunks, flushed_chunks, target_chunks
                ));
            }
        }
    }

    let first_chunk = first_chunk?;
    let mut chunks = Vec::with_capacity(max_batch_chunks);
    chunks.push(first_chunk);
    let mut chunk_count = 1_usize;

    while chunk_count < max_batch_chunks {
        match audio_rx.try_recv() {
            Ok(chunk) => {
                chunks.push(chunk);
                chunk_count += 1;
            }
            Err(_) => break,
        }
    }

    let sample_count: usize = chunks.iter().map(Vec::len).sum();
    let mut bytes = Vec::with_capacity(sample_count * 2);
    for chunk in chunks {
        for sample in chunk {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
    }

    Some(AudioBatch {
        bytes,
        chunk_count,
        flushed_chunks,
    })
}

pub(super) struct AudioProcessor {
    input_rate: f64,
    target_rate: f64,
    channels: usize,
    next_source_frame: f64,
    chunk_samples: usize,
    pending: Vec<i16>,
    audio_tx: mpsc::Sender<AudioChunk>,
    dropped_audio_chunks: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
}

impl AudioProcessor {
    pub(super) fn new(
        input_rate: u32,
        channels: usize,
        target_rate: u32,
        chunk_ms: u32,
        audio_tx: mpsc::Sender<AudioChunk>,
        dropped_audio_chunks: Arc<AtomicU64>,
        stop: Arc<AtomicBool>,
    ) -> Self {
        let chunk_samples = chunk_samples(target_rate, chunk_ms);

        Self {
            input_rate: input_rate as f64,
            target_rate: target_rate as f64,
            channels: channels.max(1),
            next_source_frame: 0.0,
            chunk_samples,
            pending: Vec::with_capacity(chunk_samples * 2),
            audio_tx,
            dropped_audio_chunks,
            stop,
        }
    }

    pub(super) fn push_samples<T>(&mut self, data: &[T], mut to_f32: impl FnMut(T) -> f32)
    where
        T: Copy,
    {
        if self.stop.load(Ordering::Relaxed) || data.is_empty() {
            return;
        }

        let frames = data.len() / self.channels;
        if frames == 0 {
            return;
        }

        let step = self.input_rate / self.target_rate;

        while self.next_source_frame < frames as f64 {
            let frame = self.next_source_frame.floor() as usize;
            let start = frame * self.channels;
            let mut mono = 0.0;

            for channel in 0..self.channels {
                mono += to_f32(data[start + channel]);
            }

            mono /= self.channels as f32;
            self.pending.push(float_to_i16(mono));

            if self.pending.len() >= self.chunk_samples {
                self.flush_chunk();
            }

            self.next_source_frame += step;
        }

        self.next_source_frame -= frames as f64;
    }

    fn flush_chunk(&mut self) {
        let samples = self.pending.drain(..self.chunk_samples).collect();

        match self.audio_tx.try_send(samples) {
            Ok(()) => {}
            Err(mpsc::error::TrySendError::Full(_)) => {
                self.dropped_audio_chunks.fetch_add(1, Ordering::Relaxed);
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                self.stop.store(true, Ordering::Relaxed);
            }
        }
    }
}

fn float_to_i16(sample: f32) -> i16 {
    let sample = sample.clamp(-1.0, 1.0);
    (sample * i16::MAX as f32) as i16
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunks_for_duration_rounds_up_and_never_zero() {
        assert_eq!(chunks_for_duration(Duration::from_millis(800), 100), 8);
        assert_eq!(chunks_for_duration(Duration::from_millis(801), 100), 9);
        assert_eq!(chunks_for_duration(Duration::ZERO, 100), 1);
        assert_eq!(chunks_for_duration(Duration::from_millis(1), 0), 1);
    }

    #[test]
    fn build_audio_batch_merges_up_to_limit() {
        let (tx, mut rx) = mpsc::channel(8);
        tx.try_send(vec![2]).unwrap();
        tx.try_send(vec![3]).unwrap();
        tx.try_send(vec![4]).unwrap();

        let batch = build_audio_batch(vec![1], &mut rx, 3, None).unwrap();

        assert_eq!(batch.bytes, vec![1, 0, 2, 0, 3, 0]);
        assert_eq!(batch.chunk_count, 3);
        assert_eq!(batch.flushed_chunks, 0);
        assert_eq!(rx.len(), 1);
    }

    #[test]
    fn build_audio_batch_half_flushes_large_backlog() {
        let (tx, mut rx) = mpsc::channel(16);
        for value in 2..=8 {
            tx.try_send(vec![value]).unwrap();
        }

        let batch = build_audio_batch(vec![1], &mut rx, 2, Some(4)).unwrap();

        assert_eq!(batch.bytes, vec![7, 0, 8, 0]);
        assert_eq!(batch.chunk_count, 2);
        assert_eq!(batch.flushed_chunks, 6);
        assert_eq!(rx.len(), 0);
    }

    #[test]
    fn audio_processor_emits_linear16_chunks() {
        let (tx, mut rx) = mpsc::channel(2);
        let dropped = Arc::new(AtomicU64::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let mut processor = AudioProcessor::new(4, 1, 4, 500, tx, dropped, stop);

        processor.push_samples(&[0.0_f32, 1.0, -1.0, 0.5], |sample| sample);

        let first = rx.try_recv().unwrap();
        let second = rx.try_recv().unwrap();
        assert_eq!(first.len(), 2);
        assert_eq!(second.len(), 2);
    }
}
