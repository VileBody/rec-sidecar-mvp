#[cfg(target_os = "macos")]
use objc::{
    msg_send,
    runtime::{Class, Object},
    sel, sel_impl,
};
#[cfg(target_os = "macos")]
use std::ffi::CStr;

#[cfg(target_os = "macos")]
#[allow(unexpected_cfgs)]
pub(crate) fn pin_window_to_all_spaces(window_title: &str) -> bool {
    unsafe {
        let Some(ns_application) = Class::get("NSApplication") else {
            return false;
        };

        let app: *mut Object = msg_send![ns_application, sharedApplication];
        if app.is_null() {
            return false;
        }

        let windows: *mut Object = msg_send![app, windows];
        if windows.is_null() {
            return false;
        }

        let count: usize = msg_send![windows, count];
        for index in 0..count {
            let window: *mut Object = msg_send![windows, objectAtIndex: index];
            if window.is_null() {
                continue;
            }

            let title: *mut Object = msg_send![window, title];
            if title.is_null() {
                continue;
            }

            let title_ptr: *const std::os::raw::c_char = msg_send![title, UTF8String];
            if title_ptr.is_null() {
                continue;
            }

            let title = CStr::from_ptr(title_ptr).to_string_lossy();
            if title != window_title {
                continue;
            }

            const CAN_JOIN_ALL_SPACES: usize = 1 << 0;
            const MOVE_TO_ACTIVE_SPACE: usize = 1 << 1;
            const STATIONARY: usize = 1 << 4;
            const IGNORES_CYCLE: usize = 1 << 6;
            const FULL_SCREEN_AUXILIARY: usize = 1 << 8;
            const NS_FLOATING_WINDOW_LEVEL: isize = 3;

            let current_behavior: usize = msg_send![window, collectionBehavior];
            let behavior = (current_behavior & !MOVE_TO_ACTIVE_SPACE)
                | CAN_JOIN_ALL_SPACES
                | STATIONARY
                | IGNORES_CYCLE
                | FULL_SCREEN_AUXILIARY;

            let _: () = msg_send![window, setCollectionBehavior: behavior];
            let _: () = msg_send![window, setLevel: NS_FLOATING_WINDOW_LEVEL];
            return true;
        }

        false
    }
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn pin_window_to_all_spaces(_window_title: &str) -> bool {
    true
}
