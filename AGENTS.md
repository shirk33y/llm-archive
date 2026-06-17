# llm-archive — AGENTS.md

## Release process

1. Bump version in `pyproject.toml` (`[project] version`)
2. Commit: `git add pyproject.toml && git commit -m "vX.Y.Z"`
3. Tag and push: `git tag vX.Y.Z && git push origin main --tags`
4. Compute new archive SHA:

   ```
   curl -sL "https://github.com/shirk33y/llm-archive/archive/refs/tags/vX.Y.Z.tar.gz" | sha256sum
   ```

5. Update `Formula/llm-archive.rb` — bump `version` and `sha256`
6. Copy formula to the tap repo:

   ```
   cp Formula/llm-archive.rb /home/linuxbrew/.linuxbrew/Homebrew/Library/Taps/shirk33y/homebrew-tap/Formula/llm-archive.rb
   ```

7. Commit and push from the tap repo:

   ```
   git -C /home/linuxbrew/.linuxbrew/Homebrew/Library/Taps/shirk33y/homebrew-tap add -A
   git -C /home/linuxbrew/.linuxbrew/Homebrew/Library/Taps/shirk33y/homebrew-tap commit -m "llm-archive vX.Y.Z"
   git -C /home/linuxbrew/.linuxbrew/Homebrew/Library/Taps/shirk33y/homebrew-tap push
   ```
