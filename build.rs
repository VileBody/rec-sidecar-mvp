use std::{env, path::PathBuf};

fn main() {
    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os != "macos" {
        return;
    }

    println!("cargo:rerun-if-changed=native/system_audio_tap.m");
    println!("cargo:rerun-if-changed=native/RecSidecarInfo.plist");

    cc::Build::new()
        .file("native/system_audio_tap.m")
        .flag("-fobjc-arc")
        .compile("rec_sidecar_system_audio_tap");

    println!("cargo:rustc-link-lib=framework=Foundation");
    println!("cargo:rustc-link-lib=framework=CoreAudio");

    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let plist_path = manifest_dir.join("native/RecSidecarInfo.plist");
    println!(
        "cargo:rustc-link-arg=-Wl,-sectcreate,__TEXT,__info_plist,{}",
        plist_path.display()
    );
}
