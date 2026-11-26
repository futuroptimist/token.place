#[path = "../cargo_profile.rs"]
mod cargo_profile;

use cargo_profile::cargo_profile_dir;
use std::path::Path;

#[test]
fn resolves_cargo_profile_for_both_build_layouts() {
    let profile = Path::new("work/build/custom target/x86_64-pc-windows-msvc/release");

    for build_unit in [
        "token-place-desktop-tauri-12345678",
        "token-place-desktop-tauri/12345678",
    ] {
        let out_dir = profile.join("build").join(build_unit).join("out");
        assert_eq!(cargo_profile_dir(&out_dir), Some(profile));
    }
}

#[test]
fn returns_none_without_a_build_directory() {
    assert_eq!(
        cargo_profile_dir(Path::new("target/release/other/out")),
        None
    );
}
