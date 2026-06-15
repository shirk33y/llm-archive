class LlmArchive < Formula
  include Language::Python::Virtualenv

  desc "Local archive for AI chats — sync web and file providers into SQLite"
  homepage "https://github.com/shirk33y/llm-archive"
  url "https://github.com/shirk33y/llm-archive.git", branch: "main"
  head "https://github.com/shirk33y/llm-archive.git", branch: "main"
  license :all_rights_reserved

  depends_on "python@3.13"

  def install
    venv = virtualenv_create(libexec, "python3")
    system venv/"bin/pip", "install", buildpath
    bin.install_symlink venv/"bin/llm-archive"
  end

  service do
    run [opt_bin/"llm-archive", "service"]
    keep_alive true
    log_path var/"log/llm-archive.log"
    error_log_path var/"log/llm-archive.log"
  end

  test do
    assert_match "llm-archive", shell_output("#{bin}/llm-archive --help")
    assert_match "not synced", shell_output("#{bin}/llm-archive sources")
  end
end
