use std::process::Command;

#[derive(Debug, Clone)]
pub struct PythonLauncher {
    pub program: String,
    pub pre_args: Vec<String>,
}

impl PythonLauncher {
    pub fn into_command_for_script(self, script_path: &str) -> Command {
        let mut cmd = Command::new(self.program);
        for arg in self.pre_args {
            cmd.arg(arg);
        }
        cmd.arg(script_path);
        cmd
    }
}

fn candidate_launchers() -> Vec<PythonLauncher> {
    if cfg!(target_os = "windows") {
        vec![
            PythonLauncher {
                program: "py".into(),
                pre_args: vec!["-3".into()],
            },
            PythonLauncher {
                program: "python".into(),
                pre_args: Vec::new(),
            },
            PythonLauncher {
                program: "python3".into(),
                pre_args: Vec::new(),
            },
        ]
    } else {
        vec![
            PythonLauncher {
                program: "python3".into(),
                pre_args: Vec::new(),
            },
            PythonLauncher {
                program: "python".into(),
                pre_args: Vec::new(),
            },
        ]
    }
}

fn launcher_is_usable(launcher: &PythonLauncher) -> bool {
    let mut cmd = Command::new(&launcher.program);
    for arg in &launcher.pre_args {
        cmd.arg(arg);
    }
    cmd.arg("--version");
    cmd.output().is_ok_and(|output| output.status.success())
}

pub fn resolve_python_launcher(env_var: &str) -> Result<PythonLauncher, String> {
    if let Ok(configured_python) = std::env::var(env_var) {
        let launcher = PythonLauncher {
            program: configured_python,
            pre_args: Vec::new(),
        };
        if launcher_is_usable(&launcher) {
            return Ok(launcher);
        }

        return Err(format!(
            "{env_var} is set but '{}' is not executable or failed '--version'",
            launcher.program
        ));
    }

    for launcher in candidate_launchers() {
        if launcher_is_usable(&launcher) {
            return Ok(launcher);
        }
    }

    Err(format!(
        "unable to locate a usable Python 3 interpreter; tried {}",
        candidate_launchers()
            .into_iter()
            .map(|launcher| {
                if launcher.pre_args.is_empty() {
                    launcher.program
                } else {
                    format!("{} {}", launcher.program, launcher.pre_args.join(" "))
                }
            })
            .collect::<Vec<_>>()
            .join(", ")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_env_var_reports_clear_error_when_unusable() {
        let key = "TOKEN_PLACE_TEST_SIDE_PYTHON";
        std::env::set_var(key, "__missing_python__");
        let error = resolve_python_launcher(key).expect_err("expected failed launcher resolution");
        assert!(error.contains("__missing_python__"));
        std::env::remove_var(key);
    }
}
