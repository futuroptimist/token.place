# Guide for LLM Assistants Working with token.place

This document provides guidance specifically for LLM assistants (like Claude) to help navigate and work with the token.place codebase more effectively.

## Project Overview

- **token.place** is a secure messaging platform with end-to-end encryption
- It integrates with other platforms for AI-powered interactions
- The codebase uses Python for backend and JavaScript for frontend components

## Key Directories

- `/`: Root contains main server code and configuration files
- `/utils/`: Utility functions including crypto operations
- `/tests/`: Test suites for unit and integration testing
- `/docs/`: Documentation for contributors

## Environment-Specific Considerations

### Windows/PowerShell Environment

When helping users in a Windows/PowerShell environment:

1. Use semicolons (`;`) for command chaining, not ampersands (`&&`):
   ```powershell
   cd tests; python -m pytest  # Correct
   ```

2. Use backslashes (`\`) for file paths:
   ```powershell
   cd tests\unit
   ```

3. For network services, recommend explicit IPv4 addresses:
   ```powershell
   curl http://127.0.0.1:5000/test  # Preferred over localhost
   ```

4. Suggest these commands for port management:
   ```powershell
   # Check processes on a port
   netstat -ano | findstr :5000

   # Kill process
   taskkill /F /PID <pid>
   ```

### Linux/Unix Environment

When helping users in a Linux/Unix environment:

1. Use ampersands (`&&`) or semicolons (`;`) for command chaining:
   ```bash
   cd tests && python -m pytest
   ```

2. Use forward slashes (`/`) for file paths:
   ```bash
   cd tests/unit
   ```

3. For port management:
   ```bash
   # Check processes on a port
   lsof -i :5000

   # Kill process
   kill -9 <pid>
   ```

## Common Tasks and Solutions

### Running Tests

```powershell
# Windows - run all tests
python -m pytest

# Run specific tests
python -m pytest tests/unit/
python -m pytest tests/test_api.py
```

### Debugging Connection Issues

If a user reports connection issues:

1. Check if the server is running:
   ```powershell
   netstat -ano | findstr :<port>
   ```

2. Suggest using explicit IPv4 addresses:
   ```
   Change localhost:<port> to 127.0.0.1:<port>
   ```

3. Look for port conflicts:
   ```powershell
   netstat -ano | findstr :<port>
   ```

## Best Practices When Assisting

1. Always check for platform-specific issues (Windows vs Linux)
2. Suggest explicit IPv4 addressing for network services
3. Remind users to clean up processes after testing
4. Reference the relevant documentation files when providing guidance

## Key Documentation References

When assisting users, refer them to these key documents:

- [CONTRIBUTING.md](../CONTRIBUTING.md): General contribution guidelines
- [tests/README.md](../tests/README.md): General testing guide
- [ONBOARDING.md](ONBOARDING.md): Repository overview and setup
- [AGENTS.md](../AGENTS.md): Automation helpers and required checks
- [STYLE_GUIDE.md](STYLE_GUIDE.md): Naming and style conventions

---

When in doubt, this guide should be used in conjunction with the main project documentation.


## API v1-only runtime baseline (v0.1.0)

- API v1 is the active runtime API for v0.1.0.
- API v1 is non-streaming; relay-path responses are returned only after full generation.
- Do not add streaming to API v1.
- API v2 exists but is incomplete; do not route active runtime traffic through API v2 yet.
- Deprecated legacy relay endpoints: `/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server`.
- Do not use, extend, or reintroduce legacy endpoints in active production paths.
- Required alignment for active paths: `server.py`, `relay.py`, `client.py`, desktop Tauri, and relay HTML chat UI must use API v1 E2EE relay routes.
- E2EE invariant: relay sees ciphertext plus safe routing metadata only; if E2EE cannot be preserved, fail closed.

Migration note: there is a known gap between `relay.py`, desktop Tauri, and relay HTML chat UI where some paths still touch legacy routes. Prompt sequence 1-4 owns that runtime migration work.

See [architecture/api_v1_e2ee_relay.md](architecture/api_v1_e2ee_relay.md).
