#!/usr/bin/env fish
# Sets up the venv and installs encoder-thing to ~/.local/bin.

set dir (dirname (realpath (status filename)))

echo "==> Creating venv..."
python3 -m venv $dir/.venv
or begin
    echo "error: python3 -m venv failed" >&2
    exit 1
end

echo "==> Installing dependencies..."
$dir/.venv/bin/pip install --quiet rich
or begin
    echo "error: pip install failed" >&2
    exit 1
end

chmod +x $dir/encoder-thing

echo "==> Done."
echo ""
echo "Install encoder-thing to ~/.local/bin? [y/N] " && read -l response
if test "$response" = y -o "$response" = Y
    mkdir -p ~/.local/bin
    ln -sf $dir/encoder-thing ~/.local/bin/encoder-thing
    echo "Linked: ~/.local/bin/encoder-thing -> $dir/encoder-thing"
    echo ""
    echo "Make sure ~/.local/bin is on your PATH:"
    echo "  fish_add_path ~/.local/bin"
else
    echo "Skipped. Run directly with: $dir/encoder-thing"
end
