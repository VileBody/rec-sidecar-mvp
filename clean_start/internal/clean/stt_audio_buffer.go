package clean

import (
	"sync/atomic"
	"time"
)

const (
	sttAudioFlushInterval = 50 * time.Millisecond
	sttAudioTargetFlush   = 100 * time.Millisecond
	sttAudioMaxFlush      = 200 * time.Millisecond

	// Browser audio arrives in ~40-50ms chunks. If STT/network stalls, keep the
	// freshest audio instead of letting seconds of stale speech trail behind.
	sttAudioQueueCriticalCommands = 48
	sttAudioQueueTargetCommands   = 24
)

type sttAudioCommand struct {
	audio   []byte
	endTurn bool
	close   bool
	err     error
}

type sttAudioStats struct {
	audioChunks               atomic.Int64
	audioBytes                atomic.Int64
	audioFlushes              atomic.Int64
	audioFlushedBytes         atomic.Int64
	audioQueueFlushes         atomic.Int64
	audioDroppedQueueCommands atomic.Int64
	audioDroppedQueueBytes    atomic.Int64
	endTurns                  atomic.Int64
	coalescedEndTurns         atomic.Int64
	maxBufferBytes            atomic.Int64
}

func enqueueSTTAudioCommand(commands chan sttAudioCommand, command sttAudioCommand, stats *sttAudioStats) {
	halfFlushSTTAudioCommandQueue(commands, stats)
	select {
	case commands <- command:
		return
	default:
		halfFlushSTTAudioCommandQueue(commands, stats)
	}
	select {
	case commands <- command:
		return
	default:
		if len(command.audio) > 0 && stats != nil {
			stats.audioDroppedQueueCommands.Add(1)
			stats.audioDroppedQueueBytes.Add(int64(len(command.audio)))
		}
	}
}

func halfFlushSTTAudioCommandQueue(commands chan sttAudioCommand, stats *sttAudioStats) {
	queued := len(commands)
	if queued < sttAudioQueueCriticalCommands {
		return
	}
	target := sttAudioQueueTargetCommands
	if target < 1 {
		target = 1
	}
	dropBudget := queued - target
	if dropBudget <= 0 {
		return
	}

	kept := make([]sttAudioCommand, 0, queued)
	droppedCommands := 0
	droppedBytes := 0
	for i := 0; i < queued; i++ {
		select {
		case queuedCommand := <-commands:
			if dropBudget > 0 && len(queuedCommand.audio) > 0 {
				droppedCommands++
				droppedBytes += len(queuedCommand.audio)
				dropBudget--
				continue
			}
			kept = append(kept, queuedCommand)
		default:
			i = queued
		}
	}
	for _, queuedCommand := range kept {
		commands <- queuedCommand
	}
	if droppedCommands > 0 && stats != nil {
		stats.audioQueueFlushes.Add(1)
		stats.audioDroppedQueueCommands.Add(int64(droppedCommands))
		stats.audioDroppedQueueBytes.Add(int64(droppedBytes))
	}
}

func runSTTAudioJitterBuffer(stream STTStream, commands <-chan sttAudioCommand, stats *sttAudioStats) error {
	buffer := &sttAudioJitterBuffer{
		stream:      stream,
		stats:       stats,
		targetBytes: durationToPCMBytes(sttAudioTargetFlush),
		maxBytes:    durationToPCMBytes(sttAudioMaxFlush),
	}
	ticker := time.NewTicker(sttAudioFlushInterval)
	defer ticker.Stop()

	for {
		select {
		case command, ok := <-commands:
			if !ok {
				return buffer.FlushAll()
			}
			if command.err != nil {
				if err := buffer.FlushAll(); err != nil {
					return err
				}
				return command.err
			}
			if command.close {
				return buffer.FlushAll()
			}
			if len(command.audio) > 0 {
				if err := buffer.Append(command.audio); err != nil {
					return err
				}
				continue
			}
			if command.endTurn {
				if err := buffer.EndTurn(); err != nil {
					return err
				}
			}
		case now := <-ticker.C:
			if err := buffer.FlushStale(now); err != nil {
				return err
			}
		}
	}
}

type sttAudioJitterBuffer struct {
	stream            STTStream
	stats             *sttAudioStats
	buffer            []byte
	firstBufferedAt   time.Time
	lastEndTurnAt     time.Time
	audioSinceEndTurn bool
	targetBytes       int
	maxBytes          int
}

func (b *sttAudioJitterBuffer) Append(pcm []byte) error {
	if len(pcm) == 0 {
		return nil
	}
	if len(b.buffer) == 0 {
		b.firstBufferedAt = time.Now()
	}
	b.buffer = append(b.buffer, pcm...)
	b.audioSinceEndTurn = true
	b.rememberMaxBuffer()
	return b.FlushReady()
}

func (b *sttAudioJitterBuffer) EndTurn() error {
	if err := b.FlushAll(); err != nil {
		return err
	}
	now := time.Now()
	if !b.audioSinceEndTurn {
		if b.stats != nil {
			b.stats.coalescedEndTurns.Add(1)
		}
		return nil
	}
	if err := b.stream.SendEndTurn(); err != nil {
		return err
	}
	if b.stats != nil {
		b.stats.endTurns.Add(1)
	}
	b.lastEndTurnAt = now
	b.audioSinceEndTurn = false
	return nil
}

func (b *sttAudioJitterBuffer) FlushReady() error {
	for len(b.buffer) >= b.targetBytes {
		if err := b.flushChunk(minInt(len(b.buffer), b.maxBytes)); err != nil {
			return err
		}
	}
	return nil
}

func (b *sttAudioJitterBuffer) FlushStale(now time.Time) error {
	if len(b.buffer) == 0 || b.firstBufferedAt.IsZero() || now.Sub(b.firstBufferedAt) < sttAudioTargetFlush {
		return nil
	}
	return b.flushChunk(minInt(len(b.buffer), b.maxBytes))
}

func (b *sttAudioJitterBuffer) FlushAll() error {
	for len(b.buffer) > 0 {
		if err := b.flushChunk(minInt(len(b.buffer), b.maxBytes)); err != nil {
			return err
		}
	}
	return nil
}

func (b *sttAudioJitterBuffer) flushChunk(size int) error {
	size = clampPCMSize(size, len(b.buffer))
	if size <= 0 {
		return nil
	}
	chunk := make([]byte, size)
	copy(chunk, b.buffer[:size])
	if err := b.stream.SendAudio(chunk); err != nil {
		return err
	}
	copy(b.buffer, b.buffer[size:])
	b.buffer = b.buffer[:len(b.buffer)-size]
	if len(b.buffer) == 0 {
		b.firstBufferedAt = time.Time{}
	} else {
		b.firstBufferedAt = time.Now()
	}
	if b.stats != nil {
		b.stats.audioFlushes.Add(1)
		b.stats.audioFlushedBytes.Add(int64(size))
	}
	return nil
}

func (b *sttAudioJitterBuffer) rememberMaxBuffer() {
	if b.stats == nil {
		return
	}
	size := int64(len(b.buffer))
	for {
		previous := b.stats.maxBufferBytes.Load()
		if size <= previous || b.stats.maxBufferBytes.CompareAndSwap(previous, size) {
			break
		}
	}
}

func durationToPCMBytes(duration time.Duration) int {
	bytesPerSecond := audioSampleRate * audioChannels * audioBitDepth / 8
	bytes := int(duration.Seconds() * float64(bytesPerSecond))
	return clampPCMSize(bytes, bytes)
}

func clampPCMSize(size, available int) int {
	if size > available {
		size = available
	}
	if size%2 != 0 {
		size--
	}
	return size
}

func minInt(left, right int) int {
	if left < right {
		return left
	}
	return right
}
