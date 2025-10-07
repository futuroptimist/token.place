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

### Logo usage guidelines

`token.place` ships a minimalist logomark derived from the production favicon. The canonical
vector source lives at `static/favicon.svg`, and a multi-resolution raster bundle is available as
`static/icon.ico` for legacy platforms. Always source artwork directly from these files instead of
re-exporting screenshots or compressed assets.

- Maintain a clear space equal to the height of the "t" glyph on all sides. This spacing prevents
  neighboring interface elements from crowding the logomark and keeps the glyph legible at small
  sizes.
- Preserve the original cyan-on-dark color pairing. If you must place the mark on a light surface,
  invert it by rendering the glyph in `#00FFFF` atop a solid `#111111` circular backdrop sized to the
  logomark's viewbox.
- Do not apply drop shadows, gradients, or color shifts. The favicon's flat styling ensures the
  brand remains crisp across Retina, OLED, and e-ink displays.
- When a square avatar is required (for example, Slack workspaces or GitHub org icons), use the ICO
  file directly so operating systems can pick the resolution that best matches the display density.

### Animated usage

Avoid animating the logo. Motion is reserved for contextual loading indicators inside the
application UI to keep the brand calm and trustworthy.

## Color Scheme

`token.place` uses a high-contrast palette that mirrors the production chat UI. The
brand colors are defined in `utils/branding/colors.py` and are available to both
Python and JavaScript tooling via the exported `BRAND_COLORS` mapping.

| Token          | Hex Code | Typical Usage                                               |
| -------------- | -------- | ----------------------------------------------------------- |
| primary cyan   | `#00FFFF`| Primary accent for call-to-action buttons and focus states. |
| accent blue    | `#007BFF`| Secondary links, toggles, and hover states.                 |
| accent green   | `#4CAF50`| Success badges and “system healthy” notifications.          |
| background dark| `#111111`| Default chat background in dark mode.                       |
| background light| `#FFFFFF`| Base background in light mode.                             |
| surface dark   | `#1A1A1A`| Message bubbles and cards in dark mode.                     |
| surface light  | `#F5F5F5`| Message bubbles and cards in light mode.                    |
| text on dark   | `#FFFFFF`| Primary copy when rendered over dark surfaces.             |
| text on light  | `#333333`| Primary copy when rendered over light surfaces.            |

Two derived palettes help designers and engineers pick the right semantic colors:

- **Dark mode palette**: background `#111111`, surface `#1A1A1A`, text `#FFFFFF`, accent
  `#00FFFF`, supporting accent `#007BFF`.
- **Light mode palette**: background `#FFFFFF`, surface `#F5F5F5`, text `#333333`, accent
  `#007BFF`, supporting accent `#4CAF50`.

These values are intentionally duplicated in code so automated tooling, lint rules, and
tests can detect accidental drift between the implementation and the brand guidelines.

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
