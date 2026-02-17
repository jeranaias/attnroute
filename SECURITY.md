# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | Yes |
| < 1.0   | No  |

## Reporting a Vulnerability

If you discover a security vulnerability in attnroute, please report it responsibly:

1. **Email**: Send details to jeranaias@gmail.com
2. **Do NOT** open a public GitHub issue for security vulnerabilities
3. Include steps to reproduce the issue
4. You will receive an acknowledgment within 48 hours

## Security Measures

### Input Validation
- Stdin size limits (10MB max) to prevent memory exhaustion
- Path traversal prevention with allowed-directory validation
- Plugin name validation (blocks path separators, null bytes, Windows reserved names)

### Platform-Specific Protections
- Windows Alternate Data Stream (ADS) blocking
- Windows reserved device name blocking (CON, NUL, COM1, etc.)
- Null byte injection prevention

### Data Integrity
- Atomic file writes (temp-file-then-rename pattern)
- Type validation on all JSON loaders
- TOCTOU race condition elimination (try/except patterns)

## Scope

attnroute processes local file paths and injects context into Claude Code prompts. It does NOT:
- Send data to external servers (except optional Claude API compression feature)
- Store credentials or secrets
- Execute arbitrary code from remote sources
