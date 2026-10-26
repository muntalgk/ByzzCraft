# Contributing

Thanks for helping! Bug reports, translations and pull requests are welcome.

## Ground rules

- **Python standard library only**, Python 3.10 or newer. No `pip install`, no build step: people run it with
  `git clone` and `python panel.py`.
- **Windows and Linux** both. Paths with `os.path`, no shell-only tricks; if something differs, handle both.
- **No Mojang files in the repository** (textures, sounds, language files, jars). The panel downloads them on
  the user's machine.
- **Interface texts in English**, wrapped in `t("...")` in Python, `{{...}}` in the HTML and `_t("...")` in the
  JavaScript. Add the Italian line to `byzzcraft/i18n/it.json`; a new language is a new `byzzcraft/i18n/<code>.json`.
- **Code and comments in English.** Comment the why, not the what.
- **Lines the panel writes to `panel.log`** (the activity log) are parsed by `byzzcraft/activity.py`: if you add or change a
  `log(...)`, add its rule there and a case in `tests/test_activity.py`.

## Tests

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

Every test file prints `PASS n FAIL m` and exits non-zero on failure. `tests/test_http.py` starts the real panel
on a free port with a fake server jar: no network, no Minecraft. GitHub Actions runs everything on Ubuntu and
Windows with Python 3.10, 3.12 and 3.14.

For a change in how the server starts, stops or is backed up, also try it with a real server (vanilla and, if it
matters, Paper or Fabric) and say in the pull request what you tried.

## Pull requests

- One change per pull request, with a short description of what and why.
- New behaviour gets a test, or an explanation of why it cannot have one.
- Update `README.md` and `README.it.md` when users would notice the change, and add a line to `CHANGELOG.md`.

## Releases

1. In a pull request: `VERSION` in `panel.py` and the `## Unreleased` heading of `CHANGELOG.md` become the new
   version (`1.2.0`), with any manual upgrade steps in that section.
2. After the merge, tag the merge commit and push the tag: `git tag -a v1.2.0 -m "ByzzCraft 1.2.0"`, then
   `git push origin v1.2.0`.
3. `ci.yml` publishes `:latest`, `:1.2` and `:1.2.0` on GHCR and the GitHub release with the Windows zip, its
   `SHA256SUMS` and that CHANGELOG section as notes. Panels then offer the update.

## Security

Do not open a public issue for a vulnerability: see [SECURITY.md](SECURITY.md).
