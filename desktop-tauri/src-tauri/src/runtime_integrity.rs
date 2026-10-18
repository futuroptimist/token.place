//! Windows packaged runtimes belong to a durable installation, never staging.
use std::path::{Path, PathBuf};

pub const REQUIRED_STDLIB_FILES: &[&str] = &[
    "Lib/encodings/__init__.py",
    "Lib/json/__init__.py",
    "Lib/os.py",
    "Lib/platform.py",
    "Lib/struct.py",
];

#[derive(Debug, PartialEq, Eq)]
pub enum IntegrityFailure {
    TemporaryInstallation,
    MissingStandardLibrary,
}

fn normalized(path: &Path) -> String {
    path.to_string_lossy()
        .replace('\\', "/")
        .trim_start_matches("//?/")
        .trim_end_matches('/')
        .to_ascii_lowercase()
}

fn is_within(path: &Path, root: &Path) -> bool {
    let path = normalized(path);
    let root = normalized(root);
    !root.is_empty() && (path == root || path.starts_with(&format!("{root}/")))
}

pub fn temporary_roots() -> Vec<PathBuf> {
    let mut roots = vec![std::env::temp_dir()];
    for key in ["TEMP", "TMP"] {
        if let Some(value) = std::env::var_os(key).filter(|v| !v.is_empty()) {
            roots.push(PathBuf::from(value));
        }
    }
    // Keep the default cleanup locations covered even if TEMP was redirected.
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        roots.push(PathBuf::from(local).join("Temp"));
    }
    if let Some(windows) = std::env::var_os("SystemRoot") {
        roots.push(PathBuf::from(windows).join("Temp"));
    }
    roots
}

pub fn validate(root: &Path, temp_roots: &[PathBuf]) -> Result<(), IntegrityFailure> {
    let resolved = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    if temp_roots.iter().any(|temp| {
        is_within(root, temp)
            || is_within(
                &resolved,
                &temp.canonicalize().unwrap_or_else(|_| temp.clone()),
            )
    }) {
        return Err(IntegrityFailure::TemporaryInstallation);
    }
    if REQUIRED_STDLIB_FILES.iter().any(|relative| {
        let path = root.join(relative);
        !path.is_file()
            || path
                .canonicalize()
                .map(|file| !is_within(&file, &resolved))
                .unwrap_or(true)
    }) {
        return Err(IntegrityFailure::MissingStandardLibrary);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_temporary_candidates_are_not_durable_installations() {
        let temp = PathBuf::from("C:/Users/operator/AppData/Local/Temp");
        for candidate in [
            "C:/Users/operator/AppData/Local/Temp/tokenplace-step05-attempt63-id/candidate-install/python-runtime",
            "c:\\users\\operator\\appdata\\local\\temp\\candidate-install\\python-runtime",
        ] {
            assert_eq!(validate(Path::new(candidate), &[temp.clone()]), Err(IntegrityFailure::TemporaryInstallation));
        }
        for installed in [
            "C:/Users/operator/AppData/Local/token.place desktop/python-runtime",
            "C:/Program Files/token.place desktop/python-runtime",
            "C:/Users/operator/AppData/Local/TemporaryApps/python-runtime",
        ] {
            assert!(!is_within(Path::new(installed), &temp));
        }
    }

    #[test]
    fn each_missing_stdlib_file_fails_integrity() {
        let root =
            std::env::temp_dir().join(format!("tokenplace-integrity-test-{}", std::process::id()));
        assert!(!root.exists(), "test fixture must be new");
        for relative in REQUIRED_STDLIB_FILES {
            let path = root.join(relative);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, "# fixture").unwrap();
        }
        assert_eq!(validate(&root, &[]), Ok(()));
        for relative in REQUIRED_STDLIB_FILES {
            let path = root.join(relative);
            std::fs::remove_file(&path).unwrap();
            assert_eq!(
                validate(&root, &[]),
                Err(IntegrityFailure::MissingStandardLibrary)
            );
            std::fs::write(path, "# fixture").unwrap();
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
