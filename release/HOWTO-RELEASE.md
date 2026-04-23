# Cutting a release

This is the maintainer-facing doc. End users should read `README.md`.

## One-time setup

1. Push the repo to GitHub.
2. Confirm `Settings → Actions → General → Workflow permissions` is set to
   **Read and write permissions**. The release workflow needs this to
   create releases.

## Cut a release

```bash
# 1. bump version
echo "0.2.0" > release/VERSION
# (optional) bump backend/__init__.py __version__ to match

# 2. update release notes
$EDITOR release/RELEASE_NOTES.md

# 3. commit
git add release/ backend/__init__.py
git commit -m "release: 0.2.0"

# 4. tag + push
git tag v0.2.0
git push origin main
git push origin v0.2.0
```

The `Release` GitHub Actions workflow triggers on the tag push, builds
`ClaudeTracker.exe` on `windows-latest`, zips the extension, and attaches
both to a new GitHub Release named `ClaudeTracker v0.2.0`.

## Dry run

Use the workflow's **Run workflow** button in the Actions tab. It will
build the artifacts and upload them as a workflow artifact (no release
created). Good for verifying the build before tagging.

## Manual build

Locally, from repo root:

```powershell
.\build.ps1
Compress-Archive -Path extension/* -DestinationPath dist/ClaudeTracker-extension.zip
```

Output lands in `dist/`.
