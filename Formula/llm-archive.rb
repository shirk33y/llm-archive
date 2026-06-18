class LlmArchive < Formula
  include Language::Python::Virtualenv

  desc "Local archive for AI chats — sync web and file providers into SQLite"
  homepage "https://github.com/shirk33y/llm-archive"
  url "https://github.com/shirk33y/llm-archive/archive/refs/tags/v0.3.0.tar.gz"
  sha256 "54c5c4ff4cae02063ad49ad70926c7427bd9fc0e851debbed73bb9158aa18d05"
  version "0.3.0"
  head "https://github.com/shirk33y/llm-archive.git", branch: "main"
  license "All rights reserved"

  depends_on "python@3.13"

  def install
    venv = virtualenv_create(libexec, "python3")
    system libexec/"bin/python", "-m", "ensurepip", "--upgrade"
    system libexec/"bin/python", "-m", "pip", "install", "--no-cache-dir", buildpath
    bin.install_symlink libexec/"bin/llm-archive"
  end

  service do
    run [opt_bin/"llm-archive", "service"]
    keep_alive true
    log_path var/"log/llm-archive.log"
    error_log_path var/"log/llm-archive.log"
  end

  test do
    assert_match "llm-archive", shell_output("#{bin}/llm-archive --help")
    assert_match "sync", shell_output("#{bin}/llm-archive --help")
  end
end
