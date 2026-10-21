use std::process::Command;

fn build_windows_ownership_helper() {
    if !std::env::var("TARGET")
        .unwrap_or_default()
        .ends_with("windows-msvc")
    {
        return;
    }
    println!("cargo:rerun-if-changed=installers/ownership.cpp");
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    let profile = out.ancestors().nth(3).expect("Cargo profile directory");
    let compiler = cc::Build::new().cpp(true).static_crt(true).get_compiler();
    for (suffix, dll) in [("exe", false), ("dll", true)] {
        let output = profile.join(format!("token-place-desktop-tauri.exe.ownership.{suffix}"));
        let mut command = compiler.to_command();
        command.args(["/nologo", "/std:c++17", "/EHsc", "/W4", "/WX", "/MT", "/O2"]);
        command.arg("installers/ownership.cpp");
        command.arg(format!(
            "/Fo{}",
            out.join(format!("ownership-{suffix}.obj")).display()
        ));
        command.arg(format!("/Fe{}", output.display()));
        if dll {
            command.args(["/LD", "/DTOKEN_PLACE_MSI"]);
        }
        command.args(["/link", "msi.lib", "advapi32.lib"]);
        if !dll {
            command.args(["/SUBSYSTEM:WINDOWS", "/ENTRY:wmainCRTStartup"]);
        }
        if dll && std::env::var("TARGET").unwrap().starts_with("i686") {
            command.args([
                "/EXPORT:TokenPlaceResolve=_TokenPlaceResolve@4",
                "/EXPORT:TokenPlaceVerify=_TokenPlaceVerify@4",
            ]);
        }
        assert!(
            command
                .status()
                .expect("Existing MSVC toolchain required for installer ownership helper")
                .success(),
            "Installer ownership helper compilation failed"
        );
    }
}

fn main() {
    println!("cargo:rerun-if-env-changed=TOKENPLACE_BUILD_COMMIT");
    println!("cargo:rerun-if-env-changed=GITHUB_SHA");
    println!("cargo:rerun-if-env-changed=TARGET");
    let git_sha = std::env::var("TOKENPLACE_BUILD_COMMIT")
        .ok()
        .filter(|sha| !sha.trim().is_empty())
        .or_else(|| {
            Command::new("git")
                .args(["rev-parse", "HEAD"])
                .output()
                .ok()
                .and_then(|output| output.status.success().then_some(output.stdout))
                .and_then(|stdout| String::from_utf8(stdout).ok())
                .map(|sha| sha.trim().to_string())
                .filter(|sha| !sha.is_empty())
        })
        .or_else(|| {
            std::env::var("GITHUB_SHA")
                .ok()
                .filter(|sha| !sha.trim().is_empty())
        });
    if let Some(git_sha) = git_sha {
        println!("cargo:rustc-env=TOKENPLACE_BUILD_COMMIT={git_sha}");
    }
    println!(
        "cargo:rustc-env=TOKENPLACE_TARGET_TRIPLE={}",
        std::env::var("TARGET").unwrap_or_else(|_| "unknown".into())
    );
    build_windows_ownership_helper();
    tauri_build::build()
}
