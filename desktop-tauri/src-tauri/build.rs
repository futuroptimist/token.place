use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=GITHUB_SHA");
    println!("cargo:rerun-if-env-changed=TARGET");
    let git_sha = std::env::var("GITHUB_SHA").ok().or_else(|| {
        Command::new("git")
            .args(["rev-parse", "--short=12", "HEAD"])
            .output()
            .ok()
            .and_then(|output| output.status.success().then_some(output.stdout))
            .and_then(|stdout| String::from_utf8(stdout).ok())
            .map(|sha| sha.trim().to_string())
            .filter(|sha| !sha.is_empty())
    });
    if let Some(git_sha) = git_sha {
        println!("cargo:rustc-env=TOKENPLACE_BUILD_COMMIT={git_sha}");
    }
    println!(
        "cargo:rustc-env=TOKENPLACE_TARGET_TRIPLE={}",
        std::env::var("TARGET").unwrap_or_else(|_| "unknown".into())
    );
    tauri_build::build()
}
