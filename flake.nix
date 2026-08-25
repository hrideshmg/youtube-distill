{
  description = "DevShell Template";

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
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            nodejs
          ];

          shellHook = "echo hi!";
        };
      }
    );
}
