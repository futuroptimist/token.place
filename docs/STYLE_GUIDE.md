# Style Guide for `token.place`

This document outlines the style and branding guidelines for the `token.place` project. Following these guidelines helps maintain consistency across documentation, code, and communications.

## Project Name

### Correct Stylization

Always style the project name as lowercase `token.place`:

- ✅ `token.place`
- ❌ `Token.place`
- ❌ `TokenPlace`
- ❌ `Token Place`
- ❌ `token-place`

The lowercase styling emphasizes that it is a URL and reflects the modern, technical nature of the project.

### Usage in Documentation

- When referring to the project in documentation, always use the format `token.place`.
- In headings and titles, you may use `token.place` even at the beginning of a sentence.
- When used in a sentence, do not capitalize even at the beginning of a sentence. Instead, rephrase the sentence if needed.

Examples:
- ✅ "Welcome to `token.place`."
- ✅ "`token.place` provides end-to-end encryption."
- ✅ "This guide explains how to use `token.place`."
- ❌ "Token.place is a secure service."

### Usage in Code

- In variable names, use `tokenPlace` (camelCase) or `token_place` (snake_case) depending on the language conventions.
- In class names, use `TokenPlace` (PascalCase).
- For file names, use `token_place` (snake_case) for Python files and `tokenPlace` (camelCase) for JavaScript files.
- In comments and docstrings, refer to the project as `token.place`.

Examples:
```python
# Python (snake_case)
token_place_client = TokenPlaceClient()
```

```javascript
// JavaScript (camelCase)
const tokenPlaceConfig = {
  endpoint: 'https://token.place/api'
};
```

### Usage in URLs

For URLs and domains, always use the full form:

- ✅ `https://token.place`
- ✅ `https://api.token.place`
- ✅ `https://docs.token.place`

## Logo and Visual Elements

[TBD: Add logo usage guidelines when a logo is created]

## Color Scheme

[TBD: Add color scheme when finalized]

## Language and Tone

- Use clear, concise language
- Be technical but accessible
- Avoid jargon where possible
- Focus on security and privacy as key benefits

## Documentation Style

- Use Markdown for all documentation
- Follow Google Style for Python docstrings
- Use JSDoc for JavaScript documentation
- Include examples for all API endpoints

## Version References

When referring to versions of `token.place`:

- Use semantic versioning (MAJOR.MINOR.PATCH)
- Reference versions as `token.place v1.0.0` not `Token.place Version 1.0.0`

By following these guidelines, we ensure consistent presentation of the `token.place` brand across all platforms and materials.
