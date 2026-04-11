use std::process::{Command, Stdio};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonRuntime {
    pub program: String,
    pub prefix_args: Vec<String>,
}

impl PythonRuntime {
    pub fn apply_to(&self, command: &mut tokio::process::Command) {
        command.args(&self.prefix_args);
    }
}

fn candidate_commands_for_platform() -> Vec<PythonRuntime> {
    if cfg!(target_os = "windows") {
        return vec![
            PythonRuntime {
                program: "py".into(),
                prefix_args: vec!["-3".into()],
            },
            PythonRuntime {
                program: "python".into(),
                prefix_args: Vec::new(),
            },
            PythonRuntime {
                program: "python3".into(),
                prefix_args: Vec::new(),
            },
        ];
    }

    vec![
        PythonRuntime {
            program: "python3".into(),
            prefix_args: Vec::new(),
        },
        PythonRuntime {
            program: "python".into(),
            prefix_args: Vec::new(),
        },
        PythonRuntime {
            program: "py".into(),
            prefix_args: vec!["-3".into()],
        },
    ]
}

fn can_execute_python(runtime: &PythonRuntime) -> bool {
    let mut cmd = Command::new(&runtime.program);
    cmd.args(&runtime.prefix_args)
        .arg("-c")
        .arg("import sys; print(sys.version)")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    cmd.status().is_ok_and(|status| status.success())
}

pub fn resolve_python_runtime(env_var: &str) -> PythonRuntime {
    if let Ok(explicit) = std::env::var(env_var) {
        let explicit_runtime = PythonRuntime {
            program: explicit,
            prefix_args: Vec::new(),
        };
        if can_execute_python(&explicit_runtime) {
            return explicit_runtime;
        }
    }

    for candidate in candidate_commands_for_platform() {
        if can_execute_python(&candidate) {
            return candidate;
        }
    }

    if cfg!(target_os = "windows") {
        return PythonRuntime {
            program: "py".into(),
            prefix_args: vec!["-3".into()],
        };
    }

    PythonRuntime {
        program: "python3".into(),
        prefix_args: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_python_runtime_prefers_explicit_when_valid() {
        let key = "TOKEN_PLACE_TEST_PYTHON_RUNTIME";
        let known_good = candidate_commands_for_platform()
            .into_iter()
            .find(can_execute_python)
            .expect("expected at least one python runtime during tests");

        unsafe { std::env::set_var(key, &known_good.program) };
        let runtime = resolve_python_runtime(key);
        unsafe { std::env::remove_var(key) };

        assert_eq!(runtime.program, known_good.program);
    }

    #[test]
    fn resolve_python_runtime_skips_invalid_explicit_binary() {
        let key = "TOKEN_PLACE_TEST_PYTHON_RUNTIME";
        unsafe { std::env::set_var(key, "definitely-not-a-real-python-binary") };
        let runtime = resolve_python_runtime(key);
        unsafe { std::env::remove_var(key) };

        assert_ne!(runtime.program, "definitely-not-a-real-python-binary");
    }
}
