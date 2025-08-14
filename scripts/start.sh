#!/bin/bash
# Unix shell script to launch token.place (Linux/macOS)

# Detect platform
if [[ "$OSTYPE" == "darwin"* ]]; then
    export PLATFORM="darwin"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    export PLATFORM="linux"
else
    export PLATFORM="unknown"
fi

# Detect Python
if ! command -v python3 &> /dev/null; then
    echo "Python 3 not found in PATH."
    echo "Please install Python 3.8 or higher and make sure it's in your PATH."
    exit 1
fi

# Set default environment variables
export TOKEN_PLACE_ENV="development"

# Parse arguments
ACTION="start"
COMPONENT="all"
PORT=""
ENV="$TOKEN_PLACE_ENV"

while [[ $# -gt 0 ]]; do
    case $1 in
        start|stop|restart|status)
            ACTION="$1"
            shift
            ;;
        --component)
            COMPONENT="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --env)
            ENV="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            shift
            ;;
    esac
done

# Set environment variables
export TOKEN_PLACE_ENV="$ENV"

# Build command
CMD="python3 -m scripts.launcher $ACTION --component $COMPONENT"
if [[ -n "$PORT" ]]; then
    CMD="$CMD --port $PORT"
fi
if [[ -n "$ENV" ]]; then
    CMD="$CMD --env $ENV"
fi

# Make script executable on first run if needed
if [[ ! -x "$0" ]]; then
    chmod +x "$0"
fi

# Run the command
echo "Running: $CMD"
$CMD

exit $?
