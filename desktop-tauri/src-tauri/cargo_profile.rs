use std::{ffi::OsStr, path::Path};

pub(crate) fn cargo_profile_dir(out_dir: &Path) -> Option<&Path> {
    // Cargo 1.100 split the package name and hash into separate directories.
    // Both layouts keep the build-script output below the profile's build directory.
    out_dir
        .ancestors()
        .find(|path| path.file_name() == Some(OsStr::new("build")))
        .and_then(Path::parent)
}
