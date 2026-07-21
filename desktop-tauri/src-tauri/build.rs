use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=TOKEN_PLACE_BUILD_ID");
    println!("cargo:rerun-if-env-changed=GITHUB_SHA");
    if std::env::var("TOKEN_PLACE_BUILD_ID").is_err() {
        if let Ok(sha) = std::env::var("GITHUB_SHA") {
            let short = sha.chars().take(12).collect::<String>();
            println!("cargo:rustc-env=TOKEN_PLACE_BUILD_ID={short}");
        } else if let Ok(output) = Command::new("git")
            .args(["rev-parse", "--short=12", "HEAD"])
            .output()
        {
            if output.status.success() {
                let short = String::from_utf8_lossy(&output.stdout).trim().to_string();
                if !short.is_empty() {
                    println!("cargo:rustc-env=TOKEN_PLACE_BUILD_ID={short}");
                }
            }
        }
    }
    tauri_build::build()
}
