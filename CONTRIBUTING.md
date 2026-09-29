# Contributing

Bug reports, provider compatibility fixes, and improvements to installation or
display are welcome. Open an issue or pull request on GitHub.

Run the offline suite before submitting:

```sh
python3 -m unittest discover -s tests -v
python3 -m ai_watch --demo --once
```

Keep Python 3.10 compatibility and avoid adding runtime dependencies unless
there is a clear benefit. Provider probes, configuration, and display are
separate modules. A provider failure should leave other accounts usable and
should never turn missing quota data into zero usage.

Use fabricated accounts and tokens in tests. Never attach credential files,
real access tokens, or raw provider responses containing account details to an
issue. For screenshots, `ai-watch --demo` supplies synthetic data. Include your
OS, Python version, provider CLI version, command, and the sanitized error when
reporting a compatibility issue.
