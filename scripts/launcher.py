#!/usr/bin/env python3
"""
Cross-platform launcher script for token.place components.
This script can start the server, relay, and API components.
"""

import os
import sys
import argparse
import subprocess
import platform
import signal
import time
import logging
import importlib
from pathlib import Path
from typing import List, Dict, Optional

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import our configuration and path utilities
from config import config
from utils.path_handling import (
    IS_WINDOWS, IS_MACOS, IS_LINUX,
    get_executable_extension, ensure_dir_exists
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(config.get('paths.logs_dir'), 'launcher.log'))
    ]
)
logger = logging.getLogger('launcher')

# Define component information
COMPONENTS = {
    'server': {
        'module': 'server.main',
        'default_port': config.get('server.port'),
        'depends_on': [],
    },
    'relay': {
        'module': 'relay.main',
        'default_port': config.get('relay.port'),
        'depends_on': ['server'],
    },
    'api': {
        'module': 'api.main',
        'default_port': config.get('api.port'),
        'depends_on': ['relay'],
    },
}

running_processes: Dict[str, subprocess.Popen] = {}

def check_dependencies():
    """
    Check if all required dependencies are installed.
    Returns True if all dependencies are met, False otherwise.
    """
    try:
        # Check Python version
        python_version = tuple(map(int, platform.python_version_tuple()))
        if python_version < (3, 8):
            logger.error(f"Python 3.8+ is required, but you have {platform.python_version()}")
            return False

        # Try to import key dependencies
        for module in ("cryptography", "fastapi", "pydantic"):
            importlib.import_module(module)

        logger.info("All key dependencies are met")
        return True
    except ImportError as e:
        logger.error(f"Missing dependency: {str(e)}")
        logger.info("Please run 'pip install -r requirements.txt' to install required dependencies")
        return False

def start_component(component_name: str, port: Optional[int] = None) -> bool:
    """
    Start a component by name.

    Args:
        component_name: Name of the component to start
        port: Optional port override

    Returns:
        True if component started successfully, False otherwise
    """
    if component_name not in COMPONENTS:
        logger.error(f"Unknown component: {component_name}")
        return False

    component = COMPONENTS[component_name]

    # Start dependencies first
    for dependency in component['depends_on']:
        if dependency not in running_processes:
            if not start_component(dependency):
                logger.error(f"Failed to start dependency {dependency} for {component_name}")
                return False

    # Prepare the command
    cmd = [sys.executable, '-m', component['module']]

    # Add the port if specified
    if port:
        cmd.extend(['--port', str(port)])

    # Create the environment variables
    env = os.environ.copy()
    env['TOKEN_PLACE_ENV'] = config.env
    env['PLATFORM'] = config.platform

    # Create the log file
    log_dir = config.get('paths.logs_dir')
    ensure_dir_exists(log_dir)
    log_file = os.path.join(log_dir, f"{component_name}.log")

    try:
        logger.info(f"Starting {component_name} on port {port or component['default_port']}...")

        with open(log_file, 'a') as log_f:
            # Start the process
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=log_f,
                stderr=log_f,
                text=True
            )

        # Add to running processes
        running_processes[component_name] = process

        # Wait a bit to check if process started successfully
        time.sleep(1)

        # Check if process is still running
        if process.poll() is not None:
            logger.error(f"{component_name} failed to start. Check the log at {log_file}")
            return False

        logger.info(f"{component_name} started successfully (PID: {process.pid})")
        return True

    except Exception as e:
        logger.error(f"Error starting {component_name}: {str(e)}")
        return False

def stop_component(component_name: str) -> bool:
    """
    Stop a component by name.

    Args:
        component_name: Name of the component to stop

    Returns:
        True if component stopped successfully, False otherwise
    """
    if component_name not in running_processes:
        logger.warning(f"{component_name} is not running")
        return True

    process = running_processes[component_name]

    try:
        logger.info(f"Stopping {component_name} (PID: {process.pid})...")

        # Send the termination signal
        if IS_WINDOWS:
            # On Windows, use taskkill to terminate the process tree
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=False)
        else:
            # On Unix-like systems, use SIGTERM
            process.terminate()

            # Wait for process to terminate
            for _ in range(5):
                if process.poll() is not None:
                    break
                time.sleep(1)

            # If process is still running, use SIGKILL
            if process.poll() is None:
                logger.warning(f"{component_name} didn't terminate gracefully, forcing...")
                process.kill()

        # Wait for the process to terminate
        process.wait(timeout=5)

        # Remove from running processes
        del running_processes[component_name]

        logger.info(f"{component_name} stopped successfully")
        return True

    except Exception as e:
        logger.error(f"Error stopping {component_name}: {str(e)}")
        return False

def stop_all():
    """Stop all running components in reverse dependency order."""
    components_to_stop = list(running_processes.keys())

    # Stop in reverse dependency order
    for component_name in reversed(components_to_stop):
        stop_component(component_name)

def signal_handler(_sig, _frame):
    """Handle termination signals."""
    logger.info("Termination signal received, stopping all components...")
    stop_all()
    sys.exit(0)

def main():
    """Main entry point for the launcher."""
    parser = argparse.ArgumentParser(description='Cross-platform launcher for token.place components')
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status'], help='Action to perform')
    parser.add_argument('--component', choices=['all', 'server', 'relay', 'api'], default='all', help='Component to manage')
    parser.add_argument('--port', type=int, help='Override the default port')
    parser.add_argument('--env', choices=['development', 'testing', 'production'], help='Override the environment')
    args = parser.parse_args()

    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Override environment if specified
    if args.env:
        os.environ['TOKEN_PLACE_ENV'] = args.env

    # Check dependencies first
    if args.action == 'start' and not check_dependencies():
        sys.exit(1)

    # Get the components to manage
    components = list(COMPONENTS.keys()) if args.component == 'all' else [args.component]

    # Execute the requested action
    if args.action == 'start':
        for component in components:
            start_component(component, args.port)

    elif args.action == 'stop':
        for component in reversed(components):
            stop_component(component)

    elif args.action == 'restart':
        for component in reversed(components):
            stop_component(component)
        for component in components:
            start_component(component, args.port)

    elif args.action == 'status':
        for component in components:
            if component in running_processes and running_processes[component].poll() is None:
                logger.info(f"{component} is running (PID: {running_processes[component].pid})")
            else:
                logger.info(f"{component} is not running")

    # If we're not stopping everything, keep the script running to monitor the processes
    if args.action in ['start', 'restart'] and args.component != 'all':
        try:
            while any(p.poll() is None for p in running_processes.values()):
                time.sleep(1)
        except KeyboardInterrupt:
            stop_all()

if __name__ == '__main__':
    main()
