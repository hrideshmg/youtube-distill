{
  description = "youtube-distill devshell";

  inputs = {
    nixos-config.url = "path:/home/hridesh/nix-config";
    nixpkgs.follows = "nixos-config/nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python313;
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.ffmpeg
          ];

          # Make uv use the Nix-provided interpreter instead of downloading its own.
          UV_PYTHON = "${python}/bin/python";
          UV_PYTHON_DOWNLOADS = "never";
        };
      }
    );
}
