# Development Guide

## Dependency Management

### Preventing Dependency Conflicts

This project has experienced dependency conflicts in the past. To prevent regressions:

#### 1. **Critical Dependency Relationships**

- `pytest-benchmark 5.1.0+` requires `pytest>=8.1`
- `pytest-playwright 0.7.0+` supports `pytest 8.x` (older versions only support pytest <8.0)
- `playwright>=1.40.0` is required by `pytest-playwright 0.7.0+`

#### 2. **Before Updating Dependencies**

Always run the dependency validator before making changes:

```bash
python scripts/validate_dependencies.py
```

#### 3. **When Adding New Dependencies**

1. Check compatibility with existing packages
2. Update the comments in `requirements.txt` if needed
3. Run the validator script
4. Test locally before committing

#### 4. **Version Pinning Strategy**

- **Pin exact versions** for testing dependencies that are known to have compatibility issues
- **Use range constraints** (`>=`) for core dependencies that are stable
- **Document critical relationships** in `requirements.txt` comments

### Common Dependency Issues

#### Pytest Version Conflicts

**Problem**: pytest-benchmark 5.1.0+ requires pytest>=8.1, but some plugins may not support pytest 8.x yet.

**Solution**:
1. Check plugin compatibility before upgrading
2. Use compatible versions (e.g., pytest-playwright 0.7.0+ for pytest 8.x support)
3. Update all related packages together

#### Playwright Browser Binaries

**Problem**: Playwright tests fail because browser binaries aren't installed.

**Solution**: Always run `playwright install` after installing/upgrading playwright packages.

### CI Pipeline Protection

The CI pipeline includes:
1. **Dependency validation** - Checks for conflicts before installation
2. **Playwright browser installation** - Ensures browser binaries are available
3. **Comprehensive test suite** - Catches integration issues early

### Local Development Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install Playwright browsers
playwright install

# 3. Validate everything works
python scripts/validate_dependencies.py

# 4. Run tests
./run_all_tests.sh
```

### Troubleshooting

#### "ResolutionImpossible" Error

This indicates conflicting version constraints. Common causes:
- Plugin requires older pytest version
- Multiple packages require incompatible versions of the same dependency

**Fix**: Update to compatible versions or use dependency ranges instead of exact pins.

#### Missing Playwright Browsers

Error: `Executable doesn't exist at .../chrome-linux/headless_shell`

**Fix**: Run `playwright install` to download browser binaries.

### Best Practices

1. **Test dependency changes locally** before committing
2. **Use the validator script** regularly
3. **Keep testing dependencies up-to-date** together
4. **Document critical relationships** in requirements.txt
5. **Monitor CI failures** for early warning signs
