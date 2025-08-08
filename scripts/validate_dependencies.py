#!/usr/bin/env python3
"""
Dependency validation script to prevent version conflicts.

This script checks that all dependencies in requirements.txt can be installed
together without conflicts, and validates specific compatibility requirements.
"""

import subprocess
import sys
import tempfile
import os
import shlex
from pathlib import Path

def run_command(cmd, cwd=None):
    """Run a command and return success status and output."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)

def validate_dependencies():
    """Validate that all dependencies can be installed together."""
    print("🔍 Validating dependency compatibility...")
    
    # Get the project root directory
    project_root = Path(__file__).parent.parent
    requirements_file = project_root / "requirements.txt"
    
    if not requirements_file.exists():
        print("❌ requirements.txt not found!")
        return False
    
    # Create a temporary virtual environment
    with tempfile.TemporaryDirectory() as temp_dir:
        venv_path = Path(temp_dir) / "test_env"
        
        print("📦 Creating temporary virtual environment...")
        success, _, stderr = run_command(f"python -m venv {venv_path}")
        if not success:
            print(f"❌ Failed to create virtual environment: {stderr}")
            return False
        
        # Determine python and pip paths based on OS
        if os.name == 'nt':  # Windows
            python_path = venv_path / "Scripts" / "python.exe"
            pip_cmd = f'"{python_path}" -m pip'
        else:  # Unix/Linux/macOS
            python_path = venv_path / "bin" / "python"
            pip_cmd = f'"{python_path}" -m pip'
        
        print("⬆️ Upgrading pip...")
        success, stdout, stderr = run_command(f'{pip_cmd} install --upgrade pip')
        # Note: pip upgrade often shows warnings but still succeeds
        if not success and "Successfully installed pip" not in stdout:
            print(f"❌ Failed to upgrade pip: {stderr}")
            print(f"STDOUT: {stdout}")
            return False
        
        print("📋 Installing dependencies from requirements.txt...")
        success, stdout, stderr = run_command(f'{pip_cmd} install -r "{requirements_file}"')
        
        if not success:
            print("❌ Dependency installation failed!")
            print("STDOUT:", stdout[-1000:] if len(stdout) > 1000 else stdout)  # Last 1000 chars
            print("STDERR:", stderr[-1000:] if len(stderr) > 1000 else stderr)  # Last 1000 chars
            if "ResolutionImpossible" in stderr or "conflicting dependencies" in stderr:
                print("\n🔧 Dependency conflict detected!")
                print("This usually means version constraints are incompatible.")
                print("Check the comments in requirements.txt for compatibility notes.")
            return False
        
        print("✅ All dependencies installed successfully!")
        
        # Validate specific critical dependencies
        print("🧪 Validating critical dependency versions...")
        
        # Check pytest version
        success, stdout, stderr = run_command(f"{pip_cmd} show pytest")
        if success and "Version:" in stdout:
            version_line = [line for line in stdout.split('\n') if line.startswith('Version:')][0]
            pytest_version = version_line.split(':')[1].strip()
            print(f"  📌 pytest: {pytest_version}")
            
            # Check if pytest version is >= 8.1 (required by pytest-benchmark 5.1.0+)
            major, minor = map(int, pytest_version.split('.')[:2])
            if major < 8 or (major == 8 and minor < 1):
                print(f"❌ pytest version {pytest_version} is too old! pytest-benchmark 5.1.0+ requires pytest>=8.1")
                return False
        
        # Check pytest-playwright version
        success, stdout, stderr = run_command(f"{pip_cmd} show pytest-playwright")
        if success and "Version:" in stdout:
            version_line = [line for line in stdout.split('\n') if line.startswith('Version:')][0]
            version = version_line.split(':')[1].strip()
            print(f"  📌 pytest-playwright: {version}")
        
        # Check playwright version
        success, stdout, stderr = run_command(f"{pip_cmd} show playwright")
        if success and "Version:" in stdout:
            version_line = [line for line in stdout.split('\n') if line.startswith('Version:')][0]
            version = version_line.split(':')[1].strip()
            print(f"  📌 playwright: {version}")
        
        print("✅ All dependency versions look good!")
        return True

def main():
    """Main function."""
    print("=" * 60)
    print(" Dependency Compatibility Validator")
    print("=" * 60)
    
    success = validate_dependencies()
    
    if success:
        print("\n🎉 All dependencies are compatible!")
        print("💡 Tip: Run this script before updating dependencies to catch conflicts early.")
        sys.exit(0)
    else:
        print("\n💥 Dependency validation failed!")
        print("🔧 Please fix the dependency conflicts before proceeding.")
        sys.exit(1)

if __name__ == "__main__":
    main() 