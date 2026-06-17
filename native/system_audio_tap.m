#import <Foundation/Foundation.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreAudio/AudioHardware.h>
#import <CoreAudio/AudioHardwareTapping.h>
#import <CoreAudio/CATapDescription.h>
#import <stdbool.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>
#import <unistd.h>

typedef void (*RecSystemAudioSamplesCallback)(
    void *context,
    const float *samples,
    uint32_t frame_count,
    uint32_t channels,
    double sample_rate
);

typedef struct RecSystemAudioTapHandle {
    AudioObjectID tap_id;
    AudioObjectID aggregate_device_id;
    AudioDeviceIOProcID io_proc_id;
    Float64 sample_rate;
    uint32_t channels;
    uint32_t bytes_per_frame;
    RecSystemAudioSamplesCallback callback;
    void *context;
    bool started;
} RecSystemAudioTapHandle;

static void write_error(char *buffer, size_t buffer_len, NSString *message) {
    if (buffer == NULL || buffer_len == 0) {
        return;
    }

    if (message == nil) {
        buffer[0] = '\0';
        return;
    }

    snprintf(buffer, buffer_len, "%s", message.UTF8String);
}

static NSString *status_message(NSString *operation, OSStatus status) {
    return [NSString stringWithFormat:@"%@ failed with OSStatus %d", operation, (int)status];
}

static OSStatus get_process_object_for_pid(pid_t pid, AudioObjectID *out_id) {
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioHardwarePropertyTranslatePIDToProcessObject,
        .mScope = kAudioObjectPropertyScopeGlobal,
        .mElement = kAudioObjectPropertyElementMain,
    };
    UInt32 size = sizeof(AudioObjectID);
    return AudioObjectGetPropertyData(
        kAudioObjectSystemObject,
        &addr,
        sizeof(pid),
        &pid,
        &size,
        out_id
    );
}

static OSStatus get_nominal_sample_rate(AudioObjectID device_id, Float64 *out_rate) {
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioDevicePropertyNominalSampleRate,
        .mScope = kAudioObjectPropertyScopeGlobal,
        .mElement = kAudioObjectPropertyElementMain,
    };
    UInt32 size = sizeof(Float64);
    return AudioObjectGetPropertyData(device_id, &addr, 0, NULL, &size, out_rate);
}

static OSStatus get_stream_format(AudioObjectID device_id, AudioStreamBasicDescription *out_format) {
    AudioObjectPropertyAddress candidates[] = {
        {
            .mSelector = kAudioDevicePropertyStreamFormat,
            .mScope = kAudioDevicePropertyScopeInput,
            .mElement = kAudioObjectPropertyElementMain,
        },
        {
            .mSelector = kAudioDevicePropertyStreamFormat,
            .mScope = kAudioObjectPropertyScopeGlobal,
            .mElement = kAudioObjectPropertyElementMain,
        },
    };

    for (size_t index = 0; index < sizeof(candidates) / sizeof(candidates[0]); index += 1) {
        UInt32 size = sizeof(AudioStreamBasicDescription);
        OSStatus status = AudioObjectGetPropertyData(
            device_id,
            &candidates[index],
            0,
            NULL,
            &size,
            out_format
        );
        if (status == noErr) {
            return noErr;
        }
    }

    return kAudioHardwareUnknownPropertyError;
}

static void destroy_handle(RecSystemAudioTapHandle *handle) {
    if (handle == NULL) {
        return;
    }

    if (handle->started && handle->io_proc_id != NULL) {
        AudioDeviceStop(handle->aggregate_device_id, handle->io_proc_id);
    }

    if (handle->io_proc_id != NULL) {
        AudioDeviceDestroyIOProcID(handle->aggregate_device_id, handle->io_proc_id);
    }

    if (handle->aggregate_device_id != kAudioObjectUnknown) {
        AudioHardwareDestroyAggregateDevice(handle->aggregate_device_id);
    }

    if (handle->tap_id != kAudioObjectUnknown) {
        AudioHardwareDestroyProcessTap(handle->tap_id);
    }

    free(handle);
}

static OSStatus tap_io_proc(
    AudioObjectID in_device,
    const AudioTimeStamp *in_now,
    const AudioBufferList *in_input_data,
    const AudioTimeStamp *in_input_time,
    AudioBufferList *out_output_data,
    const AudioTimeStamp *in_output_time,
    void *in_client_data
) {
    (void)in_device;
    (void)in_now;
    (void)in_input_time;
    (void)out_output_data;
    (void)in_output_time;

    RecSystemAudioTapHandle *handle = (RecSystemAudioTapHandle *)in_client_data;
    if (handle == NULL || handle->callback == NULL || in_input_data == NULL) {
        return noErr;
    }

    if (in_input_data->mNumberBuffers == 0) {
        return noErr;
    }

    const AudioBuffer *buffer = &in_input_data->mBuffers[0];
    if (buffer->mData == NULL || handle->bytes_per_frame == 0) {
        return noErr;
    }

    UInt32 frame_count = buffer->mDataByteSize / handle->bytes_per_frame;
    if (frame_count == 0) {
        return noErr;
    }

    handle->callback(
        handle->context,
        (const float *)buffer->mData,
        frame_count,
        handle->channels,
        handle->sample_rate
    );

    return noErr;
}

bool rec_system_audio_is_supported(void) {
    if (@available(macOS 14.2, *)) {
        return true;
    }
    return false;
}

RecSystemAudioTapHandle *rec_system_audio_create(
    RecSystemAudioSamplesCallback callback,
    void *context,
    double *out_sample_rate,
    uint32_t *out_channels,
    char *error_buffer,
    size_t error_buffer_len
) {
    @autoreleasepool {
        if (!rec_system_audio_is_supported()) {
            write_error(
                error_buffer,
                error_buffer_len,
                @"Core Audio process taps require macOS 14.2 or newer"
            );
            return NULL;
        }

        if (callback == NULL) {
            write_error(error_buffer, error_buffer_len, @"system audio callback is null");
            return NULL;
        }

        AudioObjectID process_id = kAudioObjectUnknown;
        OSStatus status = get_process_object_for_pid(getpid(), &process_id);
        if (status != noErr) {
            write_error(error_buffer, error_buffer_len, status_message(@"translate PID to process object", status));
            return NULL;
        }

        CATapDescription *description =
            [[CATapDescription alloc] initMonoGlobalTapButExcludeProcesses:@[@(process_id)]];
        description.name = @"REC Sidecar System Audio Tap";
        description.privateTap = YES;
        description.muteBehavior = CATapUnmuted;

        AudioObjectID tap_id = kAudioObjectUnknown;
        status = AudioHardwareCreateProcessTap(description, &tap_id);
        if (status != noErr) {
            write_error(error_buffer, error_buffer_len, status_message(@"create process tap", status));
            return NULL;
        }

        NSDictionary *tap_dict = @{
            @kAudioSubTapUIDKey: description.UUID.UUIDString,
            @kAudioSubTapDriftCompensationKey: @NO,
        };
        NSDictionary *aggregate_dict = @{
            @kAudioAggregateDeviceNameKey: @"REC Sidecar System Audio Aggregate",
            @kAudioAggregateDeviceUIDKey: [NSString stringWithFormat:@"dev.codex.rec-sidecar.system-audio.aggregate.%@", NSUUID.UUID.UUIDString],
            @kAudioAggregateDeviceIsPrivateKey: @YES,
            @kAudioAggregateDeviceTapAutoStartKey: @YES,
            @kAudioAggregateDeviceTapListKey: @[tap_dict],
        };

        AudioObjectID aggregate_device_id = kAudioObjectUnknown;
        status = AudioHardwareCreateAggregateDevice(
            (__bridge CFDictionaryRef)aggregate_dict,
            &aggregate_device_id
        );
        if (status != noErr) {
            AudioHardwareDestroyProcessTap(tap_id);
            write_error(error_buffer, error_buffer_len, status_message(@"create aggregate device", status));
            return NULL;
        }

        Float64 sample_rate = 0.0;
        status = get_nominal_sample_rate(aggregate_device_id, &sample_rate);
        if (status != noErr) {
            AudioHardwareDestroyAggregateDevice(aggregate_device_id);
            AudioHardwareDestroyProcessTap(tap_id);
            write_error(error_buffer, error_buffer_len, status_message(@"read aggregate nominal sample rate", status));
            return NULL;
        }

        AudioStreamBasicDescription format = {0};
        status = get_stream_format(aggregate_device_id, &format);
        if (status != noErr) {
            AudioHardwareDestroyAggregateDevice(aggregate_device_id);
            AudioHardwareDestroyProcessTap(tap_id);
            write_error(error_buffer, error_buffer_len, status_message(@"read aggregate stream format", status));
            return NULL;
        }

        if (format.mFormatID != kAudioFormatLinearPCM ||
            !(format.mFormatFlags & kAudioFormatFlagIsFloat) ||
            format.mBitsPerChannel != 32 ||
            format.mChannelsPerFrame == 0) {
            AudioHardwareDestroyAggregateDevice(aggregate_device_id);
            AudioHardwareDestroyProcessTap(tap_id);
            write_error(
                error_buffer,
                error_buffer_len,
                [NSString stringWithFormat:
                    @"unsupported system audio tap format: formatID=%u flags=%u bits=%u channels=%u bytesPerFrame=%u",
                    (unsigned int)format.mFormatID,
                    (unsigned int)format.mFormatFlags,
                    (unsigned int)format.mBitsPerChannel,
                    (unsigned int)format.mChannelsPerFrame,
                    (unsigned int)format.mBytesPerFrame]
            );
            return NULL;
        }

        RecSystemAudioTapHandle *handle =
            (RecSystemAudioTapHandle *)calloc(1, sizeof(RecSystemAudioTapHandle));
        if (handle == NULL) {
            AudioHardwareDestroyAggregateDevice(aggregate_device_id);
            AudioHardwareDestroyProcessTap(tap_id);
            write_error(error_buffer, error_buffer_len, @"failed to allocate system audio handle");
            return NULL;
        }

        handle->tap_id = tap_id;
        handle->aggregate_device_id = aggregate_device_id;
        handle->sample_rate = sample_rate;
        handle->channels = format.mChannelsPerFrame;
        handle->bytes_per_frame = format.mBytesPerFrame;
        handle->callback = callback;
        handle->context = context;
        handle->started = false;

        status = AudioDeviceCreateIOProcID(
            aggregate_device_id,
            tap_io_proc,
            handle,
            &handle->io_proc_id
        );
        if (status != noErr) {
            write_error(error_buffer, error_buffer_len, status_message(@"create aggregate IOProc", status));
            destroy_handle(handle);
            return NULL;
        }

        if (out_sample_rate != NULL) {
            *out_sample_rate = sample_rate;
        }
        if (out_channels != NULL) {
            *out_channels = handle->channels;
        }

        return handle;
    }
}

bool rec_system_audio_start(
    RecSystemAudioTapHandle *handle,
    char *error_buffer,
    size_t error_buffer_len
) {
    @autoreleasepool {
        if (handle == NULL) {
            write_error(error_buffer, error_buffer_len, @"system audio handle is null");
            return false;
        }

        if (handle->started) {
            return true;
        }

        OSStatus status = AudioDeviceStart(handle->aggregate_device_id, handle->io_proc_id);
        if (status != noErr) {
            write_error(error_buffer, error_buffer_len, status_message(@"start aggregate device", status));
            return false;
        }

        handle->started = true;
        return true;
    }
}

void rec_system_audio_destroy(RecSystemAudioTapHandle *handle) {
    @autoreleasepool {
        destroy_handle(handle);
    }
}
