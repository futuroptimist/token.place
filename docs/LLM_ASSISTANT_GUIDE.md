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


## API v1 relay rules for assistants (must-follow)

- API v1 is the active API for `v0.1.0` and the only active runtime target.
- API v1 is non-streaming for relay/client-server inference; return full responses only.
- Do not add streaming to API v1.
- API v2 is incomplete; do not route runtime traffic through API v2 until API v1 launch and
  `v0.1.0` finalization.
- Deprecated legacy relay endpoints (`/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server`)
  are historical compatibility only. Do not use or extend them for active production paths, and do
  not reintroduce them as fallbacks.
- Keep active inference paths aligned on API v1 E2EE routes for `server.py`, `relay.py`,
  `client.py`, desktop Tauri flows, and relay HTML chat UI.
- Relay-visible surfaces must stay ciphertext-only (+ safe routing metadata). Plaintext prompts,
  messages, responses, tool arguments, or model output text must never appear in relay-owned
  state/logs/diagnostics/payloads.
- If a path cannot preserve E2EE, fail closed.

Migration context: there is a known gap between `relay.py`, desktop Tauri, and relay HTML chat UI
where some E2E segments still use legacy routes. Follow-up migration work owns the remaining repairs.
Reference: [docs/architecture/api_v1_e2ee_relay.md](architecture/api_v1_e2ee_relay.md).

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
